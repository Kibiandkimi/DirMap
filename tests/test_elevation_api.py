import io
import time

import numpy as np
from PIL import Image

from dirmap import elevation, service, store
from test_api import automatic_request, choice_request, choose_plan, client, request as plan_request, wait_job
from test_elevation import kml, plane


def register(client, username, claim=False):
    response = client.post('/api/auth/register', json={'username': username, 'password': 'terrain-test-password', 'claimOriginal': claim})
    assert response.status_code == 200, response.text
    return response.json()['user']


def upload(client, points, name='walk.kml'):
    response = client.post('/api/elevation/tracks', files=[('files', (name, kml(points, gx=True), 'application/vnd.google-earth.kml+xml'))])
    assert response.status_code == 200, response.text
    receipt = response.json()
    if receipt['job']:
        job = wait_job(client, receipt['job']['id'])
        assert job['state'] == 'succeeded', job
    return receipt


def test_shared_terrain_is_public_but_other_contributors_cannot_edit_or_claim_a_track(client):
    assert client.get('/api/elevation').status_code == 200
    assert client.post('/api/elevation/rebuild').status_code == 401
    alice = register(client, 'terrain-alice', True)
    first = upload(client, plane())
    identifier = first['added'][0]
    register(client, 'terrain-bob')
    shared = client.get('/api/elevation').json()
    assert shared['tracks'][0]['ownerId'] == alice['id'] and len(shared['regions']) == 1
    assert client.patch('/api/elevation/tracks/'+identifier, json={'version': shared['version'], 'enabled': False}).status_code == 403
    assert client.request('DELETE', '/api/elevation/tracks/'+identifier, json={'version': shared['version']}).status_code == 403
    repeated = upload(client, plane(), 'different-filename.kml')
    assert not repeated['added'] and repeated['duplicates'][0]['ownerName'] == alice['username']
    assert repeated['version'] == shared['version']
    client.cookies.clear()
    region = client.get('/api/elevation/regions/'+shared['regions'][0]['id']).json()
    assert region['supportedCells'] > 0 and None in region['height']
    exported = client.get('/api/elevation/regions/'+region['id']+'/height.tif')
    assert exported.status_code == 200
    with Image.open(io.BytesIO(exported.content)) as raster:
        assert raster.mode == 'F' and raster.size == (region['columns'], region['rows'])
        assert 4326 in raster.tag_v2[34735] and raster.tag_v2[42113] == 'nan'
        np.testing.assert_allclose(np.asarray(raster), np.flipud(np.array(region['height'], float).reshape(region['rows'], region['columns'])), atol=.01, equal_nan=True)
    assert client.patch('/api/elevation/tracks/'+identifier, json={'version': shared['version'], 'enabled': False}).status_code == 401


def reviewed(client):
    state = store.get_state('szu-1')
    state['controls'][0]['state'] = 'accepted'
    service.project_controls(state); store.save_state('szu-1', state)
    assert client.post('/api/maps/szu-1/review', json={'version': 1, 'registrationChecked': True,
        'controlsChecked': True, 'passagesChecked': True}).status_code == 200


def test_new_terrain_updates_draft_costs_but_a_published_course_keeps_its_height_snapshot(client):
    register(client, 'terrain-teacher', True)
    upload(client, plane())
    reviewed(client)
    def plan():
        response = client.post('/api/plans', json={**plan_request(), 'timeLimitSec': 2, 'useElevation': True})
        job = wait_job(client, response.json()['id']); assert job['state'] == 'succeeded', job
        return job['result']
    original = plan()
    assert original['elevationSnapshot']['models'] and original['routes'][0]['elevation']['ascentM'] > 0
    published = client.post('/api/releases', json={'assetId': 'szu-1', 'planId': original['id'], 'title': 'Slope-aware course'})
    assert published.status_code == 200, published.text
    release_id = published.json()['id']
    old_revision = original['elevationSnapshot']['revision']
    upload(client, plane(5, 16), 'another-walk.kml')
    assert elevation.signature(store.get_state('szu-1')) != old_revision
    # A draft keeps the heights it was planned with, so it stays publishable
    # while that snapshot exists, and is refused once the snapshot is gone.
    later = client.post('/api/releases', json={'assetId': 'szu-1', 'planId': original['id'], 'title': 'Planned before the new walk'})
    assert later.status_code == 200, later.text
    assert client.get('/api/releases/'+later.json()['id']).json()['plan']['elevationSnapshot']['revision'] == old_revision
    artifact = elevation.root()/original['elevationSnapshot']['models'][0]['artifact']
    pinned = artifact.read_bytes(); artifact.unlink()
    refused = client.post('/api/releases', json={'assetId': 'szu-1', 'planId': original['id'], 'title': 'Snapshot cleaned'})
    assert refused.status_code == 422 and '快照' in refused.json()['detail']
    artifact.write_bytes(pinned)
    new = plan()
    assert new['graphHash'] != original['graphHash'] and new['baseGraphHash'] == original['baseGraphHash']
    release = client.get('/api/releases/'+release_id).json()
    assert release['superseded'] is False and release['plan']['elevationSnapshot']['revision'] == old_revision
    position = original['routes'][0]['geometry'][0]
    rejoin = client.post('/api/releases/'+release_id+'/navigate', json={'teamId': 'team1', 'lnglat': position,
        'accuracyM': 6, 'timestampMs': time.time()*1000, 'completedControlIds': []})
    assert rejoin.status_code == 200, rejoin.text
    assert rejoin.json()['graphHash'] == original['graphHash']


def test_a_course_published_before_shared_terrain_keeps_rerouting_on_flat_costs(client):
    register(client, 'terrain-legacy', True)
    reviewed(client)
    response = client.post('/api/plans', json={**plan_request(), 'timeLimitSec': 2})
    plan = wait_job(client, response.json()['id'])['result']
    published = client.post('/api/releases', json={'assetId': 'szu-1', 'planId': plan['id'], 'title': 'Before heights'})
    release_id = published.json()['id']
    # The format written before shared heights existed.
    path = store.DATA/'releases'/release_id/'release.json'
    legacy = store.read_json(path)
    for key in ('elevationSnapshot', 'baseGraphHash'):
        legacy['plan'].pop(key)
    store.atomic_json(path, legacy)
    upload(client, plane())
    assert elevation.relevant(store.get_state('szu-1'))
    release = client.get('/api/releases/'+release_id).json()
    assert release['superseded'] is False
    rejoin = client.post('/api/releases/'+release_id+'/navigate', json={'teamId': 'team1', 'lnglat': plan['routes'][0]['geometry'][0],
        'accuracyM': 6, 'timestampMs': time.time()*1000, 'completedControlIds': []})
    assert rejoin.status_code == 200, rejoin.text
    assert rejoin.json()['graphHash'] == plan['graphHash'] and 'elevation' not in rejoin.json()['route']


def test_choosing_a_compared_team_count_keeps_flat_costs_when_slopes_were_off(client):
    register(client, 'terrain-teams', True)
    upload(client, plane())
    job = client.post('/api/plans/auto-teams', json=automatic_request(useElevation=False)).json()
    compared = wait_job(client, job['id'])
    assert compared['state'] == 'succeeded', compared
    compared = compared['result']
    assert compared['request']['useElevation'] is False and not compared['elevationSnapshot']['enabled']
    selected = choose_plan(client, choice_request(compared, 2))
    assert selected['request']['useElevation'] is False and selected['graphHash'] == compared['graphHash']
    assert 'elevation' not in selected['routes'][0]
