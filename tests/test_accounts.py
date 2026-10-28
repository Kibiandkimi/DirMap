"""Accounts work in separate workbenches and need no teacher token there."""
import os

import cv2
from fastapi.testclient import TestClient
import numpy as np
import pytest

from dirmap import accounts, samples, service, store
from dirmap.app import Control, app
from dirmap.geometry import Registration, affine_points
from dirmap.vision import TERRAIN_SCHEMA


def fixture_state(asset_id='szu-1', source_hash='fixture'):
    pixels = [[0, 0], [40, 0], [40, 40], [0, 40]]
    affine = [[1, 0, 0], [0, -1, 40]]
    reg = Registration(affine, pixels, affine_points(affine, pixels))
    state = {'assetId': asset_id, 'version': 1, 'width': 41, 'height': 41, 'name': 'Account test', 'sourceHash': source_hash,
             'controls': [{'id': 'c1', 'label': '31', 'pixel': [26, 26], 'radiusPx': 3, 'required': True,
                           'state': 'accepted', 'serviceBaseSec': 5, 'servicePerPersonSec': 1}],
             'registration': reg.as_dict(), 'quality': {'fitCount': 4, 'validationCount': 0}, 'edits': [],
             'reviewed': False, 'graphHash': None, 'frame': {'crs': 'WGS84', 'origin': [113.9304, 22.535]},
             'extent': [0, 0, 40, 40], 'terrainSchema': TERRAIN_SCHEMA, 'statistics': {}}
    service.project_controls(state)
    return state


def save_fixture(asset_id='szu-1', source_hash='fixture'):
    directory = store.asset_dir(asset_id)
    np.savez_compressed(directory/'terrain.npz', costs=np.ones((41, 41), np.float32))
    cv2.imwrite(str(directory/'overlay.png'), np.zeros((10, 10, 4), np.uint8))
    store.save_state(asset_id, fixture_state(asset_id, source_hash))


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    monkeypatch.delenv('DIRMAP_ADMIN_TOKEN', raising=False)
    service.GRAPH_CACHE.clear()
    save_fixture()
    try:
        with TestClient(app) as client:
            yield client
    finally:
        service.GRAPH_CACHE.clear()


def account(name='teacher-a', password='correct horse', **extra):
    client = TestClient(app)
    response = client.post('/api/auth/register', json={'username': name, 'password': password, **extra})
    assert response.status_code == 200, response.text
    return client


def plan_request(version=1):
    return {'assetId': 'szu-1', 'version': version, 'controlIds': ['c1'], 'startPixel': [6, 6], 'finishPixel': [6, 6],
            'mode': 'all_visit', 'teams': [{'id': 'team1', 'people': 2, 'speedMps': 1.4}],
            'objective': 'min_max_finish_then_sum', 'timeLimitSec': .5}


def wait(client, job_id):
    import time
    for _ in range(1500):
        job = client.get('/api/jobs/'+job_id).json()
        if job['state'] in ('succeeded', 'failed'):
            return job
        time.sleep(.01)
    pytest.fail('job did not finish')


def png_bytes():
    return cv2.imencode('.png', np.full((40, 40, 3), 240, np.uint8))[1].tobytes()


