import copy

import numpy as np
import pytest

from dirmap.access import AccessResolver
from dirmap.geometry import Registration, affine_points
from dirmap.graph import build_graph, plan_matrix, expand_route
from dirmap.vision import analyze
import cv2


def road_scene(wall=False, prohibited=False, closed=False):
    shape = (100, 120)
    masks = {k: np.zeros(shape, bool) for k in ('building', 'water', 'point_ink', 'vegetation', 'restricted', 'olive', 'paved', 'prohibited')}
    masks['restricted'][30:70, 48:83] = True
    masks['olive'][:] = masks['restricted']
    masks['paved'][:, 28:43] = True
    costs = np.full(shape, 1.5, np.float32); costs[masks['paved']] = 1; costs[masks['restricted']] = 0
    if wall:
        masks['point_ink'][:, 44:47] = True; costs[:, 44:47] = 0
    if prohibited:
        masks['prohibited'][45:56, 50:61] = True
    corners = [[0, 0], [119, 0], [119, 99], [0, 99]]
    matrix = [[.5, 0, 0], [0, -.5, 49.5]]
    reg = Registration(matrix, corners, affine_points(matrix, corners), regularization=0)
    graph = build_graph(costs, reg, 'roadside-arrival')
    graph.access_resolver = AccessResolver(graph, reg, masks)
    if closed:
        graph.closed_edits = np.zeros(shape, bool); graph.closed_edits[50, 55] = True
    return graph, reg, masks


def test_private_green_association_is_not_a_route_segment_or_a_map_edit():
    graph, reg, masks = road_scene()
    controls = [{'id': 'a', 'pixel': [55, 40], 'radiusPx': 24}, {'id': 'b', 'pixel': [55, 60], 'radiusPx': 24}]
    original, costs = copy.deepcopy(controls), graph.costs.copy()
    bundle = plan_matrix(graph, controls, [35, 10], [35, 90], reg)
    assert not bundle['unresolved'] and np.isfinite(bundle['matrix']).all()
    assert all(a['automatic'] and a['method'] == 'nearby_road' for a in bundle['approaches'])
    assert all(0 < a['offsetM'] < 9 for a in bundle['approaches'])
    assert controls == original
    for arrival in bundle['approaches']:
        x, y = np.rint(arrival['pixel']).astype(int)
        assert masks['paved'][y, x] and not masks['restricted'][y, x]
    route = expand_route(graph, bundle, [0, 1, 2, 3])
    assert all(graph.edge_valid(a, b) for a, b in route['edgeIds'])
    # No target-centre leg or connector is added across the green area.
    assert all(not masks['restricted'][round(graph.pixels[node, 1]), round(graph.pixels[node, 0])] for node in route['nodeIds'])
    np.testing.assert_array_equal(graph.costs, costs)


@pytest.mark.parametrize('restriction', ['wall', 'prohibited', 'closed', 'water'])
def test_road_fallback_keeps_other_barriers_and_explicit_restrictions(restriction):
    graph, reg, masks = road_scene(**{restriction: True} if restriction != 'water' else {})
    if restriction == 'water':
        masks['water'][45:56, 50:61] = True
    result = plan_matrix(graph, [{'id': 'a', 'pixel': [55, 50], 'radiusPx': 24}], [35, 10], [35, 90], reg)
    assert result['unresolved']


def test_explicit_arrival_is_not_silently_replaced():
    graph, reg, _ = road_scene()
    chosen = {'id': 'a', 'pixel': [55, 50], 'approachPixel': [60, 50], 'radiusPx': 24}
    assert plan_matrix(graph, [chosen], [35, 10], [35, 90], reg)['unresolved']


def test_brown_contours_never_become_a_path_through_private_green():
    image = np.full((280, 360, 3), [25, 180, 164], np.uint8)
    image[:, 25:43] = [207, 222, 245]
    cv2.line(image, (0, 100), (359, 100), (24, 113, 188), 2)
    cv2.line(image, (0, 230), (359, 30), (24, 113, 188), 2)
    result = analyze(image)
    assert result['costs'][100, 140] == 0
    assert result['semantics']['olive'][100, 140]
    assert result['costs'][160, 32] > 0
    # Brown ink crosses the real road too. Only the olive-surrounded part
    # is restored as private green, so the path remains continuous.
    assert result['costs'][100, 32] > 0
