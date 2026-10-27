import json
from pathlib import Path
import time

import cv2
from fastapi.testclient import TestClient
import numpy as np
import pytest

from dirmap import store, service
from dirmap.app import app
from dirmap.geometry import Frame, Registration, affine_points
from dirmap.vision import TERRAIN_SCHEMA


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    monkeypatch.delenv('DIRMAP_ADMIN_TOKEN', raising=False)
    service.GRAPH_CACHE.clear()
    pixels = [[0, 0], [40, 0], [40, 40], [0, 40]]
    affine = [[1, 0, 0], [0, -1, 40]]
    reg = Registration(affine, pixels, affine_points(affine, pixels))
    state = {'assetId': 'szu-1', 'version': 1, 'width': 41, 'height': 41, 'name': 'API test', 'sourceHash': 'fixture',
             'controls': [{'id': 'c1', 'label': '31', 'pixel': [26, 26], 'radiusPx': 3, 'required': True,
                           'state': 'candidate', 'serviceBaseSec': 5, 'servicePerPersonSec': 1}],
             'registration': reg.as_dict(), 'quality': {'fitCount': 4, 'validationCount': 0}, 'edits': [],
             'reviewed': False, 'graphHash': None, 'frame': {'crs': 'WGS84', 'origin': [113.9304, 22.535]},
             'extent': [0, 0, 40, 40], 'terrainSchema': TERRAIN_SCHEMA, 'statistics': {}}
    service.project_controls(state)
    directory = store.asset_dir('szu-1')
    np.savez_compressed(directory/'terrain.npz', costs=np.ones((41, 41), np.float32))
    cv2.imwrite(str(directory/'overlay.png'), np.zeros((10, 10, 4), np.uint8))
    store.save_state('szu-1', state)
    try:
        with TestClient(app) as testclient:
            yield testclient
    finally:
        # The lifespan joins its live worker before DATA is restored.
        service.GRAPH_CACHE.clear()


def request(version=1, people=2):
    return {'assetId': 'szu-1', 'version': version, 'controlIds': ['c1'], 'startPixel': [6, 6], 'finishPixel': [6, 6],
            'mode': 'all_visit', 'teams': [{'id': 'team1', 'people': people, 'speedMps': 1.4}],
            'objective': 'min_max_finish_then_sum', 'timeLimitSec': .5}


def wait_job(client, job_id, timeout=15):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        response = client.get('/api/jobs/'+job_id)
        assert response.status_code == 200
        job = response.json()
        if job['state'] in ('succeeded', 'failed'):
            return job
        time.sleep(.01)
    pytest.fail(f'Job did not finish before the observation deadline: {job}')


def test_runtime_validation_and_origin_restrictions(client, monkeypatch):
    invalid = request(); invalid['teams'][0]['people'] = 0
    assert client.post('/api/plans', json=invalid).status_code == 422
    assert client.post('/api/plans', json=request(), headers={'Origin': 'https://unrelated.test'}).status_code == 403
    monkeypatch.setenv('DIRMAP_ADMIN_TOKEN', 'test-only-token')
    assert client.post('/api/plans', json=request()).status_code == 401
    assert client.post('/api/plans', json=request(), headers={'X-DirMap-Token': 'test-only-token'}).status_code == 200
    assert client.get('/api/health').json()['adminTokenRequired'] is True


def test_reserved_endpoint_identifiers_cannot_enter_the_control_list(client):
    state = store.get_state('szu-1')
    control = {k: v for k, v in state['controls'][0].items() if k not in ('lnglat', 'calibrated')}
    control['id'] = 'start'
    assert client.patch('/api/maps/szu-1', json={'version': 1, 'controls': [control]}).status_code == 422


def test_changed_base_registration_invalidates_copy_but_label_edits_do_not(client):
    import copy
    base = store.get_state('szu-1')
    copied = copy.deepcopy(base); copied['assetId'] = 'szu-2'
    copied['quality'].update(baseAssetId='szu-1', baseSourceHash=base['sourceHash'],
                              baseRegistrationHash=service.registration_stamp(base))
    store.save_state('szu-2', copied)
    base['version'] += 1; base['controls'][0]['label'] = '教师核对编号'
    store.save_state('szu-1', base)
    assert not service.registration_stale(copied)
    base['registration']['target'][0][0] += 2
    store.save_state('szu-1', base)
    assert service.registration_stale(copied)
    response = client.get('/api/maps/szu-2').json()
    assert response['registrationStale'] and not response['reviewed']
    assert all(not c['calibrated'] and c['lnglat'] is None for c in response['controls'])
    assert client.get('/assets/maps/szu-2/overlay.png').status_code == 409
    with pytest.raises(ValueError, match='基准图'):
        service.get_graph(copied)