def test_accounts_see_only_their_own_maps_jobs_and_states(server, monkeypatch):
    monkeypatch.setenv('DIRMAP_ADMIN_TOKEN', 'original-teacher-token')
    first, second = account('teacher-a'), account('teacher-b')
    uploaded = first.post('/api/maps/upload', files={'file': ('park.png', png_bytes(), 'image/png')},
                          params={'siteId': 'dashahe-park'})
    # No teacher token: the session decides whose workbench this is.
    assert uploaded.status_code == 200 and uploaded.json()['siteId'] == 'dashahe-park'
    asset_id = uploaded.json()['id']
    assert asset_id in {m['id'] for m in first.get('/api/catalog').json()['maps']}
    assert asset_id not in {m['id'] for m in second.get('/api/catalog').json()['maps']}
    assert second.get(f'/api/maps/{asset_id}').status_code == 422
    assert second.delete(f'/api/maps/{asset_id}').status_code == 422
    # The original workbench's fixture is not part of either new workbench.
    assert first.get('/api/maps/szu-1').json()['processed'] is False
    with store.using(store.LEGACY):
        assert store.get_state('szu-1')['name'] == 'Account test'
    # Anonymous visitors keep the original workbench under its old token.
    anonymous = TestClient(app)
    assert anonymous.get('/api/maps/szu-1').json()['name'] == 'Account test'
    assert anonymous.post('/api/maps/upload', files={'file': ('a.png', png_bytes(), 'image/png')}).status_code == 401
    assert first.delete(f'/api/maps/{asset_id}').status_code == 200
    assert asset_id not in {m['id'] for m in first.get('/api/catalog').json()['maps']}


def test_jobs_and_courses_of_one_account_are_invisible_to_another(server):
    first, second = account('teacher-a'), account('teacher-b')
    for client in (first, second):
        with store.using(client_workspace(client)):
            save_fixture()
    job = first.post('/api/plans', json=plan_request()).json()
    assert second.get('/api/jobs/'+job['id']).status_code == 404
    assert second.post(f'/api/jobs/{job["id"]}/cancel').status_code == 404
    assert job['id'] not in [j['id'] for j in second.get('/api/jobs').json()]
    plan = wait(first, job['id'])['result']
    review = {'version': 1, 'registrationChecked': True, 'controlsChecked': True, 'passagesChecked': True}
    assert first.post('/api/maps/szu-1/review', json=review).status_code == 200
    release = first.post('/api/releases', json={'assetId': 'szu-1', 'planId': plan['id']}).json()
    assert [r['id'] for r in first.get('/api/releases').json()] == [release['id']]
    assert second.get('/api/releases').json() == []
    assert second.post(f'/api/releases/{release["id"]}/session', json={'action': 'start'}).status_code == 403
    assert first.post(f'/api/releases/{release["id"]}/session', json={'action': 'start'}).status_code == 200
    # Students need no account; the course is judged against its own workbench.
    student = TestClient(app)
    course = student.get(f'/api/releases/{release["id"]}').json()
    assert course['superseded'] is False and 'workspace' not in course and course['siteId'] == 'szu-yuehai'
    assert student.get(f'/api/releases/{release["id"]}/reference').json()['type'] == 'FeatureCollection'
    # A change in another workbench's map with the same ID changes nothing.
    with store.using(client_workspace(second)):
        changed = store.get_state('szu-1'); changed['version'] = 9; store.save_state('szu-1', changed)
    assert student.get(f'/api/releases/{release["id"]}/status').json()['superseded'] is False


def client_workspace(client):
    user = accounts.session_user(client.cookies.get(accounts.COOKIE))
    return user['workspace']


def test_claiming_the_original_workbench_ends_anonymous_access(server, monkeypatch):
    monkeypatch.setenv('DIRMAP_ADMIN_TOKEN', 'original-teacher-token')
    anonymous = TestClient(app)
    status = anonymous.get('/api/auth/me').json()
    assert status['user'] is None and status['original'] == {'claimed': False, 'tokenRequired': True, 'hasData': True}
    refused = TestClient(app).post('/api/auth/register', json={'username': 'owner', 'password': 'correct horse',
                                                                'claimOriginal': True, 'originalToken': 'wrong'})
    assert refused.status_code == 400 and '口令' in refused.json()['detail']
    owner = account('owner', claimOriginal=True, originalToken='original-teacher-token')
    assert owner.get('/api/auth/me').json()['user']['ownsOriginalWorkbench'] is True
    assert owner.get('/api/maps/szu-1').json()['name'] == 'Account test'
    assert owner.post('/api/maps/szu-1/review', json={'version': 1, 'registrationChecked': True, 'controlsChecked': True,
                                                      'passagesChecked': True}).status_code == 200
    assert anonymous.get('/api/maps/szu-1').status_code == 401
    assert anonymous.get('/api/catalog').json()['login'] is True
    assert anonymous.get('/api/auth/me').json()['original']['claimed'] is True
    later = account('second-teacher')
    assert later.post('/api/auth/claim-original', json={'originalToken': 'original-teacher-token'}).status_code == 400


