"""Incremental scoring must match an independent full timeline simulation."""
import numpy as np
import pytest

from dirmap.route_scores import RouteScores
from dirmap.schedule import timeline


@pytest.mark.parametrize('objective', ['min_max_finish_then_sum', 'min_total_time', 'min_distance'])
@pytest.mark.parametrize('mandatory', [(), (2, 5)])
def test_every_insertion_and_two_route_change_matches_full_recomputation(objective, mandatory):
    rng = np.random.default_rng(49)
    matrix = rng.uniform(1, 90, (9, 9)); np.fill_diagonal(matrix, 0)
    teams = [{'people': 2, 'speedMps': 1.2, 'departureSec': 7},
             {'people': 5, 'speedMps': 2, 'departureSec': 23},
             {'people': 3, 'speedMps': .8, 'departureSec': 0}]
    service = rng.uniform(1, 15, (3, 7))
    sequences = [[0, 1, 2, 5, 8], [0, 2, 3, 5, 8], [0, 2, 5, 4, 8]] if mandatory else [[0, 1, 2, 8], [0, 3, 5, 8], [0, 4, 8]]
    def full(routes):
        cost = sum(matrix[a, b] for route in routes for a, b in zip(route, route[1:]))
        times = timeline(routes, [matrix/t['speedMps'] for t in teams], service,
                         [t['departureSec'] for t in teams], mandatory)['finishSec']
        return (cost,) if objective == 'min_distance' else (sum(times),) if objective == 'min_total_time' else (max(times), sum(times))
    scores = RouteScores(matrix, sequences, teams, service, objective, mandatory)
    assert scores.score() == pytest.approx(full(sequences))
    for team, route in enumerate(sequences):
        for index in range(1, len(route)):
            modified = [seq.copy() for seq in sequences]
            modified[team].insert(index, 6)
            a, b = route[index-1:index+1]
            delta = matrix[a, 6]+matrix[6, b]-matrix[a, b]
            assert scores.insertion(team, scores.stages[team][index-1], delta, service[team, 5]) == pytest.approx(full(modified))
            assert scores.replacements({team: modified[team]}) == pytest.approx(full(modified))
    changed = [seq.copy() for seq in sequences]
    changed[0].remove(1); changed[2].insert(1, 1)
    assert scores.replacements({0: changed[0], 2: changed[2]}) == pytest.approx(full(changed))


def test_a_shorter_individual_tour_can_worsen_the_class_after_gathering():
    # Team B fixes the first release at 50 seconds. Visiting P after the
    # meeting shortens A's own walk but adds work after that shared release.
    points = np.array([[0, 0], [15, 5], [-20, 0], [10, 0], [15, 0], [20, 0]], float)
    matrix = np.linalg.norm(points[:, None]-points[None, :], axis=-1)
    teams = [{'speedMps': 1}, {'speedMps': 1}]
    routes = [[0, 1, 3, 5], [0, 2, 3, 4, 5]]
    scores = RouteScores(matrix, routes, teams, np.zeros((2, 4)), 'min_max_finish_then_sum', (3,))
    candidate = [0, 3, 1, 5]
    old_distance = sum(matrix[a, b] for a, b in zip(routes[0], routes[0][1:]))
    new_distance = sum(matrix[a, b] for a, b in zip(candidate, candidate[1:]))
    assert new_distance < old_distance
    assert scores.score() == pytest.approx((60, 120))
    assert scores.replacements({0: candidate}) == pytest.approx((50+np.sqrt(200), 110+np.sqrt(200)))
    assert scores.replacements({0: candidate}) > scores.score()