def test_different_planning_inputs_do_not_reuse_other_teams_jobs(client):
    one = client.post('/api/plans', json=request(people=2)).json()
    two = client.post('/api/plans', json=request(people=7)).json()
    assert one['id'] != two['id']
    first, second = wait_job(client, one['id']), wait_job(client, two['id'])
    assert first['state'] == second['state'] == 'succeeded'
    assert first['result']['routes'][0]['people'] == 2
    assert second['result']['routes'][0]['people'] == 7
    assert second['result']['routes'][0]['serviceSec']-first['result']['routes'][0]['serviceSec'] == 5


def test_completed_http_submission_is_idempotent_and_task_history_is_readable(client):
    headers = {'X-DirMap-Request-Id': 'network-retry-123'}
    submitted = client.post('/api/plans', json=request(), headers=headers).json()
    finished = wait_job(client, submitted['id'])
    assert finished['state'] == 'succeeded'
    repeated = client.post('/api/plans', json=request(), headers=headers).json()
    assert repeated['id'] == submitted['id'] and repeated['result'] == finished['result']
    lookup = client.get('/api/jobs/request/network-retry-123')
    assert lookup.json()['id'] == submitted['id']
    assert lookup.headers['cache-control'] == 'no-store'
    assert client.get('/api/jobs?assetId=szu-1').json()[0]['state'] == 'succeeded'
    assert client.post('/api/plans', json=request(people=8), headers=headers).status_code == 422


def test_mixed_api_enforces_every_team_points_and_preserves_rule_metadata(client):
    from collections import Counter
    state = store.get_state('szu-1')
    base = state['controls'][0]
    state['controls'] = [{**base, 'id': f'c{i}', 'label': str(30+i), 'pixel': point}
                         for i, point in enumerate(([26, 26], [10, 28], [32, 12], [26, 6]), 1)]
    store.save_state('szu-1', state)
    payload = request(); payload.update(mode='mixed_coverage', timeLimitSec=5, controlIds=['c1', 'c2', 'c3', 'c4'],
        mandatoryControlIds=['c1', 'c3'], mandatoryOrder=True,
        teams=[{'id': str(i), 'people': 4, 'speedMps': 1.4} for i in range(3)])
    result = wait_job(client, client.post('/api/plans', json=payload).json()['id'])['result']
    assert result['status'] == 'OPTIMAL'
    assert result['coverage']['mandatoryControlIds'] == ['c1', 'c3']
    assert result['request']['mandatoryOrder'] is True
    assert all([c for c in route['controlIds'] if c in ('c1', 'c3')] == ['c1', 'c3'] for route in result['routes'])
    assert Counter(c for route in result['routes'] for c in route['controlIds']) == {'c1': 3, 'c3': 3, 'c2': 1, 'c4': 1}
    payload['mandatoryControlIds'] = ['not-selected']
    assert client.post('/api/plans', json=payload).status_code == 422


def test_terrain_refresh_keeps_teacher_controls_edits_and_registration(client, monkeypatch):
    state = store.get_state('szu-1'); state['controls'][0]['label'] = '教师自定点'
    state['controls'][0].update(approachPixel=[24, 24], state='accepted', labelSource='manual')
    state['edits'] = [{'kind': 'close', 'pixels': [[12, 12]], 'radiusPx': 2}]
    state['reviewed'] = True
    store.save_state('szu-1', state)
    source = store.DATA/'source.png'; cv2.imwrite(str(source), np.full((41, 41, 3), 245, np.uint8))
    monkeypatch.setattr(store, 'get_asset', lambda asset_id: {'id': asset_id, 'path': str(source), 'sha256': 'fixture'})
    job = client.post('/api/maps/szu-1/terrain', json={'version': 1}).json()
    done = wait_job(client, job['id']); assert done['state'] == 'succeeded', done
    updated = store.get_state('szu-1')
    assert updated['version'] == 2 and not updated['reviewed']
    assert updated['terrainSchema'] == TERRAIN_SCHEMA
    for field in ('controls', 'edits', 'registration', 'quality'):
        assert updated[field] == state[field]
    assert client.post('/api/maps/szu-1/terrain', json={'version': 1}).status_code == 409


