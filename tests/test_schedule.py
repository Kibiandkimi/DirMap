"""Hand-calculated class barriers and teacher-prescribed routes."""
import numpy as np
import pytest

from dirmap.optimizer import solve
from dirmap.schedule import custom_sequences, apply_schedule


def course():
    points = np.array([0, 10, 20, 30, 40, 0])
    matrix = np.abs(points[:, None] - points[None, :]).astype(float)
    controls = [{'id': name} for name in ('m1', 'm2', 'p', 'q')]
    teams = [{'id': 'fast', 'people': 3, 'speedMps': 2, 'departureSec': 0},
             {'id': 'slow', 'people': 5, 'speedMps': 1, 'departureSec': 10}]
    custom = [{'teamId': 'slow', 'controlIds': ['q', 'm1', 'm2']},
              {'teamId': 'fast', 'controlIds': ['m1', 'p', 'm2']}]
    return matrix, controls, teams, custom


def test_custom_order_waits_for_every_team_and_accumulates_wait_at_each_meeting():
    matrix, controls, teams, custom = course()
    result = solve(matrix, controls, teams, 'custom', mandatory_ids=['m1', 'm2'],
                   mandatory_order=True, custom_routes=custom)
    fast, slow = result['routes']
    assert result['status'] == 'FIXED' and not result['proof']['globalOptimalityProven']
    assert fast['controlIds'] == ['m1', 'p', 'm2']
    assert slow['controlIds'] == ['q', 'm1', 'm2']
    assert result['coverage']['synchronization'] == 'wait_for_all'
    assert [r['waitSec'] for r in (fast, slow)] == [87, 11]
    assert [r['finishSec'] for r in (fast, slow)] == [141, 151]
    assert result['proof']['objectiveValue'] == [151, 292]
    assert result['rendezvous'] == [
        {'controlId': 'm1', 'allArrivedSec': 90, 'departureSec': 100},
        {'controlId': 'm2', 'allArrivedSec': 123, 'departureSec': 131}]
    for route in (fast, slow):
        assert route['durationSec'] == route['travelSec'] + route['serviceSec'] + route['waitSec']
        assert route['finishSec'] == route['departureSec'] + route['durationSec']
        for stop in route['stops']:
            assert stop['readySec'] == stop['arrivalSec'] + stop['serviceSec']
            assert stop['departureSec'] == stop['readySec'] + stop['waitSec']
    assert fast['stops'][0]['arrivalSec'] == 5
    assert slow['stops'][2]['arrivalSec'] == 110


def test_public_schedule_uses_the_actual_expanded_legs_and_retains_every_stop():
    matrix, controls, teams, custom = course()
    result = solve(matrix, controls, teams, 'custom', mandatory_ids=['m1', 'm2'], custom_routes=custom)
    for route in result['routes']:
        route['legs'] = [{'travelWeightM': matrix[a, b]} for a, b in zip(route['order'], route['order'][1:])]
    result['routes'][0]['legs'][0]['travelWeightM'] = 210  # expanded first leg takes 100 seconds longer
    meetings = apply_schedule(result['routes'], controls, teams, ['m1', 'm2'])
    assert meetings[0]['departureSec'] == 113
    assert meetings[1]['departureSec'] == 144
    assert [r['finishSec'] for r in result['routes']] == [154, 164]
    assert [r['waitSec'] for r in result['routes']] == [0, 24]
    assert all(route['stops'][-1]['controlId'] == 'finish' for route in result['routes'])


@pytest.mark.parametrize('routes, message', [
    ([{'teamId': 'fast', 'controlIds': ['m1', 'p', 'm2', 'q']}], '每支队伍'),
    ([{'teamId': 'fast', 'controlIds': ['m1', 'p', 'm2']}, {'teamId': 'slow', 'controlIds': ['m2', 'q', 'm1']}], '顺序'),
    ([{'teamId': 'fast', 'controlIds': ['m1', 'p', 'm2']}, {'teamId': 'slow', 'controlIds': ['q', 'm1']}], '遗漏'),
    ([{'teamId': 'fast', 'controlIds': ['m1', 'm2']}, {'teamId': 'slow', 'controlIds': ['q', 'm1', 'm2']}], '覆盖'),
    ([{'teamId': 'fast', 'controlIds': ['m1', 'p', 'p', 'm2']}, {'teamId': 'slow', 'controlIds': ['q', 'm1', 'm2']}], '重复'),
    ([{'teamId': 'fast', 'controlIds': ['m1', 'p', 'x', 'm2']}, {'teamId': 'slow', 'controlIds': ['q', 'm1', 'm2']}], '不存在'),
])
def test_custom_routes_refuse_missing_points_duplicate_visits_and_meeting_deadlocks(routes, message):
    with pytest.raises(ValueError, match=message):
        custom_sequences(['m1', 'm2', 'p', 'q'], ['fast', 'slow'], routes, ['m1', 'm2'])


def test_custom_without_rendezvous_keeps_the_teams_independent():
    matrix, controls, teams, custom = course()
    result = solve(matrix, controls, teams, 'custom', custom_routes=custom)
    assert result['rendezvous'] == []
    assert [r['waitSec'] for r in result['routes']] == [0, 0]
    assert [r['finishSec'] for r in result['routes']] == [54, 140]
