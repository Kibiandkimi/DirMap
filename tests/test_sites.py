"""Places: their own reference and frame; accounts may add more."""
from fastapi.testclient import TestClient
import numpy as np
import pytest

from dirmap import groups, service, sites, store
from dirmap.app import app
from dirmap.geometry import Frame
from tests.test_accounts import account, client_workspace, plan_request, png_bytes, save_fixture, wait


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    monkeypatch.delenv('DIRMAP_ADMIN_TOKEN', raising=False)
    service.GRAPH_CACHE.clear()
    with TestClient(app) as client:
        yield client
    service.GRAPH_CACHE.clear()


def test_dashahe_park_ships_a_reference_in_its_own_frame(server):
    park = sites.get('dashahe-park')
    assert park['builtin'] and park['status'] == 'ready'
    features = sites.reference('dashahe-park')
    kinds = {feature['kind'] for feature in features}
    assert {'building', 'water', 'pitch', 'road', 'green'} <= kinds
    # Local metres are measured from the park itself, not the campus.
    centre = np.mean([feature['center'] for feature in features if feature['kind'] == 'pitch'], axis=0)
    assert np.linalg.norm(centre) < 1500
    lnglat = sites.frame(park).to_lnglat(centre)
    assert 113.949 < lnglat[0] < 113.965 and 22.552 < lnglat[1] < 22.567
    campus = Frame().to_local([park['origin']])[0]
    assert 3000 < np.linalg.norm(campus) < 5000
    reference = server.get('/api/reference', params={'site': 'dashahe-park'}).json()
    assert len(reference['features']) == len(features)
    assert {f['geometry']['type'] for f in reference['features'] if f['properties']['kind'] == 'waterway'} <= {'LineString'}
    assert server.get('/api/reference', params={'site': 'nowhere'}).status_code == 422
    catalog = server.get('/api/catalog').json()
    assert [s['id'] for s in catalog['sites']][:2] == ['szu-yuehai', 'dashahe-park']
    assert all(m['siteId'] == 'szu-yuehai' for m in catalog['maps'] if m.get('builtin'))
    health = server.get('/api/health', params={'lng': park['origin'][0], 'lat': park['origin'][1]}).json()
    assert health['frame']['origin'] == park['origin']


def test_account_adds_a_place_by_downloading_its_osm_features(server, monkeypatch):
    calls = []
    def download(query, progress=lambda p, m: None, rounds=2):
        calls.append(query)
        return {'elements': [
            {'type': 'way', 'id': 1, 'tags': {'building': 'yes', 'name': '亭', 'irrelevant': 'dropped'},
             'geometry': [{'lat': 39.9990, 'lon': 116.3000}, {'lat': 39.9990, 'lon': 116.3004},
                          {'lat': 39.9994, 'lon': 116.3004}, {'lat': 39.9994, 'lon': 116.3000}, {'lat': 39.9990, 'lon': 116.3000}]},
            {'type': 'way', 'id': 2, 'tags': {'highway': 'footway'},
             'geometry': [{'lat': 39.998, 'lon': 116.299}, {'lat': 40.000, 'lon': 116.301}]}]}, 'https://overpass.test'
    monkeypatch.setattr(sites, 'download', download)
    teacher = account('far-away')
    too_big = teacher.post('/api/sites', json={'name': '大范围', 'bbox': [116.0, 39.8, 116.4, 40.1]})
    assert too_big.status_code == 422 and 'km' in too_big.json()['detail']
    added = teacher.post('/api/sites', json={'name': '圆明园', 'bbox': [116.295, 39.996, 116.305, 40.003]}).json()
    site = added['site']
    assert site['status'] == 'pending' and site['origin'] == [116.3, 39.9995]
    job = wait(teacher, added['job']['id'])
    assert job['state'] == 'succeeded' and job['result']['status'] == 'ready' and job['result']['featureCount'] == 2
    assert '(39.9960000,116.2950000,40.0030000,116.3050000)' in calls[0]
    listed = {s['id']: s for s in teacher.get('/api/sites').json()}
    assert listed[site['id']]['status'] == 'ready'
    saved = store.read_json(store.workspace_root(client_workspace(teacher))/'sites'/site['id']/'reference.json')
    assert saved['elements'][0]['tags'] == {'building': 'yes', 'name': '亭'} and saved['source']['license'] == 'ODbL 1.0'
    reference = teacher.get('/api/reference', params={'site': site['id']}).json()
    assert len(reference['features']) == 2
    # Another account neither sees nor uses this place.
    other = account('neighbour')
    assert site['id'] not in {s['id'] for s in other.get('/api/sites').json()}
    assert other.post('/api/maps/upload', files={'file': ('a.png', png_bytes(), 'image/png')},
                      params={'siteId': site['id']}).status_code == 422
    uploaded = teacher.post('/api/maps/upload', files={'file': ('a.png', png_bytes(), 'image/png')}, params={'siteId': site['id']}).json()
    assert teacher.delete(f'/api/sites/{site["id"]}').status_code == 422
    assert teacher.delete(f'/api/maps/{uploaded["id"]}').status_code == 200
    assert teacher.delete(f'/api/sites/{site["id"]}').status_code == 200
    assert teacher.delete('/api/sites/dashahe-park').status_code == 422


