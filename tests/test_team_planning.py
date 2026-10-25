"""Team-count benefits checked against exact examples and independent tours."""
from collections import Counter
import itertools

import numpy as np
import pytest

from dirmap import team_planning
from dirmap.team_planning import choose_count, make_teams, recommend_teams, team_counts


def settings(**overrides):
    return {'mode': 'all_visit', 'objective': 'min_max_finish_then_sum', 'totalPeople': 20,
            'minPeoplePerTeam': 2, 'maxTeams': 10, 'speedMps': 1., 'staggerSec': 0.,
            'targetGainFraction': .8, **overrides}


def measure(matrix):
    def evaluate(result, teams):
        return {'latestFinishSec': max(r['finishSec'] for r in result['routes']),
                'totalFinishSec': sum(r['finishSec'] for r in result['routes']),
                'totalWaitSec': sum(r['waitSec'] for r in result['routes']),
                'totalDistanceM': float(sum(matrix[a, b] for r in result['routes'] for a, b in zip(r['order'], r['order'][1:])))}
    return evaluate


def test_all_visit_compares_every_count_with_one_tour_and_selects_fewest_for_target_gain(monkeypatch):
    matrix = np.array([[0, 10, 0], [10, 0, 10], [0, 10, 0]], float)
    controls = [{'id': 'c', 'serviceBaseSec': 0, 'servicePerPersonSec': 1}]
    calls = []
    solve = team_planning.solve
    def counted(*args, **kwargs):
        calls.append(1)
        return solve(*args, **kwargs)
    monkeypatch.setattr(team_planning, 'solve', counted)
    plan, teams = recommend_teams(matrix, controls, settings(), 2, measure(matrix))
    result = plan['teamRecommendation']
    assert len(calls) == 1
    assert len(teams) == result['recommendedCount'] == 4
    assert result['bestMetricCount'] == 10
    assert result['retainedGainFraction'] == pytest.approx(15/18)
    assert result['gainPercent'] == 37.5
    assert result['evaluatedCounts'] == list(range(1, 11))
    assert result['allCountsCompared'] and result['allCountsOptimalityProven']
    assert [r['latestFinishSec'] for r in result['candidates']] == [40, 30, 27, 25, 24, 24, 23, 23, 23, 22]
    assert sum(t['people'] for t in teams) == 20 and all(t['people'] >= 2 for t in teams)


@pytest.mark.parametrize('overrides', [{'staggerSec': 30}, {'objective': 'min_distance'}, {'objective': 'min_total_time'}])
def test_no_gain_does_not_force_more_teams(overrides):
    matrix = np.array([[0, 10, 0], [10, 0, 10], [0, 10, 0]], float)
    controls = [{'id': 'c', 'serviceBaseSec': 0, 'servicePerPersonSec': 1}]
    plan, teams = recommend_teams(matrix, controls, settings(**overrides), 1, measure(matrix))
    assert len(teams) == 1 and plan['teamRecommendation']['noMeaningfulGain']
    assert len(plan['teamRecommendation']['candidates']) == 10


def test_non_monotone_benefit_is_not_cut_off_at_first_disappointing_count():
    rows = [{'teamCount': k, 'latestFinishSec': time, 'totalFinishSec': time*k, 'totalDistanceM': 100*k}
            for k, time in enumerate([100, 80, 85, 50, 48], 1)]
    selected = choose_count(rows, 'min_max_finish_then_sum', .9)
    assert selected['recommendedCount'] == 4 and selected['bestMetricCount'] == 5
    assert rows[2]['marginalGain'] == -5


def test_small_split_comparison_matches_every_independent_partition_and_permutation(monkeypatch):
    points = np.array([[0, 0], [2, 4], [9, 1], [7, 7], [-2, 5], [0, 0]], float)
    matrix = np.linalg.norm(points[:, None]-points[None, :], axis=2)
    controls = [{'id': str(i), 'serviceBaseSec': i+1, 'servicePerPersonSec': .5} for i in range(4)]
    request = settings(mode='split_coverage', totalPeople=8, maxTeams=4)
    calls = []
    original = team_planning.subset_tours
    def cached(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(team_planning, 'subset_tours', cached)
    plan, _ = recommend_teams(matrix, controls, request, 4, measure(matrix))
    assert len(calls) == 1
    for row in plan['teamRecommendation']['candidates']:
        count = row['teamCount']
        teams = make_teams(request, count)
        best = (float('inf'), float('inf'))
        for order in itertools.permutations(range(1, 5)):
            for cuts in itertools.combinations(range(1, 4), count-1):
                offsets = (0, *cuts, 4)
                times = []
                for k, team in enumerate(teams):
                    route = (0, *order[offsets[k]:offsets[k+1]], 5)
                    travel = sum(matrix[a, b] for a, b in zip(route, route[1:]))
                    service = sum(controls[n-1]['serviceBaseSec']+team['people']*controls[n-1]['servicePerPersonSec'] for n in route[1:-1])
                    times.append(travel+service)
                best = min(best, (max(times), sum(times)))
        assert row['optimalityProven']
        assert (row['latestFinishSec'], row['totalFinishSec']) == pytest.approx(best)


def test_mixed_recommendation_keeps_every_team_compulsory_and_shared_points_exactly_once():
    points = np.array([[0, 0], [2, 0], [9, 2], [6, 5], [2, 7], [0, 0]], float)
    matrix = np.linalg.norm(points[:, None]-points[None, :], axis=2)
    controls = [{'id': str(i)} for i in range(4)]
    request = settings(mode='mixed_coverage', totalPeople=15, minPeoplePerTeam=3, maxTeams=5,
                       mandatoryControlIds=['0', '2'], mandatoryOrder=True)
    plan, teams = recommend_teams(matrix, controls, request, 3, measure(matrix))
    assert plan['teamRecommendation']['evaluatedCounts'] == [1, 2, 3, 4, 5]
    counts = Counter(c for route in plan['routes'] for c in route['controlIds'])
    assert counts == Counter({'0': len(teams), '2': len(teams), '1': 1, '3': 1})
    assert all([c for c in r['controlIds'] if c in ('0', '2')] == ['0', '2'] for r in plan['routes'])


def test_unproven_tour_does_not_turn_into_proven_team_counts(monkeypatch):
    matrix = np.ones((4, 4)); np.fill_diagonal(matrix, 0)
    controls = [{'id': 'a'}, {'id': 'b'}]
    solve = team_planning.solve
    monkeypatch.setattr(team_planning, 'solve', lambda *a, **kw: solve(*a, **kw, use_exact=False))
    result, _ = recommend_teams(matrix, controls, settings(), .02, measure(matrix))
    assert not result['teamRecommendation']['allCountsOptimalityProven']
    assert all(not row['optimalityProven'] for row in result['teamRecommendation']['candidates'])
    assert not result['proof']['globalOptimalityProven']


def test_feasible_count_range_respects_people_coverage_and_departure_limits():
    assert team_counts(3, 2, 30, 10, 'all_visit') == [1]
    assert team_counts(30, 2, 30, 2, 'split_coverage') == [1, 2]
    assert team_counts(30, 2, 30, 2, 'mixed_coverage', 43200) == [1, 2, 3]
    for values in ((2, 3, 5, 2, 'all_visit'), (True, 1, 5, 2, 'all_visit'), (20, 2, 10, 5, 'custom')):
        with pytest.raises(ValueError):
            team_counts(*values)
