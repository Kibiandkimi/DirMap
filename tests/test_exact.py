"""Independent permutation/partition oracles for the bounded circuit solver."""

import itertools
import math
import time

import numpy as np
import pytest

from dirmap.exact import optimize
from dirmap.optimizer import solve, subset_tours
from dirmap import exact, optimizer
from dirmap.route_scores import RouteScores, improves


def model_score(matrix, controls, teams, routes, objective):
    times, distances = [], []
    for team, route in zip(teams, routes):
        edges = list(zip(route, route[1:]))
        distances.append(sum(round(matrix[a, b]*1000) for a, b in edges))
        times.append(sum(math.ceil(matrix[a, b]/team['speedMps']*1000) for a, b in edges)
                     +sum(math.ceil((controls[i-1].get('serviceBaseSec', 5)
                                     +team['people']*controls[i-1].get('servicePerPersonSec', 1))*1000)
                          for i in route[1:-1])
                     +math.ceil(team.get('departureSec', 0)*1000))
    return (sum(distances),) if objective == 'min_distance' else (sum(times),) if objective == 'min_total_time' else (max(times), sum(times))


@pytest.mark.parametrize('objective', ['min_max_finish_then_sum', 'min_total_time', 'min_distance'])
def test_circuit_partition_matches_complete_independent_enumeration(objective):
    rng = np.random.default_rng(67)
    matrix = rng.integers(1, 80, (7, 7)).astype(float)  # deliberately directed/non-Euclidean
    np.fill_diagonal(matrix, 0)
    controls = [{'id': str(i), 'serviceBaseSec': i+1, 'servicePerPersonSec': i % 3} for i in range(5)]
    teams = [{'id': 'slow', 'people': 5, 'speedMps': 1., 'departureSec': 0},
             {'id': 'fast', 'people': 2, 'speedMps': 2., 'departureSec': 8}]
    best = None
    for sequence in itertools.permutations(range(1, 6)):
        for split in range(1, 5):
            routes = [[0, *sequence[:split], 6], [0, *sequence[split:], 6]]
            score = model_score(matrix, controls, teams, routes, objective)
            best = score if best is None or score < best else best
    result = optimize(matrix, controls, teams, 'split_coverage', objective,
                      [[0, 1, 2, 6], [0, 3, 4, 5, 6]], time.perf_counter()+5)
    assert result['proven']
    assert model_score(matrix, controls, teams, result['sequences'], objective) == best
    np.testing.assert_allclose(result['proof']['modelObjectiveValue'], np.asarray(best)/1000)
    assert result['proof']['bestBound'] == result['proof']['modelObjectiveValue']
    assert result['proof']['relativeGap'] == 0
    assert len(result['proof']['stages']) == (2 if objective == 'min_max_finish_then_sum' else 1)


def test_all_visit_circuit_matches_directed_tour_oracle_and_distinct_endpoints():
    rng = np.random.default_rng(513)
    matrix = rng.integers(1, 100, (8, 8)).astype(float)
    np.fill_diagonal(matrix, 0)
    controls = [{'id': str(i)} for i in range(6)]
    teams = [{'id': 'a', 'people': 6, 'speedMps': 1.3, 'departureSec': 0},
             {'id': 'b', 'people': 3, 'speedMps': 2.1, 'departureSec': 25}]
    best = min(sum(matrix[a, b] for a, b in zip([0, *p], [*p, 7]))
               for p in itertools.permutations(range(1, 7)))
    result = optimize(matrix, controls, teams, 'all_visit', 'min_distance',
                      [list(range(8))]*2, time.perf_counter()+5)
    assert result['proven']
    for route in result['sequences']:
        assert route[0] == 0 and route[-1] == 7
        assert sorted(route[1:-1]) == list(range(1, 7))
        assert sum(matrix[a, b] for a, b in zip(route, route[1:])) == best
    assert result['proof']['modelObjectiveValue'] == [best*2]


