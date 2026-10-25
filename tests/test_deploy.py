"""Release integrity and atomic recovery without touching a live server."""
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

import pytest

from deploy.package import build_package
from deploy import receiver

REVISION = 'a'*40


def fixture(root):
    files = {'dirmap/app.py': '# application', 'requirements.lock': 'numpy==2.5.4\n',
             'web/dist/index.html': '<html>app</html>', 'web/dist/sw.js': '// worker',
             'deploy/receiver.py': '# receiver', 'deploy/run-server.sh': '# runner',
             'data/uploads.json': 'teaching data', 'data/projects/test/source.img': 'private map',
             'config.env': 'private operator configuration', '.env': 'private environment'}
    for name, body in files.items():
        path = root/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    return root


def test_release_round_trip_contains_only_verified_code_and_preserves_local_data(tmp_path):
    root = fixture(tmp_path/'source')
    archive = tmp_path/'release.tar.gz'
    manifest = build_package(root, archive, REVISION)
    target = tmp_path/'target'
    target.mkdir()
    assert receiver.extract_release(archive, target, REVISION) == manifest
    assert not (target/'data').exists() and not (target/'config.env').exists()
    assert not (target/'.env').exists()
    assert (root/'data/uploads.json').read_text() == 'teaching data'
    assert (target/'web/dist/index.html').read_text() == '<html>app</html>'


@pytest.mark.parametrize('name, kind', [('../escaped', 'file'), ('/tmp/escaped', 'file'),
    ('data/uploads.json', 'file'), ('dirmap/app.py', 'symlink'), ('web/dist/index.html', 'hardlink')])
def test_receiver_rejects_traversal_teaching_data_and_links(tmp_path, name, kind):
    archive = tmp_path/'bad.tar.gz'
    with tarfile.open(archive, 'w:gz') as output:
        entry = tarfile.TarInfo(name)
        if kind != 'file':
            entry.type = tarfile.SYMTYPE if kind == 'symlink' else tarfile.LNKTYPE
            entry.linkname = '/etc/passwd'
        output.addfile(entry, io.BytesIO())
    with pytest.raises(ValueError, match='unexpected'):
        receiver.extract_release(archive, tmp_path/'target', REVISION)
    assert not (tmp_path/'escaped').exists()


def test_receiver_detects_modified_bytes_and_wrong_commit(tmp_path):
    root = fixture(tmp_path/'source')
    archive = tmp_path/'release.tar.gz'
    build_package(root, archive, REVISION)
    wrong = tmp_path/'wrong'
    wrong.mkdir()
    with pytest.raises(ValueError, match='revision'):
        receiver.extract_release(archive, wrong, 'b'*40)
    changed = tmp_path/'changed.tar.gz'
    with tarfile.open(archive) as source, tarfile.open(changed, 'w:gz') as output:
        for entry in source:
            body = source.extractfile(entry).read()
            if entry.name == 'web/dist/index.html':
                body = b'modified frontend'
                entry.size = len(body)
            output.addfile(entry, io.BytesIO(body))
    target = tmp_path/'target'
    target.mkdir()
    with pytest.raises(ValueError, match='checksum'):
        receiver.extract_release(changed, target, REVISION)


@pytest.mark.parametrize('failure', ['restart', 'health'])
def test_failed_activation_restores_old_code_and_runtime_without_writing_data(tmp_path, monkeypatch, failure):
    old, new = tmp_path/'releases/old', tmp_path/'releases/new'
    old.mkdir(parents=True)
    new.mkdir()
    data = tmp_path/'data'
    data.mkdir()
    (data/'uploads.json').write_text('teacher edits')
    (tmp_path/'current').symlink_to(old)
    (old/'data').symlink_to(data)
    (new/'data').symlink_to(data)
    calls = []
    def restart():
        calls.append((tmp_path/'current').resolve())
        if failure == 'restart' and calls[-1] == new:
            raise RuntimeError('restart failed')
    monkeypatch.setattr(receiver, 'restart', restart)
    monkeypatch.setattr(receiver, 'wait_health', lambda *args, **kwargs: (tmp_path/'current').resolve() == old)
    with pytest.raises(RuntimeError):
        receiver.activate(tmp_path, new, REVISION)
    assert calls == [new, old]
    assert (tmp_path/'current').resolve() == old
    assert (data/'uploads.json').read_text() == 'teacher edits'


