import cv2
import numpy as np
import pytest

from dirmap.access import AccessResolver
from dirmap.geometry import Registration, affine_points
from dirmap.graph import build_graph, expand_route, plan_matrix
from dirmap.vision import analyze
from dirmap.canopies import open_boundaries


def covered_passage(shade=200, wall_width=2, bench=False):
    image = np.full((250, 300, 3), 255, np.uint8)
    image[60:171, 70:231] = 128
    image[60:171, 135:166] = shade
    image[60:60+wall_width, 70:231] = 0
    image[171-wall_width:171, 70:231] = 0
    image[60:171, 70:70+wall_width] = 0
    image[60:171, 231-wall_width:231] = 0
    if bench:
        image[85:87, 144:157] = 0
    cv2.circle(image, (150, 115), 18, (220, 20, 215), 2, cv2.LINE_AA)
    return image


def prepared(image, edits=()):
    result = analyze(image)
    assert len(result['controls']) == 1
    corners = [[0, 0], [299, 0], [299, 249], [0, 249]]
    matrix = [[.5, 0, 0], [0, -.5, 124.5]]
    reg = Registration(matrix, corners, affine_points(matrix, corners), regularization=0)
    graph = build_graph(result['costs'], reg, 'covered-passage', list(edits), step=4)
    graph.access_resolver = AccessResolver(graph, reg, result['semantics'])
    return result, reg, graph


@pytest.mark.parametrize('shade', [200, 215])
def test_light_grey_passage_connects_through_thin_outline_at_both_ends(shade):
    result, reg, graph = prepared(covered_passage(shade=shade))
    bundle = plan_matrix(graph, result['controls'], [150, 30], [150, 210], reg)
    assert not bundle['unresolved'], bundle['unresolved']
    assert bundle['approaches'][0]['method'] == 'exact'
    assert graph.clear_segment([150, 30], [150, 210])
    assert graph.costs[100, 100] == graph.costs[100, 200] == 0
    route = expand_route(graph, bundle, [0, 1, 2])
    assert all(graph.edge_valid(a, b) for a, b in route['edgeIds'])
    assert result['statistics']['canopyBoundaryPixels'] > 0


@pytest.mark.parametrize('parameters', [{'wall_width': 5}, {'shade': 180}])
def test_thick_wall_and_dark_roof_do_not_become_covered_passages(parameters):
    result, reg, graph = prepared(covered_passage(**parameters))
    assert not graph.clear_segment([150, 30], [150, 210])
    bundle = plan_matrix(graph, result['controls'], [150, 30], [150, 210], reg)
    assert bundle['unresolved']


def test_separate_ink_inside_a_canopy_is_not_its_outline():
    result, reg, graph = prepared(covered_passage(bench=True))
    assert np.all(graph.costs[85:87, 144:157] == 0)
    assert not graph.clear_segment([150, 70], [150, 100])
    bundle = plan_matrix(graph, result['controls'], [150, 30], [150, 210], reg)
    assert not bundle['unresolved']


def test_a_teacher_can_close_one_end_and_keep_access_from_the_other():
    _, reg, graph = prepared(covered_passage(), [{'kind': 'close', 'pixels': [[135, 61], [165, 61]], 'radiusPx': 3}])
    assert not graph.clear_segment([150, 30], [150, 115])
    assert graph.clear_segment([150, 115], [150, 210])
    assert graph.costs[61, 150] == 0


def test_tiles_keep_region_identity_across_an_internal_obstacle_and_tile_seams():
    image = np.full((300, 500, 3), 255, np.uint8)
    image[30:250, 40:470] = 200
    image[30:32, 40:470] = image[248:250, 40:470] = 0
    image[30:250, 40:42] = image[30:250, 468:470] = 0
    # This long bench cuts some tiles completely, but its two sides belong
    # to the same grey region through the connection beyond its endpoints.
    image[120:122, 80:390] = 0
    blocked = (image[..., 0] < 196).astype(np.uint8)
    empty = np.zeros(blocked.shape, np.uint8)
    controls = [{'radiusPx': 25}]
    whole, count = open_boundaries(image, blocked, empty, empty, controls, tile_size=1024)
    assert count == 1 and whole.any() and not whole[120:122, 80:390].any()
    for size in (32, 63, 127):
        tiled, found = open_boundaries(image, blocked, empty, empty, controls, tile_size=size)
        assert found == count
        np.testing.assert_array_equal(tiled, whole)
