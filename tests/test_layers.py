"""Layer intersections are not junctions; only declared portals connect them."""
import numpy as np
import pytest

from dirmap.geometry import Registration, affine_points
from dirmap.layers import build


def scene(portals=(), restricted=None):
    costs = np.zeros((41, 41), np.float32)
    costs[18:23, :] = 1
    costs[:, 18:23] = 1
    source = [[0, 0], [40, 0], [40, 40], [0, 40]]
    affine = [[1, 0, 0], [0, -1, 40]]
    registration = Registration(affine, source, affine_points(affine, source))
    surfaces = [{'id': 'bridge', 'label': '桥上', 'level': 1, 'accepted': True}]
    edits = [
        {'kind': 'open', 'surfaceId': 'bridge', 'pixels': [[20, 2], [20, 38]], 'radiusPx': 2, 'cost': 1.7, 'separateGround': True},
        {'kind': 'open', 'surfaceId': 'ground', 'pixels': [[2, 20], [38, 20]], 'radiusPx': 2, 'cost': 1},
    ]
    return build(costs, registration, 'crossing', edits, surfaces, list(portals), restricted, step=2)


def portal(identifier, pixel, accepted=True, length=0):
    return {'id': identifier, 'label': identifier, 'fromSurface': 'ground', 'toSurface': 'bridge',
            'pixel': pixel, 'radiusPx': 3, 'lengthM': length, 'costFactor': 1.7, 'accepted': accepted}


def test_crossing_pixel_locations_do_not_connect_different_surfaces():
    graph = scene([portal('north', [20, 2]), portal('south', [20, 38])])
    ground = graph.snap([20, 20], surface_id='ground')
    bridge = graph.snap([20, 20], surface_id='bridge')
    assert ground != bridge and graph.components[ground] != graph.components[bridge]
    assert graph.surface_for_node(ground) == 'ground'
    assert graph.surface_for_node(bridge) == 'bridge'
    with pytest.raises(ValueError, match='叠合层级'):
        graph.resolve_surface([20, 20])
    assert graph.resolve_surface([2, 20]) == 'ground'
    assert graph.resolve_surface([20, 20], 'bridge') == 'bridge'
    a, b = graph.snap([2, 20]), graph.snap([38, 20])
    distances, parents = graph.shortest_paths([a], targets=[b, bridge])
    assert np.isfinite(distances[0, b]) and np.isinf(distances[0, bridge])
    route = graph.expand(parents[0], a, b)
    assert all(graph.surface_for_node(node) == 'ground' for node in route)


def test_only_an_accepted_portal_makes_a_layer_transition_possible():
    pending = scene([portal('stairs', [20, 20], accepted=False, length=4)])
    a, b = pending.snap([2, 20]), pending.snap([20, 35], surface_id='bridge')
    assert pending.components[a] != pending.components[b]
    graph = scene([portal('stairs', [20, 20], length=4)])
    a, b = graph.snap([2, 20]), graph.snap([20, 35], surface_id='bridge')
    distances, parents = graph.shortest_paths([a], targets=[b])
    route = graph.expand(parents[0], a, b)
    changes = [(u, v) for u, v in zip(route, route[1:]) if graph.surface_for_node(u) != graph.surface_for_node(v)]
    assert len(changes) == 1
    edge = changes[0]
    assert graph.portal_edges[edge]['id'] == 'stairs'
    assert graph.lengths[edge] == pytest.approx(4)
    assert graph.weights[edge] == pytest.approx(6.8)
    assert all(graph.edge_valid(u, v) for u, v in zip(route, route[1:]))
    assert sum(float(graph.weights[u, v]) for u, v in zip(route, route[1:])) == pytest.approx(distances[0, b])


def test_zero_length_surface_join_is_a_real_edge_not_a_missing_link():
    graph = scene([portal('join', [20, 20])])
    edge = next(edge for edge in graph.portal_edges)
    assert graph.weights[edge] == 0 and graph.lengths[edge] == 0
    assert graph.edge_valid(*edge)
    distances, parents = graph.shortest_paths([edge[0]], targets=[edge[1]])
    assert distances[0, edge[1]] == 0
    assert graph.expand(parents[0], *edge) == list(edge)


def test_unconfirmed_or_forbidden_portals_cannot_reopen_ground():
    restricted = np.zeros((41, 41), bool)
    restricted[1:5, 18:23] = True
    with pytest.raises(ValueError, match='禁入'):
        scene([portal('forbidden', [20, 2])], restricted)
    graph = scene([portal('pending', [20, 2], accepted=False)])
    assert graph.cost_at([20, 2], 'ground') == 0


def test_layer_references_must_exist():
    with pytest.raises(ValueError, match='不同层级'):
        scene([{**portal('bad', [20, 20]), 'toSurface': 'unknown'}])


def test_cache_keeps_surface_ownership_and_zero_portal_edges(tmp_path, monkeypatch):
    from dirmap import graph_cache
    monkeypatch.setenv('DIRMAP_CACHE_DIR', str(tmp_path))
    graph = scene([portal('join', [20, 20])])
    graph_cache.write('layered-fixture', graph)
    restored = graph_cache.read('layered-fixture')
    assert restored and restored.content_hash == graph.content_hash
    assert restored.surface_order == graph.surface_order
    np.testing.assert_array_equal(restored.node_surfaces, graph.node_surfaces)
    np.testing.assert_array_equal(restored.weights.data, graph.weights.data)
    for edge in graph.portal_edges:
        assert restored.edge_valid(*edge)
        assert restored.portal_edges[edge] == graph.portal_edges[edge]
    assert restored.snap([20, 20], surface_id='bridge') == graph.snap([20, 20], surface_id='bridge')


def test_lower_level_ground_survives_a_separate_upper_surface():
    costs = np.ones((41, 41), np.float32)
    source = [[0, 0], [40, 0], [40, 40], [0, 40]]
    affine = [[1, 0, 0], [0, -1, 40]]
    reg = Registration(affine, source, affine_points(affine, source))
    lower = np.zeros_like(costs, bool); lower[10:31, 10:31] = True
    graph = build(costs, reg, 'lower-area', [{'kind': 'open', 'surfaceId': 'bridge', 'pixels': [[20, 2], [20, 38]],
        'radiusPx': 2, 'cost': 1, 'separateGround': True}], [{'id': 'bridge', 'label': '桥上', 'level': 1, 'accepted': True}], [], lower_level=lower)
    assert graph.cost_at([20, 20]) == 1 and graph.cost_at([20, 5]) == 0
    a, b = graph.snap([20, 20]), graph.snap([20, 20], surface_id='bridge')
    assert graph.components[a] != graph.components[b]
