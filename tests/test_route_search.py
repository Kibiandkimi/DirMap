"""Independent matching oracles, legal swaps and retained incumbents."""
import itertools
import time

import numpy as np
import pytest

from dirmap.optimizer import evaluate_routes, solve
from dirmap.route_scores import RouteScores
from dirmap.route_search import class_search, improve_routes, singleton_assignment


@pytest.mark.parametrize('objective', ['min_max_finish_then_sum', 'min_total_time', 'min_distance'])
def test_single_visit_assignment_matches_all_permutations(objective):
    matrix = np.random.default_rng(440).integers(0, 80, (7, 7)).astype(float)
    np.fill_diagonal(matrix, 0)
    controls = [{'id': str(i), 'serviceBaseSec': i, 'servicePerPersonSec': i % 3} for i in range(5)]
    teams = [{'id': str(i), 'people': i+1, 'speedMps': 1+i/4, 'departureSec': i*3} for i in range(5)]
    def score(order):
        times = [team['departureSec']+matrix[0, node]/team['speedMps']+controls[node-1]['serviceBaseSec']
                 +team['people']*controls[node-1]['servicePerPersonSec']+matrix[node, 6]/team['speedMps']
                 for team, node in zip(teams, order)]
        distance = sum(matrix[0, node]+matrix[node, 6] for node in order)
        return (distance,) if objective == 'min_distance' else (sum(times),) if objective == 'min_total_time' else (max(times), sum(times))
    best = min(score(order) for order in itertools.permutations(range(1, 6)))
    result = solve(matrix, controls, teams, 'split_coverage', objective, .001, use_exact=False)
    assert result['status'] == 'OPTIMAL'
    assert score([route['order'][1] for route in result['routes']]) == pytest.approx(best)
    assert result['proof']['objectiveValue'] == pytest.approx(best)


def test_zero_cost_assignments_are_edges_not_missing_entries():
    matrix = np.zeros((5, 5))
    teams = [{'id': str(i), 'people': 1, 'speedMps': 1} for i in range(3)]
    controls = [{'id': str(i), 'serviceBaseSec': 0, 'servicePerPersonSec': 0} for i in range(3)]
    result = solve(matrix, controls, teams, 'split_coverage', time_limit=.001)
    assert result['proof']['objectiveValue'] == [0, 0]
    assert sorted(route['controlIds'][0] for route in result['routes']) == ['0', '1', '2']


def test_singleton_swaps_improve_without_ever_emptying_a_team():
    points = np.array([[0, 0], [20, 0], [2, 0], [0, 0]], float)
    matrix = np.linalg.norm(points[:, None]-points[None, :], axis=2)
    teams = [{'speedMps': 1}, {'speedMps': 4}]
    routes = [[0, 1, 3], [0, 2, 3]]
    service = [[0, 0], [0, 0]]
    result = improve_routes(matrix, routes, teams, service, 'min_max_finish_then_sum', (), time.perf_counter()+1)
    assert result == [[0, 2, 3], [0, 1, 3]]
    assert routes == [[0, 1, 3], [0, 2, 3]]


def test_single_team_mixed_order_uses_exact_tour_and_keeps_compulsory_order():
    rng = np.random.default_rng(62)
    matrix = rng.integers(1, 70, (8, 8)).astype(float); np.fill_diagonal(matrix, 0)
    controls = [{'id': str(i), 'serviceBaseSec': 0, 'servicePerPersonSec': 0} for i in range(6)]
    teams = [{'id': 'one', 'people': 2, 'speedMps': 1}]
    result = solve(matrix, controls, teams, 'mixed_coverage', time_limit=2, mandatory_ids=['1', '4'], mandatory_order=True)
    best = min(sum(matrix[a, b] for a, b in zip((0, *order, 7), (0, *order, 7)[1:]))
               for order in itertools.permutations(range(1, 7)) if order.index(2) < order.index(5))
    assert result['status'] == 'OPTIMAL' and result['proof']['algorithm'] == 'held_karp_ordered'
    assert result['routes'][0]['finishSec'] == pytest.approx(best)
    assert [c for c in result['routes'][0]['controlIds'] if c in ('1', '4')] == ['1', '4']