def test_new_account_may_claim_the_original_workbench_before_it_has_its_own_work(server):
    teacher = account('late-owner')
    assert teacher.post('/api/auth/claim-original', json={}).json()['user']['ownsOriginalWorkbench'] is True
    assert teacher.get('/api/maps/szu-1').json()['name'] == 'Account test'
    busy = account('busy-teacher')
    assert busy.post('/api/maps/upload', files={'file': ('a.png', png_bytes(), 'image/png')}).status_code == 200
    assert busy.post('/api/auth/claim-original', json={}).status_code == 400


def public_sample_bundle(directory):
    data = directory/'public-source'
    for asset in store.builtin_maps():
        state = fixture_state(asset['id'], asset['sha256'])
        state['controls'][0].update(state='candidate', labelSource='tesseract')
        target = data/'projects'/asset['id']; target.mkdir(parents=True)
        np.savez_compressed(target/'terrain.npz', costs=np.ones((41, 41), np.float32))
        cv2.imwrite(str(target/'overlay.png'), np.zeros((10, 10, 4), np.uint8))
        store.atomic_json(target/'state.json', state)
    samples.write_bundle(data, samples.BUNDLE)
    return data


def test_new_workbench_starts_from_processed_samples_sharing_their_files(server, tmp_path):
    public_sample_bundle(tmp_path)
    asset = store.builtin_maps()[0]
    with store.using(store.LEGACY):
        state = fixture_state(asset['id'], asset['sha256'])
        state.update(reviewed=True, review={'notes': 'owner review'})
        state['controls'][0].update(label='私有目标', labelSource='manual', manualFields=['label'])
        directory = store.asset_dir(asset['id'])/'derived'/('a'*32)
        directory.mkdir(parents=True)
        np.savez_compressed(directory/'terrain.npz', costs=np.ones((41, 41), np.float32))
        cv2.imwrite(str(directory/'overlay.png'), np.zeros((10, 10, 4), np.uint8))
        state['artifacts'] = {'terrain.npz': 'derived/'+'a'*32+'/terrain.npz', 'overlay.png': 'derived/'+'a'*32+'/overlay.png'}
        store.save_state(asset['id'], state, operation_id='legacy-operation')
        store.atomic_json(store.asset_dir(asset['id'])/'plan-0123456789abcdef.json', {'id': 'private'})
    teacher = account('newcomer')
    workspace = client_workspace(teacher)
    copied = teacher.get(f'/api/maps/{asset["id"]}').json()
    assert copied['controls'][0]['label'] == '31' and copied['reviewed'] is False and 'review' not in copied
    source = samples.source_root()/'projects'/asset['id']/'terrain.npz'
    target = store.workspace_root(workspace)/'projects'/asset['id']/'terrain.npz'
    assert os.stat(source).st_ino == os.stat(target).st_ino
    assert not list((store.workspace_root(workspace)/'projects'/asset['id']).glob('plan-*.json'))
    # Editing the copy leaves the owner's map untouched.
    patch = {'version': copied['version'], 'controls': [{k: v for k, v in copied['controls'][0].items()
                                                         if k in Control.model_fields} | {'label': '99'}]}
    assert teacher.patch(f'/api/maps/{asset["id"]}', json=patch).status_code == 200
    with store.using(store.LEGACY):
        assert store.get_state(asset['id'])['controls'][0]['label'] == '私有目标'
    second = account('another-newcomer')
    assert second.get(f'/api/maps/{asset["id"]}').json()['controls'][0]['label'] == '31'
    other = store.workspace_root(client_workspace(second))/'projects'/asset['id']/'terrain.npz'
    assert other.stat().st_ino == target.stat().st_ino


