"""A published layered course keeps target levels and rejects ambiguous GPS."""
import time

from test_api import client, request as base_request, wait_job
from dirmap import store
from dirmap.geometry import Frame, Registration


def setup_layers(client, connected):
    state = store.get_state('szu-1')
    control = {key: value for key, value in state['controls'][0].items()
               if key in ('id', 'label', 'pixel', 'radiusPx', 'required', 'state', 'serviceBaseSec', 'servicePerPersonSec')}
    control.update(pixel=[20, 30], surfaceId='bridge', state='accepted')
    response = client.patch('/api/maps/szu-1', json={'version': state['version'], 'controls': [control],
        'surfaces': [{'id': 'bridge', 'label': '桥上', 'level': 1, 'accepted': True}],
        'edits': [{'kind': 'open', 'surfaceId': 'bridge', 'pixels': [[20, 4], [20, 36]], 'radiusPx': 2, 'cost': 1.7, 'separateGround': True}],
        'portals': [{'id': 'entrance', 'label': '北侧入口', 'fromSurface': 'ground', 'toSurface': 'bridge',
                     'pixel': [20, 4], 'radiusPx': 5, 'lengthM': 3, 'costFactor': 1.7, 'accepted': connected}]})
    assert response.status_code == 200, response.text
    return response.json()


def plan(client, state, **extra):
    request = base_request(version=state['version'])
    request.update(extra)
    submitted = client.post('/api/plans', json=request)
    assert submitted.status_code == 200, submitted.text
    job = wait_job(client, submitted.json()['id'])
    assert job['state'] == 'succeeded', job
    return job['result']


def test_unconfirmed_entrance_does_not_connect_an_upper_control(client):
    state = setup_layers(client, False)
    result = plan(client, state)
    assert result['status'] == 'NEEDS_REVIEW'
    assert any(item['id'] == 'c1' for item in result['unresolved'])


def test_published_course_retains_levels_and_navigation_requires_a_layer_at_overlap(client):
    state = setup_layers(client, True)
    result = plan(client, state)
    assert len(result['routes']) == 1
    assert result['targetSurfaces']['c1'] == 'bridge'
    transitions = result['routes'][0]['transitions']
    assert [(p['fromSurface'], p['toSurface']) for p in transitions] == [('ground', 'bridge'), ('bridge', 'ground')]
    assert all(p['label'] == '北侧入口' and p['lengthM'] == 3 for p in transitions)
    ambiguous = plan(client, state, startPixel=[20, 20])
    assert ambiguous['status'] == 'NEEDS_REVIEW' and any('层级' in p['message'] for p in ambiguous['unresolved'])
    explicit = plan(client, state, startPixel=[20, 20], startSurfaceId='bridge')
    assert explicit['routes'][0]['surfaceSections'][0]['surfaceId'] == 'bridge'
    reviewed = client.post('/api/maps/szu-1/review', json={'version': state['version'], 'registrationChecked': True,
        'controlsChecked': True, 'passagesChecked': True})
    assert reviewed.status_code == 200, reviewed.text
    confirmed = client.post('/api/plans/confirm', json={'assetId': 'szu-1', 'version': state['version'], 'planId': result['id']})
    outcome = wait_job(client, confirmed.json()['id'])
    assert outcome['state'] == 'succeeded', outcome
    release = client.post('/api/releases', json={'assetId': 'szu-1', 'planId': outcome['result']['id']})
    assert release.status_code == 200, release.text
    identifier = release.json()['id']
    pixel = [20, 20]
    world, _ = Registration.from_dict(state['registration']).forward([pixel])
    position = Frame().to_lnglat(world[0]).tolist()
    payload = {'teamId': 'team1', 'lnglat': position, 'accuracyM': 3, 'timestampMs': time.time()*1000,
               'completedControlIds': []}
    response = client.post('/api/releases/'+identifier+'/navigate', json=payload)
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'NEEDS_REVIEW'
    response = client.post('/api/releases/'+identifier+'/navigate', json={**payload, 'surfaceId': 'bridge'})
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'READY'
    assert response.json()['route']['surfaceSections'][0]['surfaceId'] == 'bridge'
    portals = [{**p, 'accepted': False} for p in state['portals']]
    changed = client.patch('/api/maps/szu-1', json={'version': state['version'], 'portals': portals})
    assert changed.status_code == 200
    assert client.get('/api/releases/'+identifier).json()['superseded'] is True


def test_unconfirmed_surface_does_not_change_existing_ground(client):
    state = store.get_state('szu-1')
    response = client.patch('/api/maps/szu-1', json={'version': state['version'],
        'surfaces': [{'id': 'draft', 'label': '未核验通道', 'level': 1}],
        'edits': [{'kind': 'open', 'surfaceId': 'draft', 'pixels': [[20, 0], [20, 40]], 'radiusPx': 3, 'separateGround': True}]})
    assert response.status_code == 200, response.text
    result = plan(client, response.json())
    assert result['routes'] and not result['routes'][0]['transitions']
    assert result['surfaces'] == [{'id': 'ground', 'label': '地面', 'level': 0, 'accepted': True}]
