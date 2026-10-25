"""Stair semantics must open treads, never the rails or a crossing barrier."""
import cv2
import numpy as np
import pytest

from dirmap import store
from dirmap.geometry import Registration, affine_points
from dirmap.graph import build_graph
from dirmap.vision import analyze, read_image
from dirmap.stairs import detect_stairs


def flight():
    image = np.full((140, 160, 3), 245, np.uint8)
    for x in (59, 101):
        cv2.line(image, (x, 0), (x, 139), (10, 10, 10), 3)
    for y in range(42, 99, 7):
        cv2.line(image, (61, y), (99, y), (10, 10, 10), 1)
    return image


def graph_for(result):
    height, width = result['costs'].shape
    source = [[0, 0], [width-1, 0], [width-1, height-1], [0, height-1]]
    affine = [[1, 0, 0], [0, -1, height-1]]
    return build_graph(result['costs'], Registration(affine, source, affine_points(affine, source)), 'stairs')


def test_short_stairs_can_enter_a_wide_landing_beside_a_retained_wall_and_leave_a_narrow_one():
    image = np.full((150, 150, 3), 245, np.uint8)
    protected = np.zeros((150, 150), np.uint8)
    image[60:, :55] = 145; protected[60:, :55] = 255
    image[:, 96:] = 145; protected[:, 96:] = 255
    image[70:83, 55:79] = [50, 140, 65]; protected[70:83, 55:79] = 255
    for x, y0, y1 in ((55, 60, 69), (95, 40, 110)):
        cv2.line(image, (x, y0), (x, y1), (20, 20, 20), 1)
    for y, x0, x1 in ((60, 55, 95), (70, 55, 95), (82, 79, 95)):
        cv2.line(image, (x0, y), (x1, y), (20, 20, 20), 1)
    cv2.line(image, (81, 60), (94, 60), (20, 20, 20), 4)
    _, treads, _ = detect_stairs(image, [{'radiusPx': 40}], protected)
    free = ((cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) > 185) & (protected == 0)).astype(np.uint8)
    free[treads > 0] = 1
    _, components = cv2.connectedComponents(free, connectivity=4)
    assert components[45, 65] != 0 and components[45, 65] == components[100, 85]
    assert not free[59:62, 85:92].any()  # thick part of the landing's wall
    assert not free[protected > 0].any()


@pytest.mark.parametrize('angle', [0, 25, 67])
def test_detected_staircase_opens_only_treads_and_preserves_side_walls(angle):
    transform = cv2.getRotationMatrix2D((80, 70), angle, 1)
    # A larger canvas lets the side walls reach the boundary after rotation.
    image = flight()
    image = cv2.warpAffine(image, transform, (160, 140), flags=cv2.INTER_NEAREST,
                           borderValue=(10, 10, 10))
    result = analyze(image)
    assert result['statistics']['staircaseCandidates'] >= 1
    graph = graph_for(result)
    start, finish = [transform @ np.array([80, y, 1]) for y in (20, 120)]
    a, b = graph.snap(start), graph.snap(finish)
    assert graph.components[a] == graph.components[b]
    for x in (59, 101):
        point = np.rint(transform @ np.array([x, 70, 1])).astype(int)
        assert result['costs'][point[1], point[0]] == 0
    point = np.rint(transform @ np.array([80, 56, 1])).astype(int)
    assert result['costs'][point[1], point[0]] > 1  # steps slow travel


@pytest.mark.parametrize('wall_width', [2, 5])
def test_thick_wall_across_a_stair_flight_remains_blocked(wall_width):
    image = flight()
    image[69:69+wall_width, :] = 10
    result = analyze(image)
    assert np.all(result['costs'][69:69+wall_width, 62:99] == 0)
    graph = graph_for(result)
    assert graph.components[graph.snap([80, 20])] != graph.components[graph.snap([80, 120])]


def test_pillar_inside_a_tread_cell_is_not_erased_by_the_flight_footprint():
    image = flight()
    image[51:53, 79:81] = 10
    result = analyze(image)
    assert result['statistics']['staircaseCandidates'] >= 1
    assert np.all(result['costs'][51:53, 79:81] == 0)


@pytest.mark.parametrize('angle', [0, 27])
def test_tapered_stairs_follow_observed_rails_instead_of_a_rectangle(angle):
    image = np.full((140, 180, 3), 245, np.uint8)
    left = lambda y: 55+.15*y
    right = lambda y: 100-.05*y
    for side in (left, right):
        cv2.line(image, (round(side(0)), 0), (round(side(139)), 139), (10, 10, 10), 3)
    for y in range(42, 99, 7):
        cv2.line(image, (round(left(y))+2, y), (round(right(y))-2, y), (10, 10, 10), 1)
    matrix = cv2.getRotationMatrix2D((90, 70), angle, 1)
    image = cv2.warpAffine(image, matrix, (180, 140), flags=cv2.INTER_NEAREST, borderValue=(10, 10, 10))
    result = analyze(image)
    graph = graph_for(result)
    positions = [matrix @ np.array([(left(y)+right(y))/2, y, 1]) for y in (20, 120)]
    assert result['statistics']['staircaseCandidates'] >= 1
    assert graph.components[graph.snap(positions[0])] == graph.components[graph.snap(positions[1])]
    for side in (left, right):
        x, y = np.rint(matrix @ np.array([side(70), 70, 1])).astype(int)
        assert result['costs'][y, x] == 0


def test_independent_boxes_with_light_ground_between_outlines_are_not_stair_treads():
    image = np.full((160, 140, 3), 245, np.uint8)
    for y in range(35, 112, 11):
        cv2.rectangle(image, (45, y), (90, y+9), (10, 10, 10), 1)
    stairs, treads, candidates = detect_stairs(image, [{'radiusPx': 40}], np.zeros(image.shape[:2], np.uint8))
    assert not candidates and not np.any(stairs) and not np.any(treads)


@pytest.mark.parametrize('fill', [(145, 145, 145), (90, 150, 160), (220, 180, 80)])
def test_repeated_markings_in_a_roof_garden_or_water_do_not_open_a_passage(fill):
    image = flight()
    image[42:100, 62:99] = fill
    for y in range(42, 99, 7):
        cv2.line(image, (61, y), (99, y), (10, 10, 10), 1)
    result = analyze(image)
    assert np.all(result['costs'][45:96, 65:96] == 0)


def test_real_map_stairs_between_pools_are_identified_without_opening_water_or_rails():
    image = read_image(store.get_asset('szu-1')['path'])
    result = analyze(image)
    assert np.count_nonzero(result['semantics']['stairs'][496:526, 835:855]) > 100
    # The flanking blue pools and the dark side rail are independently
    # visible in the original, outside all checkpoint overprint.
    assert result['costs'][506, 815] == 0
    assert result['costs'][506, 879] == 0
    assert result['costs'][506, 837] == 0


def test_real_stairs_and_repeated_round_boxes_are_distinguished():
    image = read_image(store.get_asset('szu-3')['path'])
    result = analyze(image)
    # Both rows contain separate closed frames with light ground between
    # their outlines, unlike the shared single lines in the supplied legend.
    assert not np.any(result['semantics']['stairs'][1728:1797, 1702:1777])
    # Retain all four genuine flights in the original candidate inventory,
    # including the small, oblique three-cell flight beside the pool.
    for x0, y0, x1, y1 in [(1404, 735, 1427, 766), (1673, 872, 1700, 892),
                           (1842, 1040, 1857, 1060), (1270, 1540, 1310, 1575)]:
        assert np.count_nonzero(result['semantics']['stairs'][y0:y1, x0:x1]) > 15
