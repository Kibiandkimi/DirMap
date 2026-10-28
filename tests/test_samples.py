"""Public starter data stays separate from user work and remains space-bounded."""
import base64
import hashlib
import io
import json
from pathlib import Path
import zipfile

import pytest

from dirmap import accounts, samples, store
from test_accounts import public_sample_bundle


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path/'server')
    return public_sample_bundle(tmp_path)


def test_a_private_edit_cannot_be_packaged_as_a_public_sample(bundle):
    original = samples.BUNDLE.read_bytes()
    path = bundle/'projects/szu-1/state.json'
    state = store.read_json(path)
    state['controls'][0].update(label='private edit', labelSource='manual', manualFields=['label'])
    store.atomic_json(path, state)
    with pytest.raises(ValueError, match='teacher edits'):
        samples.write_bundle(bundle, samples.BUNDLE)
    assert samples.BUNDLE.read_bytes() == original


@pytest.mark.parametrize('corruption', ['checksum', 'path'])
def test_invalid_public_bundles_never_publish_a_partial_cache(bundle, corruption):
    document = store.read_json(samples.BUNDLE)
    if corruption == 'checksum':
        document['files'][next(iter(document['files']))] = '0'*64
    else:
        content = b'not a starter map'
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as archive:
            archive.writestr('../outside', content)
        document['files'] = {'../outside': hashlib.sha256(content).hexdigest()}
        document['zipBase64'] = base64.b64encode(output.getvalue()).decode()
    samples.BUNDLE.write_text(json.dumps(document))
    with pytest.raises(ValueError):
        samples.source_root()
    assert not list((store.DATA/'shared-samples').glob('*/ready.json'))
    assert not (store.DATA/'outside').exists()


def test_pruning_old_public_snapshots_keeps_the_accounts_hard_links(bundle):
    first_snapshot = samples.source_root()
    accounts.seed_workspace('a'*16)
    retained = store.workspace_root('a'*16)/'projects/szu-1/terrain.npz'
    original = retained.read_bytes()
    assert retained.stat().st_ino == (first_snapshot/'projects/szu-1/terrain.npz').stat().st_ino
    for revision, workspace in [(2, 'b'*16), (3, 'c'*16)]:
        path = bundle/'projects/szu-1/state.json'; state = store.read_json(path)
        state['statistics']['publicRevision'] = revision
        store.atomic_json(path, state); samples.write_bundle(bundle, samples.BUNDLE)
        accounts.seed_workspace(workspace)
    snapshots = [p for p in (store.DATA/'shared-samples').iterdir() if p.is_dir()]
    assert len(snapshots) == 2 and not first_snapshot.exists()
    assert retained.read_bytes() == original


def test_a_cached_snapshot_is_revalidated_after_the_interpretation_changes(bundle, monkeypatch):
    samples.source_root()
    monkeypatch.setattr(samples, 'TERRAIN_SCHEMA', 'new-interpretation')
    with pytest.raises(ValueError, match='rebuilt'):
        samples.source_root()