def test_reprocessing_preserves_surveyed_anchors_and_teacher_added_points(client, monkeypatch):
    state = store.get_state('szu-1')
    state['controls'][0].update(id='manual-43', label='43', state='accepted', approachPixel=[24, 24])
    state['quality']['method'] = 'reviewed_landmarks_mesh'
    state['edits'] = [{'kind': 'close', 'pixels': [[12, 12]], 'radiusPx': 2}]
    store.save_state('szu-1', state)
    source = store.DATA/'source.png'; cv2.imwrite(str(source), np.full((41, 41, 3), 245, np.uint8))
    monkeypatch.setattr(store, 'get_asset', lambda asset_id: {'id': asset_id, 'path': str(source),
        'sha256': 'fixture', 'name': 'test', 'filename': 'source.png'})
    def forbidden_refit(*args, **kwargs):
        pytest.fail('Automatic processing must not replace teacher calibration')
    monkeypatch.setattr(service, 'auto_register', forbidden_refit)
    job = client.post('/api/maps/szu-1/process', headers={'X-DirMap-Request-Id': 'preserve-edits-43'}).json()
    result = wait_job(client, job['id'])
    assert result['state'] == 'succeeded', result
    after = store.get_state('szu-1')
    assert after['version'] == 2 and after['reviewed'] is False
    for field in ('controls', 'edits', 'registration', 'quality'):
        assert after[field] == state[field]
    assert after['recognitionReview']['preservedTeacherControls'] == ['manual-43']
    assert client.get('/assets/maps/szu-1/working.jpg?v=2').status_code == 200


def test_recognition_upgrade_refines_automatic_points_without_refitting_or_losing_teacher_work(client, monkeypatch):
    import copy
    state = store.get_state('szu-1')
    state['quality']['method'] = 'automatic_reference_fit'
    state['terrainSchema'] = 'older-recognition'
    state['controls'][0]['required'] = False
    manual = copy.deepcopy(state['controls'][0])
    manual.update(id='manual-43', label='43', pixel=[10, 30], state='accepted', labelSource='manual')
    state['controls'].append(manual)
    state['edits'] = [{'kind': 'close', 'pixels': [[12, 12]], 'radiusPx': 2}]
    store.save_state('szu-1', state)
    before_stamp = service.registration_stamp(state)
    source = store.DATA/'source.png'; cv2.imwrite(str(source), np.full((41, 41, 3), 245, np.uint8))
    monkeypatch.setattr(store, 'get_asset', lambda asset_id: {'id': asset_id, 'path': str(source),
        'sha256': 'fixture', 'name': 'test', 'filename': 'source.png'})
    analyze = service.analyze
    def candidates(*args, **kwargs):
        result = analyze(*args, **kwargs)
        result['controls'] = [dict(state['controls'][0], id='fresh-31', pixel=[27, 26]),
                              dict(manual, id='fresh-43', pixel=[11, 30], state='candidate')]
        return result
    monkeypatch.setattr(service, 'analyze', candidates)
    def forbidden_refit(*args, **kwargs):
        pytest.fail('Recognition upgrade must preserve the existing automatic calibration')
    monkeypatch.setattr(service, 'auto_register', forbidden_refit)
    monkeypatch.setattr(service, 'register_to_map', forbidden_refit)
    assert client.get('/api/maps/szu-1').json()['terrainStale'] is True
    headers = {'X-DirMap-Request-Id': 'recognition-upgrade-31'}
    job = client.post('/api/maps/szu-1/process?preserveRegistration=true', headers=headers).json()
    result = wait_job(client, job['id'])
    assert result['state'] == 'succeeded', result
    after = store.get_state('szu-1')
    assert after['version'] == 2 and after['terrainSchema'] == TERRAIN_SCHEMA
    assert service.registration_stamp(after) == before_stamp
    assert after['quality'] == state['quality'] and after['edits'] == state['edits']
    automatic = next(c for c in after['controls'] if c['id'] == 'c1')
    assert automatic['pixel'] == [27, 26] and automatic['required'] is False
    kept = next(c for c in after['controls'] if c['id'] == 'manual-43')
    for key in ('pixel', 'label', 'state', 'labelSource'):
        assert kept[key] == manual[key]
    assert len(after['controls']) == 2 and after['recognitionReview']['preservedTeacherControls'] == ['manual-43']
    assert client.get('/api/maps/szu-1').json()['terrainStale'] is False
    replay = client.post('/api/maps/szu-1/process?preserveRegistration=true', headers=headers).json()
    assert replay['id'] == job['id'] and store.get_state('szu-1')['version'] == 2