def test_failed_download_marks_a_new_place_and_keeps_an_old_reference(server, monkeypatch):
    def failing(query, progress=lambda p, m: None, rounds=2):
        raise ValueError('OpenStreetMap 参照服务暂时不可用')
    monkeypatch.setattr(sites, 'download', failing)
    teacher = account('unlucky')
    added = teacher.post('/api/sites', json={'name': '公园', 'bbox': [113.95, 22.55, 113.955, 22.555]}).json()
    job = wait(teacher, added['job']['id'])
    assert job['state'] == 'failed' and '暂时不可用' in job['message']
    site = next(s for s in teacher.get('/api/sites').json() if s['id'] == added['site']['id'])
    assert site['status'] == 'failed' and site['error']
    assert teacher.get('/api/reference', params={'site': site['id']}).status_code == 422


def test_maps_register_only_against_maps_of_the_same_place(server):
    teacher = account('two-places')
    workspace = client_workspace(teacher)
    with store.using(workspace):
        save_fixture('szu-1')
    uploaded = teacher.post('/api/maps/upload', files={'file': ('park.png', png_bytes(), 'image/png')},
                            params={'siteId': 'dashahe-park'}).json()
    with store.using(workspace):
        inputs = service.job_inputs('process', uploaded['id'])
    assert inputs['bases'] == {} and inputs['site'][0] == 'dashahe-park'
    assert inputs['referenceHash'] == sites.reference_hash(sites.get('dashahe-park'))


def test_course_groups_are_visible_to_the_course_teacher_only(server):
    teacher = account('class-teacher')
    with store.using(client_workspace(teacher)):
        save_fixture('szu-1')
    plan = wait(teacher, teacher.post('/api/plans', json=plan_request()).json()['id'])['result']
    review = {'version': 1, 'registrationChecked': True, 'controlsChecked': True, 'passagesChecked': True}
    assert teacher.post('/api/maps/szu-1/review', json=review).status_code == 200
    release = teacher.post('/api/releases', json={'assetId': 'szu-1', 'planId': plan['id']}).json()['id']
    student = TestClient(app)
    created = student.post('/api/groups', json={'name': '学生甲', 'releaseId': release, 'teamId': 'team1'}).json()
    assert created['group']['releaseId'] == release
    student.post(f'/api/groups/{created["groupId"]}/sync', headers={'X-DirMap-Member': created['token']},
                 json={'sharing': True, 'position': {'lnglat': [113.93, 22.535], 'accuracyM': 5, 'timestampMs': 0}})
    student.post(f'/api/groups/{created["groupId"]}/messages', headers={'X-DirMap-Member': created['token']}, json={'kind': 'help'})
    overview = teacher.get(f'/api/releases/{release}/groups').json()
    assert [g['code'] for g in overview['groups']] == [created['group']['code']]
    assert overview['groups'][0]['members'][0]['position']['lnglat'] == [113.93, 22.535]
    assert overview['groups'][0]['recent'][0]['kind'] == 'help'
    assert account('someone-else').get(f'/api/releases/{release}/groups').status_code == 403
    assert TestClient(app).get(f'/api/releases/{release}/groups').status_code in (401, 403)
    unrelated = student.post('/api/groups', json={'name': '学生乙'}).json()
    assert unrelated['group']['releaseId'] is None
    assert len(teacher.get(f'/api/releases/{release}/groups').json()['groups']) == 1
    assert student.post('/api/groups', json={'name': '学生丙', 'releaseId': 'f'*32}).status_code == 404
    assert groups.for_release('f'*32)['groups'] == []


def test_original_workbench_course_groups_need_its_token_until_claimed(server, monkeypatch):
    monkeypatch.setenv('DIRMAP_ADMIN_TOKEN', 'original-teacher-token')
    headers = {'X-DirMap-Token': 'original-teacher-token'}
    save_fixture('szu-1')
    original = TestClient(app)
    plan = wait(original, original.post('/api/plans', json=plan_request(), headers=headers).json()['id'])['result']
    review = {'version': 1, 'registrationChecked': True, 'controlsChecked': True, 'passagesChecked': True}
    assert original.post('/api/maps/szu-1/review', json=review, headers=headers).status_code == 200
    release = original.post('/api/releases', json={'assetId': 'szu-1', 'planId': plan['id']}, headers=headers).json()['id']
    # Students hold the course link; their groups' positions are not theirs to list.
    student = TestClient(app)
    assert student.get(f'/api/releases/{release}').status_code == 200
    assert student.get(f'/api/releases/{release}/groups').status_code == 401
    assert original.get(f'/api/releases/{release}/groups', headers=headers).json()['groups'] == []