def test_expired_exact_budget_does_not_claim_infeasibility_or_discard_incumbent():
    matrix = np.ones((18, 18)); np.fill_diagonal(matrix, 0)
    controls = [{'id': str(i)} for i in range(16)]
    teams = [{'id': 't', 'people': 1, 'speedMps': 1}]
    assert optimize(matrix, controls, teams, 'all_visit', 'min_distance', [list(range(18))], time.perf_counter()-1) is None
    with pytest.raises(TimeoutError):
        subset_tours(matrix, deadline=time.perf_counter()-1)
    # Even a tiny budget returns all mandatory controls, without a spurious
    # optimality proof or an INFEASIBLE label for an interrupted search.
    result = solve(matrix, controls, teams, time_limit=.00001)
    assert result['status'] == 'FEASIBLE'
    assert not result['proof']['globalOptimalityProven']
    assert sorted(result['routes'][0]['controlIds']) == sorted(c['id'] for c in controls)


def test_solver_never_reports_raw_float_optimality_for_quantized_proof():
    matrix = np.array([[0, 1.00051, 1.00049, 3], [1, 0, 2.00002, 1],
                       [1, 2, 0, 1], [3, 1, 1, 0]])
    controls = [{'id': 'a'}, {'id': 'b'}]
    teams = [{'id': 't', 'people': 1, 'speedMps': 1}]
    result = optimize(matrix, controls, teams, 'all_visit', 'min_distance', [[0, 1, 2, 3]], time.perf_counter()+2)
    assert result['proof']['quantization']['distanceM'] == .001
    assert '量化模型' in result['proof']['scope']


def rounding_reversal():
    matrix = np.full((4, 4), 10., float)
    np.fill_diagonal(matrix, 0)
    for a, b in ((0, 1), (1, 2), (2, 3)):
        matrix[a, b] = 1.00049
    matrix[0, 2], matrix[2, 1], matrix[1, 3] = 1.00051, 1., 1.
    controls = [{'id': str(i), 'serviceBaseSec': 0, 'servicePerPersonSec': 0} for i in range(2)]
    return matrix, controls


@pytest.mark.parametrize('objective', ['min_max_finish_then_sum', 'min_total_time', 'min_distance'])
def test_better_actual_incumbent_survives_a_different_proven_rounded_tour(objective):
    matrix, controls = rounding_reversal()
    teams = [{'id': 'a', 'people': 1, 'speedMps': 1},
             {'id': 'b', 'people': 2, 'speedMps': 1.3, 'departureSec': 2}]
    seed = [[0, 2, 1, 3], [0, 2, 1, 3]]
    result = optimize(matrix, controls, teams, 'all_visit', objective, seed, time.perf_counter()+2)
    assert result['sequences'] == seed
    assert not result['proven']
    proof = result['proof']
    assert proof['searchProvenOptimal'] and proof['solutionSource'] == 'incumbent'
    assert proof['termination'] == 'BETTER_ACTUAL_SOLUTION_RETAINED'
    assert proof['bestBound'] == proof['searchModelObjectiveValue']
    assert proof['modelObjectiveValue'] > proof['bestBound']
    if objective == 'min_max_finish_then_sum':
        assert proof['relativeGap'] is None
    else:
        assert proof['relativeGap'] > 0
    assert '另一方案' in proof['scope']
    assert seed == [[0, 2, 1, 3], [0, 2, 1, 3]]


def test_returned_plan_does_not_inherit_another_routes_optimality(monkeypatch):
    matrix, controls = rounding_reversal()
    teams = [{'id': 'one', 'people': 1, 'speedMps': 1}]
    # Exercise the same circuit fallback used when the small exact DP runs
    # out of budget, while giving the circuit enough time for a real proof.
    def expired(*args, **kwargs):
        raise TimeoutError('test DP interruption')
    monkeypatch.setattr(optimizer, 'subset_tours', expired)
    result = solve(matrix, controls, teams, 'all_visit', 'min_distance', 2,
                   initial_sequences=[[0, 2, 1, 3]])
    assert result['routes'][0]['order'] == [0, 2, 1, 3]
    assert result['status'] == 'FEASIBLE'
    assert not result['proof']['globalOptimalityProven']
    assert result['proof']['objectiveValue'] == pytest.approx([3.00051])
    assert result['proof']['modelObjectiveValue'] == [3.001]
    assert result['proof']['bestBound'] == [3.]