@pytest.mark.parametrize('invalid', ['missing-calibration', 'source-changed', 'base-changed'])
def test_recognition_upgrade_requires_a_current_calibration(client, monkeypatch, invalid):
    state = store.get_state('szu-1')
    if invalid == 'missing-calibration': state['registration'] = None
    elif invalid == 'source-changed': state['sourceHash'] = 'previous-source'
    else: state['quality'].update(baseAssetId='szu-2', baseRegistrationHash='old')
    store.save_state('szu-1', state)
    monkeypatch.setattr(store, 'get_asset', lambda asset_id: {'id': asset_id, 'sha256': 'fixture'})
    job = client.post('/api/maps/szu-1/process?preserveRegistration=true').json()
    result = wait_job(client, job['id'])
    assert result['state'] == 'failed' and '配准' in result['message'], result
    assert store.get_state('szu-1') == state


def test_edit_version_conflict_out_of_bounds_and_stale_plan(client):
    assert client.patch('/api/maps/szu-1', json={'version': 5, 'edits': []}).status_code == 409
    assert client.patch('/api/maps/szu-1', json={'version': 1, 'edits': [{'kind': 'close', 'pixels': [[999, 2]]}]}).status_code == 422
    result = client.patch('/api/maps/szu-1', json={'version': 1, 'edits': [{'kind': 'close', 'pixels': [[22, 18]], 'radiusPx': 2}]}).json()
    assert result['version'] == 2 and result['reviewed'] is False
    job = client.post('/api/plans', json=request()).json()
    done = wait_job(client, job['id'])
    assert done['state'] == 'failed' and '更新' in done['message']


def test_old_map_version_cannot_download_a_new_overlay_under_old_coordinates(client):
    original = client.get('/assets/maps/szu-1/overlay.png?v=1')
    assert original.status_code == 200
    changed = client.patch('/api/maps/szu-1', json={'version': 1, 'edits': []})
    assert changed.status_code == 200 and changed.json()['version'] == 2
    assert client.get('/assets/maps/szu-1/overlay.png?v=1').status_code == 409
    current = client.get('/assets/maps/szu-1/overlay.png?v=2')
    assert current.status_code == 200 and current.content == original.content


def test_immutable_release_invalidated_by_new_closure(client):
    state = store.get_state('szu-1'); state['controls'][0]['state'] = 'accepted'; store.save_state('szu-1', state)
    review = {'version': 1, 'registrationChecked': True, 'controlsChecked': True, 'passagesChecked': True}
    assert client.post('/api/maps/szu-1/review', json=review).status_code == 200
    job = client.post('/api/plans', json=request()).json()
    plan = wait_job(client, job['id'])['result']
    assert plan['draft'] is False
    response = client.post('/api/releases', json={'assetId': 'szu-1', 'planId': plan['id'], 'title': 'Test course'})
    assert response.status_code == 200
    release_id = response.json()['id']
    before = client.get(f'/api/releases/{release_id}').json()
    assert before['superseded'] is False
    image = client.get(f'/api/releases/{release_id}/overlay.png').content
    client.patch('/api/maps/szu-1', json={'version': 1, 'edits': [{'kind': 'close', 'pixels': [[22, 22]], 'radiusPx': 5}]})
    after = client.get(f'/api/releases/{release_id}').json()
    assert after['superseded'] is True
    assert after['plan'] == before['plan']
    assert client.get(f'/api/releases/{release_id}/overlay.png').content == image
    assert client.post('/api/releases', json={'assetId': 'szu-1', 'planId': plan['id']}).status_code == 422


def test_unknown_image_and_unreviewed_release_rejected(client):
    for name, payload, mime in [('bad.jpg', b'not a valid image', 'image/jpeg'),
                               ('truncated.png', b'\x89PNG\r\n\x1a\n', 'image/png')]:
        response = client.post('/api/maps/upload', files={'file': (name, payload, mime)})
        assert response.status_code == 422
    assert not list(store.DATA.glob('projects/upload-*/source.img'))
    assert client.post('/api/releases', json={'assetId': 'szu-1', 'planId': 'a'*16}).status_code == 422
    assert client.get('/api/jobs/unknown').status_code == 404


def test_uploaded_source_manifest_is_portable_and_original_download_is_exact(client):
    payload = cv2.imencode('.png', np.full((40, 60, 3), 255, np.uint8))[1].tobytes()
    response = client.post('/api/maps/upload', files={'file': ('course.png', payload, 'image/png')})
    assert response.status_code == 200
    asset_id = response.json()['id']
    manifest = store.read_json(store.DATA/'uploads.json')
    assert manifest[0]['path'] == f'projects/{asset_id}/source.img'
    assert client.get(f'/assets/maps/{asset_id}/original').content == payload
    assert client.get(f'/assets/maps/{asset_id}/working.jpg').content == payload


def automatic_request(**overrides):
    payload = {key: value for key, value in request().items() if key != 'teams'}
    payload.update(totalPeople=12, minPeoplePerTeam=2, maxTeams=6, speedMps=1.4,
                   staggerSec=0, targetGainFraction=.9, timeLimitSec=2)
    return {**payload, **overrides}


