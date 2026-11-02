import cv2
import numpy as np
import pytest

from dirmap.access import AccessResolver
from dirmap.geometry import Registration, affine_points
from dirmap.graph import build_graph, expand_route, plan_matrix
from dirmap.vision import analyze


CENTRES = [(140, 180), (305, 260), (500, 150)]


def printed_points(protection=None, quality=82, ground=(112, 237, 134)):
    image = np.full((440, 640, 3), ground, np.uint8)
    cv2.rectangle(image, (280, 0), (298, 439), (207, 222, 245), -1)
    if protection == 'area':
        cv2.rectangle(image, (110, 150), (170, 210), (110, 160, 150), -1)
    elif protection == 'small_area':
        cv2.rectangle(image, (134, 174), (146, 186), (110, 160, 150), -1)
    elif protection == 'strip':
        cv2.rectangle(image, (138, 115), (142, 245), (110, 160, 150), -1)
    elif protection == 'wall':
        cv2.line(image, (140, 115), (140, 245), (0, 0, 0), 3)
    for centre in CENTRES:
        cv2.circle(image, centre, 3, (0, 0, 0), -1, cv2.LINE_AA)
        cv2.circle(image, centre, 32, (220, 20, 215), 2, cv2.LINE_AA)
    return cv2.imdecode(cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, quality])[1], cv2.IMREAD_COLOR)


def prepared(image):
    result = analyze(image)
    corners = [[0, 0], [639, 0], [639, 439], [0, 439]]
    matrix = [[.3, 0, 0], [0, -.3, 132]]
    reg = Registration(matrix, corners, affine_points(matrix, corners), regularization=0)
    graph = build_graph(result['costs'], reg, 'printed-point-fixture', step=4)
    graph.access_resolver = AccessResolver(graph, reg, result['semantics'])
    assert len(result['controls']) == len(CENTRES)
    return result, reg, graph


@pytest.mark.parametrize('quality', [75, 92])
def test_compressed_black_points_on_green_have_legal_perimeter_approaches(quality):
    image = printed_points(quality=quality)
    result, reg, graph = prepared(image)
    original = graph.costs.copy()
    bundle = plan_matrix(graph, result['controls'], [65, 60], [65, 60], reg)
    assert not bundle['unresolved'], bundle['unresolved']
    assert len(bundle['approaches']) == 3
    for approach in bundle['approaches']:
        assert approach['method'] == 'point_symbol_edge' and approach['automatic']
        assert 0 < approach['offsetM'] < 3
    for x, y in CENTRES:
        # Black symbol interiors remain obstacles; no hollow "courtyard" is
        # manufactured by hue thresholds applied to compressed near-black.
        assert result['semantics']['point_ink'][y, x]
        assert graph.costs[y, x] == 0
    route = expand_route(graph, bundle, [0, 1, 2, 3, 4])
    assert all(graph.edge_valid(a, b) for a, b in route['edgeIds'])
    np.testing.assert_array_equal(graph.costs, original)


@pytest.mark.parametrize('protection', ['area', 'small_area', 'strip', 'wall'])
def test_point_ink_recovery_does_not_license_forbidden_areas_or_attached_walls(protection):
    result, reg, graph = prepared(printed_points(protection))
    control = min(result['controls'], key=lambda c: np.linalg.norm(np.array(c['pixel'])-CENTRES[0]))
    bundle = plan_matrix(graph, [control], [65, 60], [65, 60], reg)
    assert bundle['unresolved'], protection
    assert 'matrix' not in bundle
    if protection in ('area', 'small_area'):
        # The middle of the printed top edge retains olive evidence; JPEG
        # chroma at the tiny square's corner mixes with the bright grass.
        assert result['semantics']['restricted'][175, 140]


@pytest.mark.parametrize('quality', [75, 92])
def test_nearby_road_can_be_an_arrival_but_distant_olive_points_cannot_be_moved_arbitrarily(quality):
    result, reg, graph = prepared(printed_points(quality=quality, ground=(22, 177, 155)))
    bundle = plan_matrix(graph, result['controls'], [289, 60], [289, 60], reg)
    close = min(result['controls'], key=lambda c: np.linalg.norm(np.asarray(c['pixel'])-[305, 260]))
    assert {c['id'] for c in result['controls'] if c['id'] != close['id']} == {c['id'] for c in bundle['unresolved']}
    ready = plan_matrix(graph, [close], [289, 60], [289, 60], reg)
    assert not ready['unresolved']
    assert ready['approaches'][0]['method'] == 'nearby_road'
    route = expand_route(graph, ready, [0, 1, 2])
    assert all(graph.edge_valid(a, b) for a, b in route['edgeIds'])
    assert result['semantics']['restricted'][50, 50]
    assert graph.costs[50, 50] == 0
