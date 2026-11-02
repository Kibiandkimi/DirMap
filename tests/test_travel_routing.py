import copy
import math

import numpy as np
import pytest
from scipy.sparse import coo_matrix

from dirmap import elevation, elevation_routing, store, travel, travel_model
from dirmap.elevation_model import fingerprint
from dirmap.geometry import Frame, Registration
from dirmap.graph import RasterGraph, expand_route, plan_matrix


def setup_model(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    profile = {'corridors': [{'start': [0, 10], 'end': [120, 10], 'logFactor': math.log(1.8), 'tracks': 5}],
               'gradeLearned': True, 'coefficients': [5., -1., 5.], 'gradeRange': [-.15, .15]}
    model = {'schema': travel.SCHEMA, 'frame': Frame().as_dict(), 'profiles': {'walk': profile}, 'bbox': None}
    digest = fingerprint(model)
    record = {'artifact': 'travel-models/'+digest+'.json', 'fingerprint': digest}
    store.atomic_json(elevation.root()/record['artifact'], model)
    frozen = {'enabled': True, 'mode': 'walk', 'schema': travel.SCHEMA, 'revision': digest, 'models': [record]}
    positions = np.array([[10, 10], [60, 10], [110, 10], [10, 30], [110, 30]], float)
    edges = [(0, 1, 50), (1, 2, 50), (0, 3, 20), (3, 4, 100), (4, 2, 20)]
    rows, cols, values = [], [], []
    for a, b, length in edges:
        rows.extend([a, b]); cols.extend([b, a]); values.extend([length, length])
    matrix = coo_matrix((values, (rows, cols)), shape=(5, 5)).tocsr()
    base = RasterGraph(matrix.copy(), matrix.copy(), positions, positions, np.ones((140, 140)), 'base', 4)
    state = {'frame': Frame().as_dict()}
    graph = elevation_routing.attach(base, state, {'enabled': False, 'models': [], 'revision': 'disabled'})
    return base, travel.attach(graph, base, frozen), frozen


def test_learning_changes_the_actual_shortest_route_and_preserves_legal_edges(tmp_path, monkeypatch):
    base, graph, _ = setup_model(tmp_path, monkeypatch)
    _, parents = graph.shortest_paths([0], targets=[2])
    assert graph.expand(parents[0], 0, 2) == [0, 3, 4, 2]
    assert base.weights[0, 1] == 50 and graph.weights[0, 1] == pytest.approx(90)
    np.testing.assert_array_equal(base.weights.indices, graph.weights.indices)
    np.testing.assert_array_equal(base.lengths.data, graph.lengths.data)
    assert graph.content_hash != base.content_hash
    _, distance_parents = graph.shortest_paths([0], 'distance', targets=[2])
    assert graph.expand(distance_parents[0], 0, 2) == [0, 1, 2]


def test_long_edges_integrate_learned_and_unknown_pieces(tmp_path, monkeypatch):
    _, graph, _ = setup_model(tmp_path, monkeypatch)
    factor, covered = travel.edge_factors(graph, np.array([[-60., 10]]), np.array([[180., 10]]), [0.], [1.])
    lengths = np.arange(-60., 180., 10.)
    parts, coverage = travel.edge_factors(graph, np.column_stack((lengths, np.full(len(lengths), 10.))),
                                         np.column_stack((lengths+10, np.full(len(lengths), 10.))), np.zeros(len(lengths)), np.ones(len(lengths)))
    assert factor[0] == pytest.approx(np.mean(parts))
    assert covered[0] == pytest.approx(np.mean(coverage))
    assert 1.3 < factor[0] < 1.6 and .4 < covered[0] < .7


def test_connector_costs_matrix_and_route_expansion_agree(tmp_path, monkeypatch):
    _, graph, _ = setup_model(tmp_path, monkeypatch)
    bounds = [[0, 0], [139, 0], [139, 139], [0, 139]]
    registration = Registration([[1, 0, 0], [0, 1, 0]], bounds, bounds)
    bundle = plan_matrix(graph, [], [7, 10], [114, 10], registration)
    route = expand_route(graph, bundle, [0, 1])
    assert route['travelWeightM'] == pytest.approx(bundle['matrix'][0, 1])
    assert route['distanceM'] < route['travelWeightM']
    assert route['speedLearning']['learnedDistanceM'] > 0
    np.testing.assert_allclose(bundle['connectorOutWeights'], bundle['connectors']*1.8)


def test_non_ground_layers_keep_their_costs_and_frozen_artifacts_are_checked(tmp_path, monkeypatch):
    base, graph, frozen = setup_model(tmp_path, monkeypatch)
    base.node_surfaces = np.full(5, 1)
    base.surface_order = ['ground', 'bridge']
    heights = elevation_routing.attach(base, {'frame': Frame().as_dict()}, {'enabled': False, 'models': [], 'revision': 'off'})
    bridge = travel.attach(heights, base, frozen)
    np.testing.assert_allclose(bridge.weights.data, base.weights.data)
    assert travel.available(frozen)
    (elevation.root()/frozen['models'][0]['artifact']).unlink()
    assert not travel.available(frozen)
    with pytest.raises(ValueError, match='快照'):
        travel.load(frozen['models'][0], 'walk')