def test_successful_activation_requires_the_new_running_commit(tmp_path, monkeypatch):
    old, new = tmp_path/'releases/old', tmp_path/'releases/new'
    old.mkdir(parents=True)
    new.mkdir()
    (tmp_path/'current').symlink_to(old)
    checked = []
    monkeypatch.setattr(receiver, 'restart', lambda: None)
    def wait(port, revision, **kwargs):
        checked.append(revision)
        return revision == REVISION and (tmp_path/'current').resolve() == new
    monkeypatch.setattr(receiver, 'wait_health', wait)
    assert receiver.activate(tmp_path, new, REVISION) == old
    assert checked == [REVISION]


def test_retention_never_deletes_current_previous_or_shared_data(tmp_path):
    data = tmp_path/'data'
    data.mkdir()
    (data/'saved').write_text('keep')
    releases = tmp_path/'releases'
    releases.mkdir()
    paths = []
    for i in range(8):
        path = releases/f'20261025T00000{i}Z'
        path.mkdir()
        (path/'data').symlink_to(data)
        paths.append(path)
    (tmp_path/'current').symlink_to(paths[0])
    receiver.prune_releases(tmp_path, paths[1])
    assert len(list(releases.iterdir())) == 5
    assert paths[0].is_dir() and paths[1].is_dir()
    assert (data/'saved').read_text() == 'keep'


def test_complete_deployment_links_data_and_runtime_and_updates_receiver(tmp_path, monkeypatch):
    source = fixture(tmp_path/'source')
    archive = tmp_path/'release.tar.gz'
    build_package(source, archive, REVISION)
    root = tmp_path/'server'
    old = root/'releases/20261024T000000Z'
    old.mkdir(parents=True)
    (root/'current').symlink_to(old)
    (root/'data').mkdir()
    (root/'data/state').write_text('unchanged teaching data')
    (root/'venv').mkdir()
    (root/'bin').mkdir()
    monkeypatch.setattr(receiver, 'prepare_runtime', lambda *_: root/'venv')
    monkeypatch.setattr(receiver, 'restart', lambda: None)
    monkeypatch.setattr(receiver, 'wait_health', lambda *args, **kwargs: True)
    receiver.deploy(root, archive, REVISION, '123-1')
    active = (root/'current').resolve()
    assert active != old and old.is_dir()
    assert (active/'data').resolve() == root/'data'
    assert (active/'.venv').resolve() == root/'venv'
    assert (root/'data/state').read_text() == 'unchanged teaching data'
    assert json.loads((active/'deployment.json').read_text())['revision'] == REVISION
    assert (root/'bin/github-receiver.py').read_bytes() == (source/'deploy/receiver.py').read_bytes()
    assert not list((root/'releases').glob('.staging-*'))


def test_packaged_runtime_decodes_images_without_development_tools(tmp_path):
    import cv2
    import numpy as np
    workspace = Path(__file__).resolve().parents[1]
    root = fixture(tmp_path/'source')
    shutil.copytree(workspace/'dirmap', root/'dirmap', dirs_exist_ok=True, ignore=shutil.ignore_patterns('__pycache__'))
    cv2.imwrite(str(root/'map.png'), np.full((7, 11, 3), 255, np.uint8))
    archive = tmp_path/'release.tar.gz'
    build_package(root, archive, REVISION)
    target = tmp_path/'runtime'
    target.mkdir()
    receiver.extract_release(archive, target, REVISION)
    script = '''
import importlib.abc, pathlib, sys
class NoDevelopmentTools(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'tools' or fullname.startswith('tools.'):
            raise ModuleNotFoundError('Development tools are absent from the production artifact')
sys.meta_path.insert(0, NoDevelopmentTools())
from dirmap import images
assert pathlib.Path(images.__file__).resolve().is_relative_to(pathlib.Path.cwd())
image, info = images.read_map_image('map.png')
assert image.shape == (7, 11, 3) and info['encodedSize'] == [11, 7]
'''
    result = subprocess.run([sys.executable, '-B', '-c', script], cwd=target,
                            env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
