import copy
import io

import numpy as np
from PIL import Image

from dirmap import contours, elevation, service, store, travel, travel_model
from dirmap.elevation_render import relief
from test_api import client, wait_job
from test_elevation_api import register, upload
from dirmap.geometry import Frame
from test_travel_api import contribute


def add_curves(state):
    state['contours'] = [{'id': 'contour-'+letter*12, 'pixels': [[3, y], [20, y], [37, y]],
                         'heightM': None, 'closed': False, 'lengthPx': 34}
                        for letter, y in [('a', 8), ('b', 18), ('c', 30)]]
    return state


def test_processing_automatically_contributes_unlabelled_lines_and_recovers_without_extra_weight(client, monkeypatch):
    owner = register(client, 'automatic-lines', True)
    state = add_curves(store.get_state('szu-1')); store.save_state('szu-1', state)
    def processed(job, progress):
        return store.get_state(job['assetId'])
    monkeypatch.setattr(service, '_execute_job', processed)
    job = {'kind': 'process', 'assetId': 'szu-1', 'id': 'automatic-operation', 'workspace': store.LEGACY}
    result = service.execute_job(job, lambda *args: None)
    automatic = result['automaticContours']
    assert automatic['used'] == 3 and automatic['pending'] == 0
    source = elevation.catalogue(); record = source['tracks'][0]
    assert record['ownerId'] == owner['id'] and record['heightReference'] == 'shape'
    assert elevation.public_status()['needsRebuild'] is False
    assert elevation.models()['regions'][0]['supportedCells'] == 0
    service.execute_job(job, lambda *args: None)
    assert elevation.catalogue() == source
    # A future reference automatically supplies levels; no per-line review.
    xy = np.array([[x, y] for x in (10, 20, 30) for y in range(0, 41)])
    upload(client, np.column_stack((Frame().to_lnglat(xy), 20+xy[:, 1]*.1)))
    current = elevation.models()['regions'][0]
    assert current['supportedCells'] > 0
    alignment = next(t for t in current['alignment']['tracks'] if t['id'] == record['id'])
    assert alignment['anchoredContours'] == 3
    elevation.change_track(record['id'], owner, elevation.catalogue()['version'], enabled=False)
    service.execute_job(job, lambda *args: None)
    assert not next(t for t in elevation.catalogue()['tracks'] if t['id'] == record['id'])['enabled']


def test_track_view_scopes_points_surface_and_learning_without_changing_contributions(client):
    register(client, 'contribution-view', True)
    identifiers = [contribute(client, offset)['added'][0] for offset in range(3)]
    before = elevation.catalogue()
    region = elevation.models()['regions'][0]
    all_grid = client.get('/api/elevation/regions/'+region['id']).json()
    response = client.get('/api/elevation/regions/'+region['id'], params={'track': identifiers[0]})
    assert response.status_code == 200, response.text
    selected = response.json()
    assert selected['trackIds'] == [identifiers[0]]
    assert 0 < len(selected['observations']) < len(all_grid['observations'])
    assert selected['supportedCells'] <= all_grid['supportedCells']
    assert all(a is None or a == b for a, b in zip(selected['height'], all_grid['height']))
    detail = client.get('/api/elevation/tracks/'+identifiers[0]).json()
    assert detail['track']['id'] == identifiers[0] and detail['samples'] and detail['speedWindows']
    assert detail['distanceM'] > 0 and detail['speedLearning']['observations'] > 0
    diagnostics = client.get('/api/elevation/regions/'+region['id']+'/travel', params={'track': identifiers[0]}).json()
    assert [t['id'] for t in diagnostics['tracks']] == [identifiers[0]]
    assert len(diagnostics['profiles']['foot']['gradeCurve']) == 41
    assert all(identifiers[0] in c['trackIds'] and c['tracks'] >= 3 for c in diagnostics['profiles']['foot']['corridors'])
    assert all(t['trackId'] == identifiers[0] for t in diagnostics['profiles']['foot']['episodes'])
    assert elevation.catalogue() == before
    assert client.get('/api/elevation/regions/'+region['id'], params={'track': 'missing'}).status_code == 422


def test_contours_only_overlay_has_transparent_interiors_and_never_fills_unsupported_ground(client):
    grid = np.tile(np.arange(24.)[:, None], (1, 30)); grid[:4, :5] = np.nan
    full = relief(grid, 10)
    lines = relief(grid, 10, style='contours')
    assert full.shape == lines.shape
    assert 0 < np.count_nonzero(lines[..., 3]) < np.count_nonzero(full[..., 3]) / 2
    assert not lines[full[..., 3] == 0, 3].any()
    register(client, 'contour-overlay', True)
    contribute(client)
    region = elevation.models()['regions'][0]
    name = region['artifact'].split('/')[-1].removesuffix('.npz')
    response = client.get(f'/api/elevation/artifacts/{name}/relief.png?style=contours')
    assert response.status_code == 200
    image = np.asarray(Image.open(io.BytesIO(response.content)))
    assert image.shape[2] == 4 and (image[..., 3] == 0).any()
    assert client.get(f'/api/elevation/artifacts/{name}/relief.png?style=invalid').status_code == 422


def test_duplicate_map_sources_keep_one_constraint_until_the_last_source_is_removed(client):
    owner = register(client, 'map-sources', True)
    state = add_curves(store.get_state('szu-1'))
    first = contours.sync_automatic(state, owner)['added'][0]
    other = {**copy.deepcopy(state), 'assetId': 'copied-sheet'}
    repeat = contours.sync_automatic(other, owner)
    assert repeat['added'] == [] and repeat['duplicates'][0]['id'] == first
    assert len(elevation.catalogue()['tracks']) == 1
    contours.remove_automatic_source('szu-1')
    assert elevation.catalogue()['tracks'][0]['enabled']
    contours.remove_automatic_source('copied-sheet')
    assert elevation.catalogue()['tracks'] == []
    assert not (elevation.root()/'tracks'/(first+'.npz')).exists()