def test_automatic_team_job_reuses_one_graph_matrix_and_preserves_recommendation_on_release(client, monkeypatch):
    original = service.plan_matrix
    calls = []
    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(service, 'plan_matrix', counted)
    headers = {'X-DirMap-Request-Id': 'automatic-team-comparison'}
    job = client.post('/api/plans/auto-teams', json=automatic_request(), headers=headers).json()
    duplicate = client.post('/api/plans/auto-teams', json=automatic_request(), headers=headers).json()
    assert job['id'] == duplicate['id'] and job['kind'] == 'auto_teams'
    done = wait_job(client, job['id'])
    assert done['state'] == 'succeeded', done
    plan = done['result']
    assert calls == [1]
    assert plan['teamRecommendation']['recommendedCount'] == len(plan['routes']) == 4
    assert len(plan['request']['teams']) == 4
    assert sum(team['people'] for team in plan['request']['teams']) == 12
    assert plan['teamRecommendation']['evaluatedCounts'] == [1, 2, 3, 4, 5, 6]
    selected = next(c for c in plan['teamRecommendation']['candidates'] if c['recommended'])
    assert selected['latestFinishSec'] == pytest.approx(max(r['finishSec'] for r in plan['routes']))
    assert selected['totalDistanceM'] == pytest.approx(sum(r['distanceM'] for r in plan['routes']))
    state = store.get_state('szu-1')
    state['controls'][0]['state'] = 'accepted'
    store.save_state('szu-1', state)
    review = {'version': 1, 'registrationChecked': True, 'controlsChecked': True, 'passagesChecked': True}
    assert client.post('/api/maps/szu-1/review', json=review).status_code == 200
    released = client.post('/api/releases', json={'assetId': 'szu-1', 'planId': plan['id']})
    assert released.status_code == 200, released.text
    snapshot = client.get('/api/releases/'+released.json()['id']).json()
    assert snapshot['plan']['teamRecommendation'] == plan['teamRecommendation']


def test_automatic_distance_comparison_reports_true_walked_metres_and_weighted_times(client):
    np.savez_compressed(store.asset_dir('szu-1')/'terrain.npz', costs=np.full((41, 41), 2, np.float32))
    job = client.post('/api/plans/auto-teams', json=automatic_request(objective='min_distance')).json()
    done = wait_job(client, job['id'])
    assert done['state'] == 'succeeded', done
    plan = done['result']
    assert plan['teamRecommendation']['recommendedCount'] == 1
    route = plan['routes'][0]
    assert route['travelSec'] == pytest.approx(route['distanceM']*2/1.4)
    row = plan['teamRecommendation']['candidates'][0]
    assert row['totalDistanceM'] == pytest.approx(route['distanceM'])
    assert row['latestFinishSec'] == pytest.approx(route['finishSec'])


def compared_plan(client):
    job = client.post('/api/plans/auto-teams', json=automatic_request()).json()
    result = wait_job(client, job['id'])
    assert result['state'] == 'succeeded', result
    return result['result']


def choice_request(plan, count=2, **overrides):
    comparison = plan['teamRecommendation']
    return {'assetId': plan['assetId'], 'version': plan['version'], 'comparisonId': comparison['comparisonId'],
            'comparisonToken': comparison['comparisonToken'], 'teamCount': count, **overrides}


def choose_plan(client, payload, key='choose-comparison-count'):
    response = client.post('/api/plans/choose-team-count', json=payload, headers={'X-DirMap-Request-Id': key})
    assert response.status_code == 200, response.text
    result = wait_job(client, response.json()['id'])
    assert result['state'] == 'succeeded', result
    return result['result']


def test_choosing_compared_count_restores_exact_solution_without_optimization(client, monkeypatch):
    from dirmap import comparisons
    original = compared_plan(client)
    snapshot = comparisons.load('szu-1', original['teamRecommendation']['comparisonId'])
    orders = snapshot['content']['candidates']['2']['orders']
    def forbidden(*args, **kwargs):
        raise AssertionError('Adopting a compared solution must not run optimization again')
    monkeypatch.setattr(service, 'solve', forbidden)
    monkeypatch.setattr(service, 'recommend_teams', forbidden)
    payload = choice_request(original)
    selected = choose_plan(client, payload)
    replay = choose_plan(client, payload)
    assert replay == selected
    assert selected['id'] != original['id']
    assert [r['order'] for r in selected['routes']] == orders
    assert len(selected['routes']) == 2
    assert selected['teamRecommendation']['recommendedCount'] == 4
    assert selected['teamRecommendation']['selectedCount'] == 2
    assert selected['teamRecommendation']['selectionMode'] == 'teacher'
    row = next(r for r in original['teamRecommendation']['candidates'] if r['teamCount'] == 2)
    assert sum(r['distanceM'] for r in selected['routes']) == pytest.approx(row['totalDistanceM'])
    assert max(r['finishSec'] for r in selected['routes']) == pytest.approx(row['latestFinishSec'])
    assert store.read_json(store.asset_dir('szu-1')/f'plan-{original["id"]}.json') == original
    assert len(list(store.asset_dir('szu-1').glob('comparison-*.json'))) == 1


