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


def test_history_retention_keeps_newest_recent_referenced_and_live_records(tmp_path, monkeypatch):
    import os
    import time
    monkeypatch.setattr(store, 'DATA', tmp_path/'data')
    directory = store.asset_dir('map')
    now = time.time()
    def record(path, age_days, value=None):
        store.atomic_json(path, value or {})
        os.utime(path, (now-age_days*86400,)*2)
        return path.name
    store.save_state('map', {'assetId': 'map', 'version': 3}, operation_id='live', result={'version': 3})
    reference = lambda identifier: {'teamRecommendation': {'comparisonId': identifier}}
    names = {
        'newest plans': [record(directory/'plan-a.json', 1), record(directory/'plan-b.json', 2)],
        'recent plan': [record(directory/'plan-c.json', 10, reference('referenced'))],
        'old plan': [record(directory/'plan-d.json', 40, reference('orphaned'))],
        'newest comparisons': [record(directory/'comparison-a.json', 1), record(directory/'comparison-b.json', 2)],
        'recent comparison': [record(directory/'comparison-c.json', 5)],
        'referenced comparison': [record(directory/'comparison-referenced.json', 60)],
        'orphaned comparison': [record(directory/'comparison-orphaned.json', 61)],
        'live receipt': [record(store.DATA/'jobs'/'results'/'live.json', 10, {'version': 3})],
        'old receipt': [record(store.DATA/'jobs'/'results'/'superseded.json', 10)],
        'staged receipt': [record(store.DATA/'jobs'/'results'/'staged.json', .01)],
    }
    release = store.DATA/'releases'/'course'/'release.json'
    record(release, 400)
    removed = store.prune_history(keep_per_asset=2, keep_sec=30*86400, receipt_age_sec=86400, now=now)
    assert sorted(path.name for path in removed) == sorted(
        names['old plan']+names['orphaned comparison']+names['old receipt'])
    assert release.exists() and store.operation_result('map', 'live') == {'version': 3}
    assert store.prune_history(keep_per_asset=2, now=now) == []
