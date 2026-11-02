from datetime import datetime, timedelta, timezone
import re
import os
import time

import numpy as np
import pytest

from dirmap import elevation, store, travel
from dirmap.geometry import Frame
from test_api import client, request as plan_request, automatic_request, choose_plan, choice_request, wait_job
from test_elevation_api import register, reviewed


def timed_kml(offset=0, pace=1.):
    xy = np.array([[x, y] for y in (5, 65, 125) for x in range(0, 121, 2)])
    points = np.column_stack((Frame().to_lnglat(xy), 20+2*np.sin(xy[:, 0]/30)+offset))
    when, coordinates = [], []
    t = datetime(2026, 10, 1, tzinfo=timezone.utc)+timedelta(days=offset)
    for point, (x, _) in zip(points, xy):
        when.append(f'<when>{t.isoformat()}</when>')
        coordinates.append(f'<gx:coord>{point[0]} {point[1]} {point[2]}</gx:coord>')
        t += timedelta(seconds=(2.25 if x >= 60 else 1.5)/pace)
    return ('<kml xmlns:gx="http://www.google.com/kml/ext/2.2"><Document><name>Timed walk</name>'
            '<Placemark><gx:Track>'+''.join(when+coordinates)+'</gx:Track></Placemark></Document></kml>').encode()


def contribute(client, offset=0):
    response = client.post('/api/elevation/tracks', files=[('files', (f'walk-{offset}.kml', timed_kml(offset)))])
    assert response.status_code == 200, response.text
    receipt = response.json()
    if receipt['job']:
        job = wait_job(client, receipt['job']['id'])
        assert job['state'] == 'succeeded', job
    return receipt


