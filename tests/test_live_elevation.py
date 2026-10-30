import io
import time

import numpy as np
from PIL import Image
import pytest

from dirmap import elevation, live_elevation, service, store
from dirmap.geometry import Frame, mercator
from test_api import client, request as plan_request, wait_job
from test_elevation import crossing_walks, kml, plane
from test_elevation_api import register, upload


def course(client, collect=True):
    """A reviewed, published course whose map covers the synthetic walks."""
    state = store.get_state('szu-1')
    corners = mercator(Frame().to_lnglat([[-100, -100], [400, 400]]))
    state['extent'] = [*corners.min(axis=0).tolist(), *corners.max(axis=0).tolist()]
    state['controls'][0]['state'] = 'accepted'
    service.project_controls(state); store.save_state('szu-1', state)
    assert client.post('/api/maps/szu-1/review', json={'version': 1, 'registrationChecked': True,
        'controlsChecked': True, 'passagesChecked': True}).status_code == 200
    plan = wait_job(client, client.post('/api/plans', json={**plan_request(), 'timeLimitSec': 1}).json()['id'])['result']
    published = client.post('/api/releases', json={'assetId': 'szu-1', 'planId': plan['id'], 'title': '实时地形课', 'collectHeights': collect})
    assert published.status_code == 200, published.text
    return published.json()['id']


def samples(walk, start, offset=0.):
    seconds = walk['times']-walk['times'][0]
    return [{'t': (start+s)*1000, 'lng': p[0], 'lat': p[1], 'h': p[2]+offset, 'hacc': 5, 'vacc': 8}
            for p, s in zip(walk['points'], seconds)]


def send(client, release_id, device, values):
    for start in range(0, len(values), live_elevation.MAX_BATCH):
        response = client.post(f'/api/releases/{release_id}/elevation-samples', json={'deviceId': device, 'samples': values[start:start+live_elevation.MAX_BATCH]})
        assert response.status_code == 200, response.text
    return response.json()


def test_phones_build_a_temporary_terrain_that_routes_and_the_shared_terrain_do_not_use(client):
    register(client, 'live-teacher', True)
    release_id = course(client)
    client.cookies.clear()  # phones carry only the course link
    walks, ground = crossing_walks()
    now = time.time()
    receipt = send(client, release_id, 'phone-steady-0001', samples(walks[0], now-3000))
    assert receipt['enabled'] and receipt['accepted'] > 0
    send(client, release_id, 'phone-drifting-01', samples(walks[1], now-2500, 4.))
    live = client.get(f'/api/releases/{release_id}/live-elevation').json()
    assert live['devices'] == 2 and live['models'] and not live['stale']
    model = live['models'][0]
    assert model['livePhones'] == 2 and model['sharedTracks'] == 0
    # Two phones that only cross each other still give one consistent surface.
    image = client.get(f"/api/releases/{release_id}/live-elevation/{model['artifact']}.png")
    assert image.status_code == 200 and image.headers['cache-control'].endswith('immutable')
    with Image.open(io.BytesIO(image.content)) as relief:
        assert relief.mode == 'RGBA' and np.asarray(relief)[..., 3].max() == 255
    path = live_elevation.artifact_path(release_id, model['artifact'])
    with np.load(path) as arrays:
        heights = elevation.HeightModel(model, {'height': arrays['height']}).sample(Frame().to_lnglat(np.mgrid[40:260:20, 40:260:20].reshape(2, -1).T))
    xy = np.mgrid[40:260:20, 40:260:20].reshape(2, -1).T.astype(float)
    error = heights-ground(xy)
    assert np.isfinite(error).all() and np.percentile(np.abs(error-np.median(error)), 95) < .6
    # Repeating a batch adds nothing; samples off the course's area, from the
    # future or without usable accuracy are dropped.
    again = send(client, release_id, 'phone-steady-0001', samples(walks[0], now-3000)[:50])
    assert again['accepted'] == 0
    far = [{'t': now*1000, 'lng': 113.99, 'lat': 22.6, 'h': 10, 'hacc': 5}, {'t': (now+3600)*1000, 'lng': 113.9304, 'lat': 22.535, 'h': 10, 'hacc': 5},
           {'t': now*1000, 'lng': 113.9304, 'lat': 22.535, 'h': 10, 'hacc': 60}, {'t': now*1000, 'lng': 113.9304, 'lat': 22.535, 'h': 10, 'hacc': 5, 'vacc': 90}]
    assert send(client, release_id, 'phone-steady-0001', far)['accepted'] == 0
    assert client.post(f'/api/releases/{release_id}/elevation-samples', json={'deviceId': 'short', 'samples': []}).status_code == 422
    # The shared terrain and the course's routes are untouched.
    assert client.get('/api/elevation').json()['tracks'] == []
    assert client.get(f'/api/releases/{release_id}').json()['plan']['elevationSnapshot']['models'] == []


