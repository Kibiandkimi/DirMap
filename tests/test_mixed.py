"""Mixed compulsory/shared visits, checked against independent enumeration."""
from collections import Counter
import itertools
import time

import numpy as np
import pytest

from dirmap.exact import optimize
from dirmap.optimizer import solve


def score(matrix, routes, controls, teams, objective):
    distance = [sum(matrix[a, b] for a, b in zip(route, route[1:])) for route in routes]
    # Independent event simulation: propagate local work and release a
    # barrier only once every team has arrived and completed its service.
    finish = [team.get('departureSec', 0) for team in teams]
    offsets = [0]*len(teams)
    order = [n for n in routes[0] if n in (1, 3)]
    for barrier in [*order, 5]:
        for i, (team, route) in enumerate(zip(teams, routes)):
            end = route.index(barrier)
            for index in range(offsets[i]+1, end+1):
                a, b = route[index-1:index+1]
                finish[i] += matrix[a, b]/team['speedMps']
                if b != 5:
                    finish[i] += controls[b-1]['serviceBaseSec']+team['people']*controls[b-1]['servicePerPersonSec']
            offsets[i] = end
        if barrier != 5:
            finish = [max(finish)]*len(teams)
    return (sum(distance),) if objective == 'min_distance' else (sum(finish),) if objective == 'min_total_time' else (max(finish), sum(finish))


def oracle(matrix, controls, teams, ordered):
    # Every team visits 1 and 3; points 2 and 4 each go to exactly one team.
    candidates = []
    for assignment in itertools.product(range(len(teams)), repeat=2):
        options = []
        for team in range(len(teams)):
            selected = [1, 3]+[n for n, owner in zip((2, 4), assignment) if owner == team]
            routes = [[0, *p, 5] for p in itertools.permutations(selected) if not ordered or p.index(1) < p.index(3)]
            options.append(routes)
        candidates += [routes for routes in itertools.product(*options)
                       if all([n for n in r if n in (1, 3)] == [n for n in routes[0] if n in (1, 3)] for r in routes)]
    return candidates


@pytest.mark.parametrize('objective', ['min_max_finish_then_sum', 'min_total_time', 'min_distance'])
@pytest.mark.parametrize('ordered', [False, True])
def test_synchronized_routes_match_independent_partitions_permutations_and_events(objective, ordered):
    rng = np.random.default_rng(427)
    matrix = rng.integers(2, 100, (6, 6)).astype(float); np.fill_diagonal(matrix, 0)
    controls = [{'id': str(i), 'serviceBaseSec': i+1, 'servicePerPersonSec': i % 2} for i in range(1, 5)]
    teams = [{'id': 'a', 'people': 2, 'speedMps': 1., 'departureSec': 0},
             {'id': 'b', 'people': 5, 'speedMps': 2., 'departureSec': 7}]
    routes = oracle(matrix, controls, teams, ordered)
    best = min(score(matrix, r, controls, teams, objective) for r in routes)
    result = solve(matrix, controls, teams, 'mixed_coverage', objective, 5, ['1', '3'], ordered)
    assert result['status'] == 'OPTIMAL'
    assert score(matrix, [r['order'] for r in result['routes']], controls, teams, objective) == best
    exact = optimize(matrix, controls, teams, 'mixed_coverage', objective, routes[0], time.perf_counter()+5,
                     mandatory_nodes=(1, 3), ordered_nodes=(1, 3) if ordered else ())
    assert exact['proven']
    assert score(matrix, exact['sequences'], controls, teams, objective) == best


def test_identical_teams_may_have_identical_compulsory_tours_and_no_shared_points():
    matrix = np.array([[0, 5, 9, 0], [5, 0, 2, 4], [9, 2, 0, 8], [0, 4, 8, 0]], float)
    controls = [{'id': '31'}, {'id': '32'}]
    teams = [{'id': str(i), 'people': 3, 'speedMps': 1} for i in range(5)]
    result = solve(matrix, controls, teams, 'mixed_coverage', time_limit=2,
                   mandatory_ids=['31', '32'], mandatory_order=True)
    assert result['status'] == 'OPTIMAL'
    assert all(r['controlIds'] == ['31', '32'] for r in result['routes'])
    exact = optimize(matrix, controls, teams, 'mixed_coverage', 'min_total_time',
                     [[0, 1, 2, 3] for _ in teams], time.perf_counter()+3,
                     mandatory_nodes=(1, 2), ordered_nodes=(1, 2))
    assert exact['proven'] and all(route == [0, 1, 2, 3] for route in exact['sequences'])


def test_large_timed_out_mixed_plan_keeps_all_compulsory_and_shared_visits():
    rng = np.random.default_rng(387)
    points = rng.random((18, 2))*100
    matrix = np.linalg.norm(points[:, None]-points[None, :], axis=-1)
    controls = [{'id': str(i)} for i in range(16)]
    teams = [{'id': str(i), 'people': 4, 'speedMps': 1.4} for i in range(10)]
    compulsory = ['0', '7', '14']
    result = solve(matrix, controls, teams, 'mixed_coverage', time_limit=.00001,
                   mandatory_ids=compulsory, mandatory_order=True)
    assert result['status'] == 'FEASIBLE'
    for route in result['routes']:
        assert [c for c in route['controlIds'] if c in compulsory] == compulsory
        assert len(set(route['controlIds'])) == len(route['controlIds'])
    counts = Counter(c for r in result['routes'] for c in r['controlIds'])
    assert counts == Counter({c['id']: 10 if c['id'] in compulsory else 1 for c in controls})


def test_mixed_requires_selected_compulsory_ids():
    controls = [{'id': '31'}]; matrix = np.ones((3, 3)); np.fill_diagonal(matrix, 0)
    teams = [{'id': 't', 'people': 1, 'speedMps': 1}]
    for ids in ([], ['32'], ['31', '31']):
        with pytest.raises(ValueError):
            solve(matrix, controls, teams, 'mixed_coverage', mandatory_ids=ids)