def reviewed_choice(client, change_service=False):
    original = compared_plan(client)
    selected = choose_plan(client, choice_request(original))
    state = store.get_state('szu-1')
    controls = [{k: v for k, v in c.items() if k not in ('calibrated', 'lnglat')} for c in state['controls']]
    controls[0].update(state='accepted', approachPixel=controls[0]['pixel'])
    if change_service:
        controls[0]['serviceBaseSec'] += 100
    updated = client.patch('/api/maps/szu-1', json={'version': 1, 'controls': controls}).json()
    assert updated['version'] == 2
    assert client.post('/api/maps/szu-1/review', json={'version': 2, 'registrationChecked': True,
        'controlsChecked': True, 'passagesChecked': True}).status_code == 200
    return selected


def test_review_and_publication_keep_the_teacher_choice_and_all_original_orders(client, monkeypatch):
    selected = reviewed_choice(client)
    def forbidden(*args, **kwargs):
        raise AssertionError('Confirmation must preserve the reviewed choice')
    monkeypatch.setattr(service, 'solve', forbidden)
    monkeypatch.setattr(service, 'recommend_teams', forbidden)
    confirmed = choose_plan(client, choice_request(selected, version=2, confirmPlanId=selected['id']), 'confirm-chosen-plan')
    assert confirmed['draft'] is False and confirmed['version'] == 2
    assert len(confirmed['routes']) == 2
    assert confirmed['teamRecommendation']['recommendedCount'] == 4
    assert confirmed['teamRecommendation']['selectedCount'] == 2
    assert confirmed['teamRecommendation']['comparisonId'] != selected['teamRecommendation']['comparisonId']
    for before, after in zip(selected['routes'], confirmed['routes']):
        assert before['order'] == after['order']
        assert before['geometry'] == after['geometry']
        assert before['finishSec'] == after['finishSec']
    response = client.post('/api/releases', json={'assetId': 'szu-1', 'planId': confirmed['id']})
    assert response.status_code == 200, response.text
    release = client.get('/api/releases/'+response.json()['id']).json()
    assert len(release['plan']['routes']) == 2 and release['plan']['teamRecommendation']['selectedCount'] == 2
    # The comparison fork revalidated for the new map version remains usable.
    another = choose_plan(client, choice_request(confirmed, 3), 'choose-after-confirmation')
    assert len(another['routes']) == 3


def test_changed_map_and_changed_service_cannot_reuse_old_comparison_proof(client):
    selected = reviewed_choice(client, change_service=True)
    response = client.post('/api/plans/choose-team-count', json=choice_request(selected, version=2))
    assert response.status_code == 200
    failure = wait_job(client, response.json()['id'])
    assert failure['state'] == 'failed' and '地图已更新' in failure['message']
    response = client.post('/api/plans/choose-team-count', json=choice_request(selected, version=2, confirmPlanId=selected['id']))
    failure = wait_job(client, response.json()['id'])
    assert failure['state'] == 'failed' and '耗时设置' in failure['message']


def test_corrupt_comparison_and_unavailable_count_are_refused(client):
    from dirmap import comparisons
    plan = compared_plan(client)
    response = client.post('/api/plans/choose-team-count', json=choice_request(plan, 30))
    failure = wait_job(client, response.json()['id'])
    assert failure['state'] == 'failed' and '不在原比较范围' in failure['message']
    path = comparisons.path('szu-1', plan['teamRecommendation']['comparisonId'])
    content = store.read_json(path)
    content['content']['candidates']['2']['orders'][0] = [0, 999, 2]
    store.atomic_json(path, content)
    response = client.post('/api/plans/choose-team-count', json=choice_request(plan))
    assert response.status_code == 422 and '记录已改变' in response.text


def test_review_of_other_targets_does_not_authorize_an_unconfirmed_plan_target(client):
    state = store.get_state('szu-1')
    state['controls'][0]['required'] = False
    store.save_state('szu-1', state)
    assert client.post('/api/maps/szu-1/review', json={'version': 1, 'registrationChecked': True,
        'controlsChecked': True, 'passagesChecked': True}).status_code == 200
    plan = compared_plan(client)
    assert plan['draft'] is True
    response = client.post('/api/releases', json={'assetId': 'szu-1', 'planId': plan['id']})
    assert response.status_code == 422 and '目标未核验' in response.text


