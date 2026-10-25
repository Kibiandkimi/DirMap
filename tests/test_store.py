"""Relocated workbenches must read their own imported originals."""
from pathlib import Path
import shutil

import pytest

from dirmap import store


@pytest.mark.parametrize('legacy_absolute_path', [True, False])
def test_uploaded_map_follows_copied_data_even_when_old_source_still_exists(tmp_path, monkeypatch, legacy_absolute_path):
    original_data = tmp_path/'original'/'data'
    monkeypatch.setattr(store, 'DATA', original_data)
    asset_id = 'upload-migration'
    source = store.asset_dir(asset_id)/'source.img'
    source.write_bytes(b'original map in the backup')
    manifest = [{'id': asset_id, 'name': 'Imported map', 'filename': 'map.jpg', 'role': 'map',
                 'path': str(source) if legacy_absolute_path else source.relative_to(original_data).as_posix(),
                 'sha256': store.file_hash(source)}]
    store.atomic_json(original_data/'uploads.json', manifest)
    copied_data = tmp_path/'new-machine'/'data'
    shutil.copytree(original_data, copied_data)
    source.write_bytes(b'unrelated newer map left at the old location')
    monkeypatch.setattr(store, 'DATA', copied_data)

    asset = store.get_asset(asset_id)
    assert Path(asset['path']) == copied_data/'projects'/asset_id/'source.img'
    assert store.file_hash(asset['path']) == manifest[0]['sha256']
    assert store.read_json(copied_data/'uploads.json') == manifest


def test_missing_copied_source_does_not_fall_back_to_original_workbench(tmp_path, monkeypatch):
    source = tmp_path/'old-source.img'
    source.write_bytes(b'not part of the selected workbench')
    copied_data = tmp_path/'restored-data'
    monkeypatch.setattr(store, 'DATA', copied_data)
    store.atomic_json(copied_data/'uploads.json', [{'id': 'upload-incomplete', 'path': str(source)}])

    asset = store.get_asset('upload-incomplete')
    assert Path(asset['path']) == copied_data/'projects'/'upload-incomplete'/'source.img'
    assert not Path(asset['path']).exists()
