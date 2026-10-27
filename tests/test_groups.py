"""Groups joined with a temporary code share positions only while sharing."""
from fastapi.testclient import TestClient
import pytest

from dirmap import groups, store
from dirmap.app import app


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    monkeypatch.delenv('DIRMAP_ADMIN_TOKEN', raising=False)
    with TestClient(app) as client:
        yield client


def fix(lng, lat, at_ms, accuracy=6):
    return {'lnglat': [lng, lat], 'accuracyM': accuracy, 'timestampMs': at_ms}


def test_members_share_positions_messages_and_help_through_a_code(server, monkeypatch):
    clock = [1_000_000.0]
    monkeypatch.setattr(groups.time, 'time', lambda: clock[0])
    created = server.post('/api/groups', json={'name': '小王', 'title': '三队'})
    assert created.status_code == 200 and created.headers['cache-control'] == 'no-store'
    host = created.json()
    assert len(host['group']['code']) == 6 and host['group']['title'] == '三队' and host['members'][0]['host']
    joined = server.post('/api/groups/join', json={'code': ' '+host['group']['code'][:3]+' '+host['group']['code'][3:], 'name': '小李'})
    member = joined.json()
    assert member['groupId'] == host['groupId'] and [m['name'] for m in member['members']] == ['小王', '小李']
    sync = lambda who, **body: server.post(f'/api/groups/{who["groupId"]}/sync', json=body, headers={'X-DirMap-Member': who['token']})
    shared = sync(host, sharing=True, position=fix(113.9566, 22.5599, clock[0]*1000)).json()
    assert shared['members'][0]['position']['lnglat'] == [113.9566, 22.5599]
    seen = sync(member, sharing=False).json()
    assert seen['members'][0]['position']['accuracyM'] == 6 and seen['members'][1]['position'] is None
    # A wrong phone clock is replaced by the server's receipt time.
    shared = sync(host, sharing=True, position=fix(113.9567, 22.5598, 0)).json()
    assert shared['members'][0]['position']['at'] == clock[0]
    pin = server.post(f'/api/groups/{member["groupId"]}/messages', json={'kind': 'pin', 'text': '集合', 'lnglat': [113.957, 22.56]},
                      headers={'X-DirMap-Member': member['token']}).json()
    assert pin['messages'][-1]['kind'] == 'pin' and pin['messages'][-1]['name'] == '小李'
    help_state = server.post(f'/api/groups/{member["groupId"]}/messages', json={'kind': 'help', 'after': pin['messages'][-1]['seq']},
                             headers={'X-DirMap-Member': member['token']}).json()
    assert [m['kind'] for m in help_state['messages']] == ['help'] and help_state['members'][1]['helpAt'] == clock[0]
    later = sync(host, sharing=True, after=1).json()
    assert [m['kind'] for m in later['messages']] == ['help']
    # Turning sharing off removes the stored position immediately.
    stopped = sync(host, sharing=False).json()
    assert stopped['members'][0]['position'] is None
    with groups.database() as db:
        assert db.execute('SELECT lng, lat FROM members WHERE id=?', (host['memberId'],)).fetchone()[:] == (None, None)
    # Positions go stale after the time-to-live.
    sync(host, sharing=True, position=fix(113.9566, 22.5599, clock[0]*1000))
    clock[0] += groups.POSITION_TTL_SEC+1
    assert sync(member, sharing=False).json()['members'][0]['position'] is None


def test_host_locks_removes_and_disbands_and_others_cannot(server):
    host = server.post('/api/groups', json={'name': '组长'}).json()
    member = server.post('/api/groups/join', json={'code': host['group']['code'], 'name': '组员'}).json()
    manage = lambda who, **body: server.post(f'/api/groups/{who["groupId"]}/manage', json=body, headers={'X-DirMap-Member': who['token']})
    assert manage(member, action='lock').status_code == 422
    assert manage(host, action='lock').json()['group']['locked'] is True
    assert server.post('/api/groups/join', json={'code': host['group']['code'], 'name': '迟到'}).status_code == 422
    assert manage(host, action='remove', memberId=member['memberId']).status_code == 200
    gone = server.post(f'/api/groups/{member["groupId"]}/sync', json={}, headers={'X-DirMap-Member': member['token']})
    assert gone.status_code == 410
    assert manage(host, action='close').json() == {'closed': True}
    assert server.post(f'/api/groups/{host["groupId"]}/sync', json={}, headers={'X-DirMap-Member': host['token']}).status_code == 410
    # The code of a disbanded group is free again and no longer joins it.
    assert server.post('/api/groups/join', json={'code': host['group']['code'], 'name': '迟到'}).status_code == 422


def test_leaving_hands_hosting_over_and_a_wrong_token_is_refused(server):
    host = server.post('/api/groups', json={'name': '甲'}).json()
    member = server.post('/api/groups/join', json={'code': host['group']['code'], 'name': '乙'}).json()
    assert server.post(f'/api/groups/{host["groupId"]}/sync', json={}, headers={'X-DirMap-Member': 'forged'}).status_code == 410
    assert server.post(f'/api/groups/{host["groupId"]}/leave', headers={'X-DirMap-Member': host['token']}).json() == {'left': True}
    state = server.post(f'/api/groups/{member["groupId"]}/sync', json={}, headers={'X-DirMap-Member': member['token']}).json()
    assert [(m['name'], m['host']) for m in state['members']] == [('乙', True)]


def test_failed_codes_are_throttled_per_address(server, monkeypatch):
    monkeypatch.setattr(groups, 'JOIN_LIMIT', (3, 600))
    for _ in range(3):
        assert server.post('/api/groups/join', json={'code': '000000', 'name': '猜'}).json()['detail'] == '组队码不存在或已过期'
    host = server.post('/api/groups', json={'name': '甲'}).json()
    blocked = server.post('/api/groups/join', json={'code': host['group']['code'], 'name': '乙'})
    assert blocked.status_code == 422 and '过多' in blocked.json()['detail']


def test_groups_expire_after_idling(server, monkeypatch):
    clock = [5_000_000.0]
    monkeypatch.setattr(groups.time, 'time', lambda: clock[0])
    host = server.post('/api/groups', json={'name': '甲'}).json()
    clock[0] += groups.IDLE_SEC+1
    assert server.post('/api/groups/join', json={'code': host['group']['code'], 'name': '乙'}).status_code == 422
    with groups.database() as db:
        assert db.execute('SELECT COUNT(*) FROM members').fetchone()[0] == 0


def test_messages_are_validated_and_rate_limited(server, monkeypatch):
    monkeypatch.setattr(groups, 'MESSAGE_LIMIT', (2, 300))
    host = server.post('/api/groups', json={'name': '甲'}).json()
    send = lambda **body: server.post(f'/api/groups/{host["groupId"]}/messages', json=body, headers={'X-DirMap-Member': host['token']})
    assert send(kind='text', text='   ').status_code == 422
    assert send(kind='pin', text='无位置').status_code == 422
    assert send(kind='text', text='x'*500).status_code == 422
    assert send(kind='text', text='到了').status_code == 200
    assert send(kind='text', text='等等').status_code == 200
    assert '频繁' in send(kind='text', text='第三条').json()['detail']
    assert server.post('/api/groups/not-hex/sync', json={}).status_code == 404