def test_timed_export_enriches_owners_untimed_record_without_duplicate_weight(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    owner = {'id': 'owner', 'username': 'Owner'}
    content = timed_kml()
    untimed = re.sub(br'<when>.*?</when>', b'', content)
    first = elevation.add_tracks([('line.kml', untimed)], owner)
    other = elevation.add_tracks([('clock.kml', content)], {'id': 'other', 'username': 'Other'})
    assert not other['timingUpgraded']
    enhanced = elevation.add_tracks([('clock.kml', content)], owner)
    assert enhanced['timingUpgraded'] == first['added'] and not enhanced['added']
    assert enhanced['version'] > first['version'] and len(elevation.catalogue()['tracks']) == 1
    repeated = elevation.add_tracks([('again.kml', content)], owner)
    assert not repeated['timingUpgraded'] and repeated['version'] == enhanced['version']
    with np.load(elevation.root()/'tracks'/(first['added'][0]+'.npz')) as data:
        assert np.isfinite(data['times']).all()


def test_activity_edit_requires_ownership_and_rebuilds_travel_not_terrain(client):
    owner = register(client, 'speed-owner', True)
    receipt = contribute(client)
    identifier = receipt['added'][0]
    before = client.get('/api/elevation').json()
    height = before['regions'][0]['fingerprint']
    assert before['tracks'][0]['speedLearning']['inferredActivity'] == 'walk'
    changed = client.patch('/api/elevation/tracks/'+identifier, json={'version': before['version'], 'activity': 'bicycle'})
    assert changed.status_code == 200, changed.text
    job = wait_job(client, changed.json()['job']['id']); assert job['state'] == 'succeeded', job
    after = client.get('/api/elevation').json()
    assert after['tracks'][0]['activity'] == 'bicycle'
    assert after['tracks'][0]['speedLearning']['inferredActivity'] == 'bicycle'
    assert after['regions'][0]['fingerprint'] == height
    assert after['travelModels'][0]['fingerprint'] != before['travelModels'][0]['fingerprint']
    assert client.patch('/api/elevation/tracks/'+identifier, json={'version': before['version'], 'activity': 'walk'}).status_code == 409
    assert client.patch('/api/elevation/tracks/'+identifier, json={'version': after['version'], 'activity': 'driving'}).status_code == 422
    register(client, 'speed-other')
    assert client.patch('/api/elevation/tracks/'+identifier, json={'version': after['version'], 'activity': 'walk'}).status_code == 403
    assert client.get('/api/elevation').json()['tracks'][0]['ownerId'] == owner['id']


def test_batch_labels_validate_all_owners_before_changing_any_track(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    a, b = {'id': 'alice', 'username': 'Alice'}, {'id': 'bob', 'username': 'Bob'}
    first = elevation.add_tracks([('a.kml', timed_kml())], a)['added'][0]
    second = elevation.add_tracks([('b.kml', timed_kml(1))], b)['added'][0]
    before = elevation.catalogue()
    with pytest.raises(elevation.NotOwner):
        elevation.label_activities({first: 'bicycle', second: 'bicycle'}, a, before['version'])
    assert elevation.catalogue() == before
    applied = elevation.label_activities({first: 'bicycle'}, a, before['version'])
    assert applied['changed'] == [first]
    repeated = elevation.label_activities({first: 'bicycle'}, a, applied['version'])
    assert repeated['version'] == applied['version'] and not repeated['changed']


def test_same_geometry_with_different_recording_times_keeps_distinct_traversals(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    owner = {'id': 'owner', 'username': 'Owner'}
    content = timed_kml()
    later = content.replace(b'2026-10-01', b'2026-10-02')
    receipts = elevation.add_tracks([('first.kml', content), ('later.kml', later)], owner)
    assert len(receipts['added']) == 2
    repeated = elevation.add_tracks([('renamed.kml', later)], owner)
    assert not repeated['added'] and len(repeated['duplicates']) == 1
    records = elevation.catalogue()['tracks']
    assert records[0]['geometryId'] == records[1]['geometryId'] and records[0]['id'] != records[1]['id']
    assert records[0]['recordedAt'] != records[1]['recordedAt']
    elevation.change_track(records[0]['id'], owner, elevation.catalogue()['version'], delete=True)
    assert not elevation.add_tracks([('reexport.kml', later)], owner)['added']


def test_cleanup_keeps_both_draft_and_published_speed_snapshots(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    records = []
    for name in ('draft', 'published', 'unused'):
        path = elevation.root()/'travel-models'/(name+'.json')
        store.atomic_json(path, {'test': name}); os.utime(path, (1, 1))
        records.append({'artifact': 'travel-models/'+path.name})
    store.atomic_json(tmp_path/'projects'/'szu-1'/'plan-test.json', {'elevationSnapshot': {'travel': {'models': [records[0]]}}})
    store.atomic_json(tmp_path/'releases'/'course'/'release.json', {'plan': {'elevationSnapshot': {'travel': {'models': [records[1]]}}}})
    elevation.prune_models()
    assert (elevation.root()/records[0]['artifact']).exists()
    assert (elevation.root()/records[1]['artifact']).exists()
    assert not (elevation.root()/records[2]['artifact']).exists()


def test_published_plans_navigation_and_continuation_keep_the_learned_model(client):
    register(client, 'speed-teacher', True)
    receipts = [contribute(client, i) for i in range(3)]
    reviewed(client)
    def plan(**options):
        job = client.post('/api/plans', json={**plan_request(), 'timeLimitSec': 2, **options})
        assert job.status_code == 200, job.text
        result = wait_job(client, job.json()['id']); assert result['state'] == 'succeeded', result
        return result['result']
    original = plan(travelMode='walk')
    frozen = original['elevationSnapshot']['travel']
    assert frozen['models'] and original['routes'][0]['speedLearning']['enabled']
    response = client.post('/api/releases', json={'assetId': 'szu-1', 'planId': original['id'], 'title': 'Learned timing'})
    assert response.status_code == 200, response.text
    release_id = response.json()['id']
    source = client.get('/api/elevation').json()
    edited = client.patch('/api/elevation/tracks/'+receipts[-1]['added'][0], json={'version': source['version'], 'activity': 'bicycle'}).json()
    assert wait_job(client, edited['job']['id'])['state'] == 'succeeded'
    current = plan()
    assert current['graphHash'] != original['graphHash']
    assert current['elevationSnapshot']['revision'] == original['elevationSnapshot']['revision']
    course = client.get('/api/releases/'+release_id).json()
    assert course['superseded'] is False and course['plan']['elevationSnapshot']['travel'] == frozen
    response = client.post('/api/releases/'+release_id+'/navigate', json={'teamId': 'team1',
        'lnglat': original['routes'][0]['geometry'][0], 'accuracyM': 6, 'timestampMs': time.time()*1000, 'completedControlIds': []})
    assert response.status_code == 200, response.text
    assert response.json()['graphHash'] == original['graphHash']
    assert response.json()['route']['speedLearning']['modelRevision'] == frozen['revision']
    response = client.post('/api/plans/refine', json={'assetId': 'szu-1', 'version': original['version'], 'planId': original['id'], 'timeLimitSec': 2})
    assert response.status_code == 200, response.text
    refined = wait_job(client, response.json()['id']); assert refined['state'] == 'succeeded', refined
    assert refined['result']['elevationSnapshot']['travel'] == frozen
    assert refined['result']['routes'][0]['travelSec'] == original['routes'][0]['travelSec']
    # Publishing an earlier draft must also use that draft's speed snapshot.
    response = client.post('/api/releases', json={'assetId': 'szu-1', 'planId': original['id'], 'title': 'Same frozen inputs'})
    assert response.status_code == 200, response.text
    artifact = elevation.root()/frozen['models'][0]['artifact']
    artifact.unlink()
    response = client.post('/api/releases', json={'assetId': 'szu-1', 'planId': original['id'], 'title': 'Missing snapshot'})
    assert response.status_code == 422 and '快照' in response.json()['detail']
    response = client.post('/api/releases/'+release_id+'/navigate', json={'teamId': 'team1',
        'lnglat': original['routes'][0]['geometry'][0], 'accuracyM': 6, 'timestampMs': time.time()*1000, 'completedControlIds': []})
    assert response.status_code == 422 and '快照' in response.json()['detail']


def test_profile_and_learning_switch_survive_automatic_team_choice(client):
    register(client, 'speed-teams', True)
    contribute(client)
    job = client.post('/api/plans/auto-teams', json=automatic_request(useLearnedSpeed=False, useElevation=False, travelMode='run')).json()
    result = wait_job(client, job['id']); assert result['state'] == 'succeeded', result
    result = result['result']
    selected = choose_plan(client, choice_request(result, 2))
    assert selected['request']['travelMode'] == 'run' and selected['request']['useLearnedSpeed'] is False
    assert not selected['elevationSnapshot']['travel']['enabled']
    assert 'speedLearning' not in selected['routes'][0]
    assert selected['graphHash'] == result['graphHash']


def test_team_paces_scale_only_moving_time_and_leave_control_service_separate(client):
    register(client, 'speed-paces', True)
    for i in range(3):
        contribute(client, i)
    payload = {**plan_request(), 'teams': [{'id': 'slow', 'people': 2, 'speedMps': 1.}, {'id': 'fast', 'people': 2, 'speedMps': 2.}]}
    response = client.post('/api/plans', json=payload)
    result = wait_job(client, response.json()['id']); assert result['state'] == 'succeeded', result
    slow, fast = result['result']['routes']
    assert slow['geometry'] == fast['geometry']
    assert slow['travelSec'] == pytest.approx(2*fast['travelSec'])
    assert slow['serviceSec'] == fast['serviceSec'] == 7