def test_warm_start_survives_expired_budget_and_is_not_mutated():
    rng = np.random.default_rng(932)
    points = rng.random((20, 2))*100
    matrix = np.linalg.norm(points[:, None]-points[None, :], axis=2)
    teams = [{'speedMps': 1, 'departureSec': 0}, {'speedMps': 2, 'departureSec': 9}]
    service = np.ones((2, 18))
    incumbent = [[0, *range(1, 10), 19], [0, *range(10, 19), 19]]
    saved = [r.copy() for r in incumbent]
    old = RouteScores(matrix, incumbent, teams, service, 'min_max_finish_then_sum').score()
    result = class_search(matrix, teams, service, 'min_max_finish_then_sum', (), (), time.perf_counter()-1, incumbent)
    assert RouteScores(matrix, result, teams, service, 'min_max_finish_then_sum').score() <= old
    assert incumbent == saved


def test_free_gathering_order_can_escape_the_nearest_first_order():
    points = np.random.default_rng(4).random((6, 2))*30
    matrix = np.linalg.norm(points[:, None]-points[None, :], axis=2)
    teams = [{'speedMps': 1, 'people': 2}, {'speedMps': 1.3, 'people': 5}]
    service = np.array([[2, 3, 4, 5], [3, 5, 4, 7]], float)
    candidates = []
    # Independently enumerate all assignments and common meeting orders,
    # then simulate releases without using RouteScores or schedule.timeline.
    for owners in itertools.product(range(2), repeat=2):
        options = []
        for team in range(2):
            visits = [1, 3]+[node for node, owner in zip((2, 4), owners) if owner == team]
            options.append([(0, *p, 5) for p in itertools.permutations(visits)])
        for routes in itertools.product(*options):
            common = tuple(n for n in routes[0] if n in (1, 3))
            if tuple(n for n in routes[1] if n in (1, 3)) != common:
                continue
            clocks, offsets = [0., 0.], [0, 0]
            for barrier in (*common, 5):
                for team, route in enumerate(routes):
                    end = route.index(barrier)
                    for i in range(offsets[team]+1, end+1):
                        a, b = route[i-1:i+1]
                        clocks[team] += matrix[a, b]/teams[team]['speedMps']
                        if b != 5:
                            clocks[team] += service[team, b-1]
                    offsets[team] = end
                if barrier != 5:
                    clocks = [max(clocks)]*2
            candidates.append(((max(clocks), sum(clocks)), common))
    oracle = min(score for score, _ in candidates)
    constrained = min(score for score, order in candidates if order == (3, 1))
    result = class_search(matrix, teams, service, 'min_max_finish_then_sum', (1, 3), (), time.perf_counter()+1)
    assert RouteScores(matrix, result, teams, service, 'min_max_finish_then_sum', (1, 3)).score() == pytest.approx(oracle)
    assert oracle < constrained
    assert all(tuple(n for n in r if n in (1, 3)) == (1, 3) for r in result)
    fixed = class_search(matrix, teams, service, 'min_max_finish_then_sum', (1, 3), (3, 1), time.perf_counter()+1)
    assert all(tuple(n for n in r if n in (1, 3)) == (3, 1) for r in fixed)


@pytest.mark.parametrize('routes', [
    [[0, 1, 4], [0, 1, 3, 4]],
    [[0, 1, 4], [0, 2, 5, 4]],
    [[0, 4], [0, 1, 2, 3, 4]],
    [[0, 1, 4]],
])
def test_invalid_incumbents_cannot_bypass_coverage_checks(routes):
    matrix = np.ones((5, 5)); np.fill_diagonal(matrix, 0)
    teams = [{'id': str(i), 'people': 2, 'speedMps': 1} for i in range(2)]
    with pytest.raises(ValueError):
        solve(matrix, [{'id': str(i)} for i in range(3)], teams, 'split_coverage', initial_sequences=routes)