def test_partial_first_objective_cannot_worsen_the_second_objective(monkeypatch):
    matrix = np.random.default_rng(1).integers(1, 80, (7, 7)).astype(float)
    np.fill_diagonal(matrix, 0)
    controls = [{'id': str(i), 'serviceBaseSec': 0, 'servicePerPersonSec': 0} for i in range(5)]
    teams = [{'id': str(i), 'people': 2, 'speedMps': 1} for i in range(2)]
    seed = [[0, 5, 3, 6], [0, 1, 2, 4, 6]]
    run = exact._run
    calls = []
    def first_stage_only(model, deadline):
        solver, status = run(model, deadline)
        calls.append(status)
        # Stop after the real first-stage solver result, without spending a
        # second-stage budget. Its incumbent remains a real feasible route.
        return solver, exact.cp_model.FEASIBLE if status == exact.cp_model.OPTIMAL else status
    monkeypatch.setattr(exact, '_run', first_stage_only)
    result = optimize(matrix, controls, teams, 'split_coverage', 'min_max_finish_then_sum', seed, time.perf_counter()+2)
    assert len(calls) == 1 and calls[0] in (exact.cp_model.FEASIBLE, exact.cp_model.OPTIMAL)
    assert model_score(matrix, controls, teams, result['sequences'], 'min_max_finish_then_sum') <= (115000, 219000)
    assert result['proof']['modelObjectiveValue'] <= [115., 219.]
    assert not result['proven']


def test_primary_improvement_may_increase_the_secondary_sum():
    matrix = np.random.default_rng(1).integers(1, 80, (7, 7)).astype(float)
    np.fill_diagonal(matrix, 0)
    controls = [{'id': str(i), 'serviceBaseSec': 0, 'servicePerPersonSec': 0} for i in range(5)]
    teams = [{'id': str(i), 'people': 2, 'speedMps': 1} for i in range(2)]
    candidates = []
    for order in itertools.permutations(range(1, 6)):
        for cut in range(1, 5):
            routes = [[0, *order[:cut], 6], [0, *order[cut:], 6]]
            candidates.append((model_score(matrix, controls, teams, routes, 'min_max_finish_then_sum'), routes))
    best, _ = min(candidates)
    original, seed = min((score, routes) for score, routes in candidates if score[0] > best[0] and score[1] < best[1])
    result = optimize(matrix, controls, teams, 'split_coverage', 'min_max_finish_then_sum', seed, time.perf_counter()+2)
    assert result['proven']
    assert model_score(matrix, controls, teams, result['sequences'], 'min_max_finish_then_sum') == best
    assert result['proof']['modelObjectiveValue'][1] > original[1]/1000


def test_zero_makespan_incumbent_remains_a_valid_two_stage_model():
    matrix = np.zeros((5, 5))
    controls = [{'id': str(i), 'serviceBaseSec': 0, 'servicePerPersonSec': 0} for i in range(3)]
    teams = [{'id': str(i), 'people': 1, 'speedMps': 1} for i in range(2)]
    result = optimize(matrix, controls, teams, 'split_coverage', 'min_max_finish_then_sum',
                      [[0, 1, 4], [0, 2, 3, 4]], time.perf_counter()+1)
    assert result['proven']
    assert result['proof']['modelObjectiveValue'] == [0, 0]


@pytest.mark.parametrize('objective', ['min_max_finish_then_sum', 'min_total_time', 'min_distance'])
def test_refinement_preserves_actual_incumbent_with_rendezvous(objective):
    matrix = np.random.default_rng(702).uniform(0, 30, (7, 7))
    np.fill_diagonal(matrix, 0)
    controls = [{'id': str(i), 'serviceBaseSec': i/7, 'servicePerPersonSec': i/11} for i in range(5)]
    teams = [{'id': 'a', 'people': 4, 'speedMps': .9, 'departureSec': 0},
             {'id': 'b', 'people': 2, 'speedMps': 1.3, 'departureSec': 7}]
    service = [[c['serviceBaseSec']+team['people']*c['servicePerPersonSec'] for c in controls] for team in teams]
    seed = [[0, 1, 2, 4, 6], [0, 2, 3, 4, 5, 6]]
    before = RouteScores(matrix, seed, teams, service, objective, (2, 4)).score()
    result = optimize(matrix, controls, teams, 'mixed_coverage', objective, seed, time.perf_counter()+.3,
                      mandatory_nodes=(2, 4), ordered_nodes=(2, 4))
    after = RouteScores(matrix, result['sequences'], teams, service, objective, (2, 4)).score()
    assert not improves(before, after)
    assert all(tuple(node for node in route if node in (2, 4)) == (2, 4) for route in result['sequences'])