def test_registration_cannot_copy_the_original_owners_private_edits(server):
    asset = store.builtin_maps()[0]
    save_fixture(asset['id'], asset['sha256'])
    owner = account('original-owner', claimOriginal=True)
    changed = owner.patch(f'/api/maps/{asset["id"]}', json={
        'version': 1,
        'controls': [{'id': 'c1', 'label': '私有训练点', 'pixel': [26, 26], 'radiusPx': 3, 'required': True, 'state': 'accepted'}],
        'edits': [{'kind': 'close', 'pixels': [[10, 11], [12, 13]], 'radiusPx': 2}],
    })
    assert changed.status_code == 200
    assert TestClient(app).get(f'/api/maps/{asset["id"]}').status_code == 401
    newcomer = account('independent-user')
    visible = newcomer.get(f'/api/maps/{asset["id"]}')
    assert visible.status_code == 200 and '私有训练点' not in visible.text
    assert visible.json().get('edits', []) == []
    assert owner.get(f'/api/maps/{asset["id"]}').json()['controls'][0]['label'] == '私有训练点'


def test_claiming_original_cannot_discard_work_on_a_sample_map(server):
    teacher = account('sample-editor')
    workspace = client_workspace(teacher)
    with store.using(workspace):
        save_fixture()
        state = store.get_state('szu-1'); state['controls'][0]['label'] = '我的改点'
        store.save_state('szu-1', state)
    refused = teacher.post('/api/auth/claim-original', json={})
    assert refused.status_code == 400
    assert client_workspace(teacher) == workspace
    assert teacher.get('/api/maps/szu-1').json()['controls'][0]['label'] == '我的改点'


def test_untouched_public_samples_can_be_replaced_when_claiming_original(server, tmp_path):
    public_sample_bundle(tmp_path)
    teacher = account('clean-starter')
    assert teacher.get('/api/maps/szu-1').json()['processed'] is True
    result = teacher.post('/api/auth/claim-original', json={})
    assert result.status_code == 200 and result.json()['user']['ownsOriginalWorkbench'] is True


def test_a_broken_public_bundle_does_not_fall_back_to_private_starters(server):
    asset = store.builtin_maps()[0]
    save_fixture(asset['id'], asset['sha256'])
    state = store.get_state(asset['id']); state['controls'][0]['label'] = 'private starter'
    store.save_state(asset['id'], state)
    samples.BUNDLE.write_text('invalid bundle')
    newcomer = account('no-private-fallback')
    assert newcomer.get(f'/api/maps/{asset["id"]}').json()['processed'] is False


def test_workbench_requests_and_images_reject_a_different_page_account(server):
    first, second = account('page-a'), account('page-b')
    asset = store.builtin_maps()[0]
    for client in (first, second):
        with store.using(client_workspace(client)):
            save_fixture(asset['id'], asset['sha256'])
    wrong = accounts.workbench_scope(accounts.session_user(first.cookies.get(accounts.COOKIE)))
    right = accounts.workbench_scope(accounts.session_user(second.cookies.get(accounts.COOKIE)))
    path = f'/api/maps/{asset["id"]}'
    body = {'version': 1, 'edits': [{'kind': 'close', 'pixels': [[10, 10]], 'radiusPx': 2}]}
    rejected = second.patch(path, json=body, headers={'X-DirMap-Workbench': wrong})
    assert rejected.status_code == 409 and rejected.json()['accountChanged'] is True
    assert second.get(path).json()['edits'] == []
    # Old browser tabs without an account binding cannot write using a newly
    # switched cookie; explicit API clients without Origin remain supported.
    assert second.patch(path, json=body, headers={'Origin': 'http://testserver'}).status_code == 409
    assert second.patch(path, json=body, headers={'Origin': 'http://testserver', 'X-DirMap-Workbench': right}).status_code == 200
    image = f'/assets/maps/{asset["id"]}/overlay.png'
    assert second.get(image, params={'workbench': wrong}).status_code == 409
    assert second.get(image, params={'workbench': wrong}, headers={'X-DirMap-Workbench': right}).status_code == 409
    visible = second.get(image, params={'workbench': right})
    assert visible.status_code == 200 and visible.headers['cache-control'] == 'no-store'
    assert second.get(path).headers['cache-control'] == 'no-store'