@pytest.mark.parametrize('changes', [{'totalPeople': True}, {'minPeoplePerTeam': 13}, {'maxTeams': 31},
                                   {'targetGainFraction': .2}, {'mode': 'custom'}, {'controlIds': ['c1', 'c1']}])
def test_automatic_team_request_rejects_inconsistent_rules(client, changes):
    assert client.post('/api/plans/auto-teams', json=automatic_request(**changes)).status_code == 422


def navigation_release(client):
    state = store.get_state('szu-1')
    first = state['controls'][0]
    first['state'] = 'accepted'
    state['controls'].append({**first, 'id': 'c2', 'label': '32', 'pixel': [32, 34]})
    state['reviewed'] = True
    state['review'] = {'registrationChecked': True, 'controlsChecked': True, 'passagesChecked': True}
    service.project_controls(state)
    store.save_state('szu-1', state)
    costs = np.ones((41, 41), np.float32)
    costs[:30, 20] = 0  # legal route goes around the wall, not through it
    np.savez_compressed(store.asset_dir('szu-1')/'terrain.npz', costs=costs)
    payload = request(); payload['controlIds'] = ['c1', 'c2']; payload['mode'] = 'fixed_order'
    result = wait_job(client, client.post('/api/plans', json=payload).json()['id'])['result']
    release_id = client.post('/api/releases', json={'assetId': 'szu-1', 'planId': result['id']}).json()['id']
    return release_id, Registration.from_dict(state['registration'])


def navigation_request(registration, pixel, completed=None):
    world, _ = registration.forward([pixel])
    return {'teamId': 'team1', 'lnglat': Frame().to_lnglat(world[0]).tolist(), 'accuracyM': 5,
            'timestampMs': time.time()*1000, 'completedControlIds': completed or []}


def test_student_rejoin_preserves_assignment_order_and_never_saves_location(client, monkeypatch):
    release_id, reg = navigation_release(client)
    snapshot = {str(p): p.read_bytes() for p in store.DATA.rglob('*') if p.is_file()}
    monkeypatch.setenv('DIRMAP_ADMIN_TOKEN', 'teacher-only')
    response = client.post(f'/api/releases/{release_id}/navigate', json=navigation_request(reg, [28, 15], ['c1']))
    assert response.status_code == 200
    result = response.json(); assert result['status'] == 'READY'
    assert result['route']['controlIds'] == ['c2']
    assert [leg['toId'] for leg in result['route']['legs']] == ['c2', 'finish']
    graph = service.get_graph(store.get_state('szu-1'))
    geometry = reg.inverse(Frame().to_local(result['route']['geometry']))
    assert all(graph.clear_segment(a, b) for a, b in zip(geometry, geometry[1:]))
    assert {str(p): p.read_bytes() for p in store.DATA.rglob('*') if p.is_file()} == snapshot
    # Read-only student navigation does not grant teacher editing privileges.
    assert client.patch('/api/maps/szu-1', json={'version': 1, 'edits': []}).status_code == 401
    assert client.post(f'/api/releases/{release_id}/navigate', json=navigation_request(reg, [28, 15]),
                       headers={'Origin': 'https://unrelated.test'}).status_code == 403


def test_student_rejoin_rejects_skipped_controls_stale_fixes_and_wall_snapping(client):
    release_id, reg = navigation_release(client)
    url = f'/api/releases/{release_id}/navigate'
    assert client.post(url, json=navigation_request(reg, [28, 15], ['c2'])).status_code == 422
    old = navigation_request(reg, [28, 15]); old['timestampMs'] -= 30000
    assert client.post(url, json=old).status_code == 422
    imprecise = navigation_request(reg, [28, 15]); imprecise['accuracyM'] = 50
    assert client.post(url, json=imprecise).status_code == 422
    wall = client.post(url, json=navigation_request(reg, [20, 12])).json()
    assert wall['status'] == 'NEEDS_REVIEW' and wall['route'] is None
    assert any(item['id'] == 'start' for item in wall['unresolved'])
    finished = client.post(url, json=navigation_request(reg, [30, 32], ['c1', 'c2'])).json()
    assert finished['status'] == 'READY' and not finished['route']['controlIds']
    assert [leg['toId'] for leg in finished['route']['legs']] == ['finish']
    assert finished['route']['serviceSec'] == 0
    client.patch('/api/maps/szu-1', json={'version': 1, 'edits': []})
    assert client.post(url, json=navigation_request(reg, [28, 15])).status_code == 409


