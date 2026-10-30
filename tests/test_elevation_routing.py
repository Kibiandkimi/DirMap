import math

import numpy as np
import pytest
from scipy.sparse import coo_matrix

from dirmap import elevation, elevation_routing
from dirmap.geometry import Frame, Registration
from dirmap.graph import RasterGraph, expand_route, plan_matrix


def hill_graph(monkeypatch):
    positions = np.array([[10, 10], [60, 10], [110, 10], [10, 50], [110, 50]], float)
    edges = [(0, 1, 50), (1, 2, 50), (0, 3, 40), (3, 4, 100), (4, 2, 40)]
    rows, cols, values = [], [], []
    for a, b, length in edges:
        rows.extend([a, b]); cols.extend([b, a]); values.extend([length, length])
    matrix = coo_matrix((values, (rows, cols)), shape=(5, 5)).tocsr()
    base = RasterGraph(matrix.copy(), matrix.copy(), positions, positions, np.ones((130, 130)), 'base', 4)
    class Hill:
        def sample(self, lnglat):
            points = Frame().to_local(lnglat)
            return np.maximum(0., 20-np.abs(points[:, 0]-60)*.4)*(points[:, 1] < 20)
    monkeypatch.setattr(elevation, 'Sampler', lambda records: Hill())
    frozen = {'enabled': True, 'revision': 'hill', 'models': [{}]}
    return base, elevation_routing.attach(base, {'frame': Frame().as_dict()}, frozen)


def test_grade_changes_the_actual_shortest_path_without_changing_legal_edges(monkeypatch):
    base, hilly = hill_graph(monkeypatch)
    _, original = base.shortest_paths([0], targets=[2])
    _, updated = hilly.shortest_paths([0], targets=[2])
    assert base.expand(original[0], 0, 2) == [0, 1, 2]
    assert hilly.expand(updated[0], 0, 2) == [0, 3, 4, 2]
    np.testing.assert_array_equal(base.weights.indices, hilly.weights.indices)
    assert base.weights[0, 1] == 50
    assert hilly.weights[0, 1] > hilly.weights[1, 0]
    assert hilly.base_content_hash == base.content_hash and hilly.content_hash != base.content_hash


def test_endpoint_connectors_and_expanded_travel_time_keep_their_direction(monkeypatch):
    _, graph = hill_graph(monkeypatch)
    bounds = [[0, 0], [129, 0], [129, 129], [0, 129]]
    registration = Registration([[1., 0., 0.], [0., 1., 0.]], bounds, bounds)
    bundle = plan_matrix(graph, [], [58, 10], [110, 10], registration)
    # The 2 m connector climbs 0.8 m, then a 50 m edge descends 20 m.
    forward = 2*math.exp(3.5*.4)+50*math.exp(3.5*.3)
    reverse = 2*math.exp(3.5*.3)+50*math.exp(3.5*.4)
    assert bundle['matrix'][0, 1] == pytest.approx(forward, abs=1e-6)
    assert bundle['matrix'][1, 0] == pytest.approx(reverse, abs=1e-6)
    result = expand_route(graph, bundle, [0, 1])
    assert result['travelWeightM'] == pytest.approx(forward, abs=1e-6)
    assert result['elevation']['coverageFraction'] == 1
    assert result['elevation']['ascentM'] == pytest.approx(.8, abs=.05)
    assert result['elevation']['descentM'] == 20


def test_missing_heights_do_not_turn_into_zero_height_cliffs(monkeypatch):
    base, _ = hill_graph(monkeypatch)
    class Missing:
        def sample(self, lnglat):
            return np.full(len(lnglat), np.nan)
    monkeypatch.setattr(elevation, 'Sampler', lambda records: Missing())
    graph = elevation_routing.attach(base, {}, {'enabled': True, 'revision': 'missing', 'models': [{}]})
    np.testing.assert_array_equal(graph.weights.data, base.weights.data)
    result = elevation_routing.profile(graph, [[10, 10], [60, 10]], ['ground', 'ground'])
    assert result['coverageFraction'] == 0 and result['ascentM'] is None
    assert result['profile'] == [{'distanceM': 0., 'heightM': None}, {'distanceM': 50., 'heightM': None}]