def test_a_write_waiting_for_the_data_lock_rechecks_its_workbench(server, monkeypatch):
    teacher = account('late-write')
    workspace = client_workspace(teacher)
    with store.using(workspace):
        save_fixture()
    with monkeypatch.context() as changed:
        changed.setattr(accounts, 'workspace_matches', lambda *args: False)
        response = teacher.patch('/api/maps/szu-1', json={'version': 1, 'edits': [{'kind': 'close', 'pixels': [[10, 10]], 'radiusPx': 2}]})
        assert response.status_code == 409 and response.json()['accountChanged'] is True
    assert teacher.get('/api/maps/szu-1').json()['edits'] == []


def test_sign_in_sign_out_password_change_and_throttling(server, monkeypatch):
    teacher = account('Teacher.Zhang', 'first password')
    assert account_status(teacher)['username'] == 'Teacher.Zhang'
    duplicate = TestClient(app).post('/api/auth/register', json={'username': 'teacher.zhang', 'password': 'another one'})
    assert duplicate.status_code == 400
    assert TestClient(app).post('/api/auth/register', json={'username': 'x', 'password': 'short'}).status_code == 400
    other = TestClient(app)
    assert other.post('/api/auth/login', json={'username': 'teacher.zhang', 'password': 'wrong'}).status_code == 400
    assert other.post('/api/auth/login', json={'username': 'TEACHER.ZHANG', 'password': 'first password'}).status_code == 200
    changed = teacher.post('/api/auth/password', json={'oldPassword': 'first password', 'newPassword': 'second password'})
    assert changed.status_code == 200
    # Other sessions end; this one stays signed in.
    assert account_status(other) is None and account_status(teacher)['username'] == 'Teacher.Zhang'
    assert teacher.post('/api/auth/logout').status_code == 200 and account_status(teacher) is None
    monkeypatch.setattr(accounts, 'NAME_LIMIT', (3, 900))
    attempt = TestClient(app)
    for _ in range(3):
        assert attempt.post('/api/auth/login', json={'username': 'teacher.zhang', 'password': 'nope'}).status_code == 400
    blocked = attempt.post('/api/auth/login', json={'username': 'teacher.zhang', 'password': 'second password'})
    assert blocked.status_code == 400 and '过多' in blocked.json()['detail']


def account_status(client):
    return client.get('/api/auth/me').json()['user']


def test_session_cookie_is_http_only_and_cross_site_writes_are_refused(server):
    import stat
    response = TestClient(app).post('/api/auth/register', json={'username': 'cookie-check', 'password': 'correct horse'})
    assert stat.S_IMODE(os.stat(store.DATA/'accounts.sqlite3').st_mode) == 0o600
    cookie = response.headers['set-cookie'].lower()
    assert 'httponly' in cookie and 'samesite=lax' in cookie
    teacher = account('cross-site')
    assert teacher.post('/api/maps/upload', files={'file': ('a.png', png_bytes(), 'image/png')},
                        headers={'Origin': 'https://elsewhere.example'}).status_code == 403


def test_uploads_stop_before_the_data_disk_fills(server, monkeypatch):
    import collections
    import shutil
    teacher = account('disk-check')
    usage = collections.namedtuple('usage', 'total used free')
    monkeypatch.setattr(shutil, 'disk_usage', lambda path: usage(10*1024**3, 9*1024**3, 1024**3))
    refused = teacher.post('/api/maps/upload', files={'file': ('a.png', png_bytes(), 'image/png')})
    assert refused.status_code == 422 and '存储空间不足' in refused.json()['detail']
    assert [m for m in teacher.get('/api/catalog').json()['maps'] if not m.get('builtin')] == []
