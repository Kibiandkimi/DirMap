#!/usr/bin/env python3
"""Receive a GitHub-built release over a restricted SSH connection.

Uses the existing /opt/dirmap layout. Code, runtime selection and rollback
move together; the shared teaching data and configuration are never copied.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.request import urlopen
import uuid

MAX_BYTES = 200 * 1024 * 1024
RELEASE_NAME = re.compile(r'\d{8}T\d{6}Z(?:-[0-9a-f]{12}-\d+-\d+)?')


def allowed_file(name):
    path = PurePosixPath(name)
    if path.is_absolute() or any(p in ('', '.', '..') for p in name.split('/')):
        return False
    if name in ('deployment.json', 'requirements.lock', 'requirements.txt', 'package.json', 'package-lock.json',
                'deploy/receiver.py', 'deploy/run-server.sh'):
        return True
    if len(path.parts) == 1 and path.suffix.lower() in ('.jpg', '.jpeg', '.png', '.pdf'):
        return True
    if len(path.parts) == 2 and path.parts[0] == 'dirmap' and path.suffix in ('.py', '.cpp', '.json'):
        return True
    return len(path.parts) >= 3 and path.parts[:2] == ('web', 'dist') and not any(p.startswith('.') for p in path.parts)


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def extract_release(archive, target, revision):
    seen, total = set(), 0
    with tarfile.open(archive, 'r:gz') as source:
        for member in source:
            if not member.isfile() or not allowed_file(member.name) or member.name in seen:
                raise ValueError('Release contains an unexpected path, duplicate, or link')
            total += member.size
            if member.size < 0 or total > MAX_BYTES:
                raise ValueError('Release exceeds the size limit')
            seen.add(member.name)
            destination = target/member.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            with source.extractfile(member) as incoming, destination.open('xb') as outgoing:
                shutil.copyfileobj(incoming, outgoing)
            destination.chmod(0o644)
    manifest = json.loads((target/'deployment.json').read_text())
    if manifest.get('schema') != 1 or manifest.get('revision') != revision:
        raise ValueError('Release revision does not match the requested commit')
    files = manifest['files']
    if set(files) != seen-{'deployment.json'}:
        raise ValueError('Release file inventory does not match its manifest')
    for name, expected in files.items():
        if digest(target/name) != expected:
            raise ValueError(f'Release checksum mismatch: {name}')
    for name in ('dirmap/app.py', 'web/dist/index.html', 'web/dist/sw.js', 'requirements.lock',
                 'deploy/receiver.py', 'deploy/run-server.sh'):
        if name not in files:
            raise ValueError(f'Release is missing {name}')
    return manifest


def pinned_versions(lock):
    pins = {}
    for line in lock.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        match = re.fullmatch(r'([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+!-]+)', line)
        if not match or match[1] in pins:
            raise ValueError('requirements.lock must contain unique exact version pins')
        pins[match[1]] = match[2]
    if not pins:
        raise ValueError('requirements.lock is empty')
    return pins


def runtime_matches(python, pins):
    if not python.is_file():
        return False
    script = ('import importlib.metadata as m,json,sys; '
              'pins=json.loads(sys.argv[1]); '
              'sys.exit(0 if all(m.version(n)==v for n,v in pins.items()) else 1)')
    env = {**os.environ, 'PYTHONPATH': '', 'PYTHONNOUSERSITE': '1'}
    result = subprocess.run([str(python), '-B', '-c', script, json.dumps(pins)], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    return result.returncode == 0


def prepare_runtime(root, release):
    lock = release/'requirements.lock'
    pins = pinned_versions(lock)
    base = root/'venv'
    if runtime_matches(base/'bin/python', pins):
        return base
    runtime = root/'venvs'/digest(lock)
    if not runtime_matches(runtime/'bin/python', pins):
        # Never install into the environment of a running or older release.
        # A partly installed, unreferenced environment can be rebuilt safely.
        for old in (root/'releases').iterdir():
            if (old/'.venv').is_symlink() and (old/'.venv').resolve() == runtime:
                raise RuntimeError('A referenced runtime is damaged; refusing to modify it in place')
        if runtime.exists():
            shutil.rmtree(runtime)
        runtime.parent.mkdir(exist_ok=True)
        subprocess.run([sys.executable, '-m', 'venv', str(runtime)], check=True, timeout=120)
        subprocess.run([str(runtime/'bin/python'), '-m', 'pip', 'install', '--no-cache-dir',
                        '--disable-pip-version-check', '--only-binary=:all:', '-r', str(lock)],
                       check=True, timeout=900)
        subprocess.run([str(runtime/'bin/python'), '-m', 'pip', 'check'], check=True, timeout=30)
        if not runtime_matches(runtime/'bin/python', pins):
            raise RuntimeError('Installed Python packages do not match requirements.lock')
    return runtime


def health(port):
    try:
        with urlopen(f'http://127.0.0.1:{port}/api/health', timeout=4) as response:
            return json.load(response)
    except (OSError, ValueError):
        return {}


def wait_health(port, revision=None, timeout=90):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        body = health(port)
        if body.get('status') == 'ok' and (revision is None or body.get('revision') == revision):
            return True
        time.sleep(1)
    return False


def switch_current(root, target):
    temporary = root/('.current-'+uuid.uuid4().hex)
    temporary.symlink_to(target)
    temporary.replace(root/'current')


def restart():
    subprocess.run(['sudo', '-n', 'systemctl', 'restart', 'dirmap.service'], check=True, timeout=60)


def activate(root, release, revision, port=8765):
    current = root/'current'
    if not current.is_symlink():
        raise RuntimeError('Existing deployment must have a current symlink')
    previous = current.resolve(strict=True)
    switched = False
    try:
        switch_current(root, release)
        switched = True
        restart()
        if not wait_health(port, revision):
            raise RuntimeError('New process did not report the expected commit as healthy')
    except BaseException:
        if switched:
            print('Activation failed; restoring the previous release.', flush=True)
            switch_current(root, previous)
            restart()
            old_metadata = previous/'deployment.json'
            old_revision = json.loads(old_metadata.read_text()).get('revision') if old_metadata.exists() else None
            if not wait_health(port, old_revision, timeout=60):
                raise RuntimeError('Rollback health check failed; inspect dirmap.service')
        raise
    return previous


def prune_releases(root, previous, keep=5):
    current = (root/'current').resolve()
    releases = sorted((p for p in (root/'releases').iterdir()
                       if not p.is_symlink() and p.is_dir() and RELEASE_NAME.fullmatch(p.name)), reverse=True)
    retained = {current, previous}
    for path in releases:
        if path in retained:
            continue
        if len(retained) < keep:
            retained.add(path)
        else:
            shutil.rmtree(path)  # rmtree does not follow data/.venv symlinks


def deploy(root, archive, revision, run_id):
    name = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())+'-'+revision[:12]+'-'+run_id
    release = root/'releases'/name
    if release.exists():
        raise ValueError('Release already exists; use a new workflow attempt')
    with tempfile.TemporaryDirectory(prefix='.staging-', dir=root/'releases') as temporary:
        staging = Path(temporary)
        extract_release(archive, staging, revision)
        runtime = prepare_runtime(root, staging)
        (staging/'.venv').symlink_to(runtime)
        if not (root/'data').is_dir():
            raise RuntimeError('Shared teaching data directory is missing')
        (staging/'data').symlink_to(root/'data')
        staging.rename(release)
    previous = activate(root, release, revision)
    # Refresh only this receiver after a successful deployment; a broken
    # release cannot replace the working receiver or its SSH restrictions.
    for source, destination, mode in (('receiver.py', 'github-receiver.py', 0o644),
                                      ('run-server.sh', 'dirmap-run', 0o755)):
        helper = root/'bin'/destination
        replacement = helper.with_suffix('.new')
        shutil.copyfile(release/'deploy'/source, replacement)
        replacement.chmod(mode)
        replacement.replace(helper)
    try:
        prune_releases(root, previous)
    except OSError as error:
        print(f'Release is healthy; old release cleanup deferred: {error}', file=sys.stderr)
    print(json.dumps({'status': 'deployed', 'revision': revision, 'releaseId': name}), flush=True)


def main():
    root = Path(os.environ.get('DIRMAP_DEPLOY_ROOT', '/opt/dirmap')).resolve()
    command = shlex.split(os.environ.get('SSH_ORIGINAL_COMMAND', ''))
    if command == ['probe']:
        print(json.dumps({'current': str((root/'current').resolve()), 'health': health(8765)}))
        return
    if (len(command) != 4 or command[0] != 'deploy' or not re.fullmatch('[0-9a-f]{40}', command[1])
            or not re.fullmatch(r'\d+-\d+', command[2]) or not re.fullmatch('[0-9a-f]{64}', command[3])):
        raise ValueError('Only probe or deploy <commit> <run-attempt> <sha256> is permitted')
    def interrupted(*_):
        raise InterruptedError('Deployment interrupted')
    for name in ('SIGHUP', 'SIGINT', 'SIGTERM'):
        signal.signal(getattr(signal, name), interrupted)
    with (root/'.github-deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with tempfile.NamedTemporaryFile(prefix='.incoming-', dir=root/'releases') as archive:
            size = 0
            while chunk := sys.stdin.buffer.read(1024*1024):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ValueError('Uploaded archive exceeds the size limit')
                archive.write(chunk)
            archive.flush()
            if digest(archive.name) != command[3]:
                raise ValueError('Uploaded archive checksum mismatch')
            deploy(root, Path(archive.name), command[1], command[2])


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(f'Deployment failed: {error}', file=sys.stderr)
        sys.exit(1)