def test_job_cancellation_is_a_protected_teacher_action(client, monkeypatch):
    assert client.post('/api/jobs/0123456789abcdef/cancel').status_code == 404
    job = client.post('/api/plans', json=request()).json()
    monkeypatch.setenv('DIRMAP_ADMIN_TOKEN', 'test-only-token')
    assert client.post(f"/api/jobs/{job['id']}/cancel").status_code == 401
    cancelled = client.post(f"/api/jobs/{job['id']}/cancel", headers={'X-DirMap-Token': 'test-only-token'}).json()
    assert cancelled['id'] == job['id'] and 'result' not in cancelled
    # A tiny plan may already have finished; otherwise it stops or is marked.
    assert cancelled['state'] == 'succeeded' or cancelled.get('cancelled') or cancelled.get('cancelRequested')


def test_students_share_progress_without_the_teacher_token(client, monkeypatch):
    state = store.get_state('szu-1'); state['controls'][0]['state'] = 'accepted'; store.save_state('szu-1', state)
    review = {'version': 1, 'registrationChecked': True, 'controlsChecked': True, 'passagesChecked': True}
    assert client.post('/api/maps/szu-1/review', json=review).status_code == 200
    plan = wait_job(client, client.post('/api/plans', json=request()).json()['id'])['result']
    release_id = client.post('/api/releases', json={'assetId': 'szu-1', 'planId': plan['id']}).json()['id']
    monkeypatch.setenv('DIRMAP_ADMIN_TOKEN', 'teacher-only')
    url = f'/api/releases/{release_id}/progress'
    body = {'teamId': 'team1', 'deviceId': 'student-phone', 'revision': 1, 'completed': ['c1'],
            'checkIns': {'c1': time.time()*1000}, 'departed': []}
    assert client.get(url).json()['teams'][0]['completed'] == []
    shared = client.post(url, json=body)
    assert shared.status_code == 200 and shared.json()['teams'][0]['finished']
    assert shared.headers['cache-control'] == 'no-store'
    assert client.get(url).json()['teams'][0]['completed'] == ['c1']
    assert client.post(url, json={**body, 'revision': 1.5}).status_code == 422
    assert client.post(url, json={**body, 'deviceId': 'x'}).status_code == 422
    assert client.post(url, json=body, headers={'Origin': 'https://elsewhere.test'}).status_code == 403
    assert client.post('/api/releases/'+'0'*32+'/progress', json=body).status_code == 404
    client.patch('/api/maps/szu-1', json={'version': 1, 'edits': []}, headers={'X-DirMap-Token': 'teacher-only'})
    assert client.post(url, json={**body, 'revision': 2}).status_code == 409


def test_teacher_lists_published_courses_with_class_progress(client, monkeypatch):
    state = store.get_state('szu-1'); state['controls'][0]['state'] = 'accepted'; store.save_state('szu-1', state)
    review = {'version': 1, 'registrationChecked': True, 'controlsChecked': True, 'passagesChecked': True}
    assert client.post('/api/maps/szu-1/review', json=review).status_code == 200
    plan = wait_job(client, client.post('/api/plans', json=request()).json()['id'])['result']
    first = client.post('/api/releases', json={'assetId': 'szu-1', 'planId': plan['id'], 'title': '第一节'}).json()['id']
    second = client.post('/api/releases', json={'assetId': 'szu-1', 'planId': plan['id'], 'title': '第二节'}).json()['id']
    (store.DATA/'releases'/'not-a-course').mkdir()
    client.post(f'/api/releases/{second}/progress', json={'teamId': 'team1', 'deviceId': 'student-phone', 'revision': 1,
                                                           'completed': ['c1'], 'checkIns': {}, 'departed': []})
    listed = client.get('/api/releases?assetId=szu-1').json()
    assert {item['id'] for item in listed} == {first, second}
    latest = next(item for item in listed if item['id'] == second)
    assert latest['url'] == '/?course='+second and latest['title'] == '第二节' and not latest['superseded']
    assert (latest['teams'], latest['startedTeams'], latest['finishedTeams']) == (1, 1, 1) and latest['lastReportAt']
    assert client.get('/api/releases?assetId=szu-2').json() == []
    monkeypatch.setenv('DIRMAP_ADMIN_TOKEN', 'teacher-only')
    assert client.get('/api/releases').status_code == 401
    assert len(client.get('/api/releases', headers={'X-DirMap-Token': 'teacher-only'}).json()) == 2
    assert client.get(f'/api/releases/{first}').status_code == 200
