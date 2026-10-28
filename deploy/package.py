#!/usr/bin/env python3
"""Build a small, checksummed release without teaching data or credentials."""
import argparse
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import re
import tarfile
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from deploy.receiver import allowed_file, digest


def build_package(root, output, revision):
    if not re.fullmatch('[0-9a-f]{40}', revision):
        raise ValueError('A full Git commit SHA is required')
    candidates = [*root.glob('*.jpg'), *root.glob('*.jpeg'), *root.glob('*.png'), *root.glob('*.pdf'),
                  *root.joinpath('dirmap').glob('*'), *root.joinpath('web/dist').rglob('*'),
                  *(root/name for name in ('requirements.lock', 'requirements.txt', 'package.json', 'package-lock.json',
                                           'deploy/receiver.py', 'deploy/run-server.sh'))]
    files = {p.relative_to(root).as_posix(): p for p in candidates
             if p.is_file() and not p.is_symlink() and allowed_file(p.relative_to(root).as_posix())}
    required = {'dirmap/app.py', 'dirmap/public-samples.json', 'web/dist/index.html', 'web/dist/sw.js', 'requirements.lock',
                'deploy/receiver.py', 'deploy/run-server.sh'}
    if not required.issubset(files):
        raise ValueError('Build the application and public samples before packaging; required release files are missing')
    manifest = {'schema': 1, 'revision': revision, 'createdAt': datetime.now(timezone.utc).isoformat(),
                'files': {name: digest(files[name]) for name in sorted(files)}}
    output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output, 'w:gz') as archive:
        for name, path in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size = path.stat().st_size
            info.mode = 0o644
            with path.open('rb') as source:
                archive.addfile(info, source)
        body = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode()
        info = tarfile.TarInfo('deployment.json')
        info.size, info.mode = len(body), 0o644
        archive.addfile(info, io.BytesIO(body))
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    manifest = build_package(ROOT, args.output, args.revision)
    print(json.dumps({'revision': args.revision, 'files': len(manifest['files']),
                      'bytes': args.output.stat().st_size, 'sha256': digest(args.output)}))