def test_only_the_courses_teacher_controls_sampling_and_saves_the_walks_as_their_tracks(client):
    teacher = register(client, 'live-owner', True)
    release_id = course(client, collect=False)
    walks, _ = crossing_walks()
    now = time.time()
    assert send(client, release_id, 'phone-steady-0001', samples(walks[0], now-3000)) == {'enabled': False, 'accepted': 0}
    assert client.post(f'/api/releases/{release_id}/live-elevation', json={'action': 'enable'}).json()['enabled'] is True
    send(client, release_id, 'phone-steady-0001', samples(walks[0], now-3000))
    send(client, release_id, 'phone-drifting-01', samples(walks[1], now-2500, 4.))
    teacher_cookies = dict(client.cookies)
    client.cookies.clear()
    assert client.post(f'/api/releases/{release_id}/live-elevation', json={'action': 'clear'}).status_code == 401
    register(client, 'live-stranger')
    assert client.post(f'/api/releases/{release_id}/live-elevation', json={'action': 'adopt'}).status_code in (403, 409)
    client.cookies.clear(); client.cookies.update(teacher_cookies)
    listed = client.get('/api/releases?assetId=szu-1').json()[0]['liveElevation']
    assert listed['enabled'] and listed['devices'] == 2 and listed['expiresAt'] > now
    saved = client.post(f'/api/releases/{release_id}/live-elevation', json={'action': 'adopt'})
    assert saved.status_code == 200, saved.text
    assert len(saved.json()['adopted']['added']) == 2 and saved.json()['adoptedCurrent']
    job = wait_job(client, saved.json()['job']['id']); assert job['state'] == 'succeeded', job
    shared = client.get('/api/elevation').json()
    assert {t['ownerId'] for t in shared['tracks']} == {teacher['id']} and len(shared['regions']) == 1
    assert all(t['name'].startswith('实时地形课 · 实时采样') for t in shared['tracks'])
    # Saving twice does not duplicate a walk; clearing removes the samples.
    again = client.post(f'/api/releases/{release_id}/live-elevation', json={'action': 'adopt'}).json()['adopted']
    assert again['added'] == [] and len(again['duplicates']) == 2
    cleared = client.post(f'/api/releases/{release_id}/live-elevation', json={'action': 'clear'}).json()
    assert cleared['samples'] == 0 and not live_elevation.live_dir(release_id).exists()
    assert client.post(f'/api/releases/{release_id}/live-elevation', json={'action': 'disable'}).json()['enabled'] is False


def test_the_temporary_terrain_starts_from_shared_walks_and_expires_after_two_days(client):
    register(client, 'live-expiry', True)
    upload(client, plane())
    release_id = course(client)
    walks, _ = crossing_walks()
    now = time.time()
    send(client, release_id, 'phone-steady-0001', samples(walks[0], now-3000))
    live = client.get(f'/api/releases/{release_id}/live-elevation').json()
    assert live['models'][0]['sharedTracks'] == 1 and live['models'][0]['livePhones'] == 1
    # A burst of new samples rebuilds lazily, not more often than every 20 s.
    send(client, release_id, 'phone-drifting-01', samples(walks[1], now-2500))
    assert live_elevation.current({'id': release_id, **client.get(f'/api/releases/{release_id}').json()}, now=live['builtAt']+5)['stale']
    assert live_elevation.prune(now=now+live_elevation.RETAIN_SEC-60) == []
    assert live_elevation.prune(now=now+live_elevation.RETAIN_SEC+60) == [release_id]
    after = client.get(f'/api/releases/{release_id}/live-elevation').json()
    assert after['samples'] == 0 and after['models'] == [] and after['enabled'] is True


def test_shared_terrain_relief_images_are_named_by_content(client):
    register(client, 'relief-owner', True)
    upload(client, plane())
    region = client.get('/api/elevation').json()['regions'][0]
    name = region['artifact'].removeprefix('models/').removesuffix('.npz')
    image = client.get(f'/api/elevation/artifacts/{name}/relief.png')
    assert image.status_code == 200 and image.headers['cache-control'].endswith('immutable')
    with Image.open(io.BytesIO(image.content)) as relief:
        assert relief.size[0] >= region['columns'] and relief.mode == 'RGBA'
    assert client.get('/api/elevation/artifacts/'+'0'*40+'/relief.png').status_code == 404
    assert client.get('/api/elevation/artifacts/not-a-model/relief.png').status_code == 404
