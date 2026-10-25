import math

import cv2
import numpy as np

from dirmap.geometry import Registration, affine_points
from dirmap.graph import build_graph
from dirmap.symbols import classify_course_symbols
from dirmap.vision import analyze, purple_restrictions, water_obstacles


def test_photo_montage_guides_do_not_cut_the_map_into_disconnected_strips():
    water = np.zeros((1000, 900), np.uint8)
    # Repeated blue north lines span only the map inset, not the whole page.
    for x in (180, 350, 520, 690):
        cv2.line(water, (x, 150), (x-14, 620), 255, 2)
    cv2.rectangle(water, (300, 320), (420, 400), 255, -1)
    cv2.line(water, (75, 250), (75, 700), 255, 2)  # independent narrow waterway
    result = water_obstacles(water)
    assert not result[460:500, 150:710].any()
    assert np.all(result[330:390, 310:410] > 0)
    assert np.all(result[250:701, 75] > 0)


def test_missing_guide_still_identifies_regular_family_but_not_unrelated_waterways():
    water = np.zeros((1000, 900), np.uint8)
    for x in (150, 450, 600):  # the stroke at 300 is obscured
        cv2.line(water, (x, 150), (x, 750), 255, 2)
    cv2.rectangle(water, (410, 330), (490, 410), 255, -1)
    cv2.line(water, (75, 250), (75, 700), 255, 2)
    result = water_obstacles(water)
    assert not result[480:520, 140:610].any()
    assert np.all(result[345:395, 425:475] > 0)
    assert np.all(result[250:701, 75] > 0)
    unequal = np.zeros_like(water)
    for x in (75, 180, 410):
        cv2.line(unequal, (x, 150), (x, 750), 255, 2)
    assert np.array_equal(water_obstacles(unequal), unequal)


def test_real_sheet_36_is_not_blocked_by_a_blue_north_guide():
    from dirmap import store
    from dirmap.vision import read_image
    image = read_image(store.get_asset('szu-2')['path'])
    result = analyze(image)
    assert result['costs'][495, 436] > 0
    assert not result['semantics']['water'][495, 436]
    # Existing lake interiors, a roof crossed by the guide, and forbidden
    # point 35 remain obstacles. Removing annotations must not open them.
    assert np.all(result['costs'][515:530, 210:230] == 0)
    assert result['costs'][210, 436] == 0
    assert result['costs'][629, 293] == 0


def frame(width, height):
    source = [[0, 0], [width-1, 0], [width-1, height-1], [0, height-1]]
    matrix = [[1, 0, 0], [0, -1, height-1]]
    return Registration(matrix, source, affine_points(matrix, source), regularization=0)


def test_paved_surface_border_is_passable_while_a_thick_wall_on_the_same_boundary_is_not():
    for width in (1, 5):
        image = np.full((360, 360, 3), [200, 216, 240], np.uint8)
        image[:, :180] = [80, 230, 100]
        cv2.line(image, (180, 0), (180, 359), (20, 20, 20), width, cv2.LINE_AA)
        for center in ((65, 65), (285, 285)):
            cv2.circle(image, center, 40, (190, 35, 165), 3, cv2.LINE_AA)
        result = analyze(image)
        graph = build_graph(result['costs'], frame(360, 360), f'paving-border-{width}', step=4)
        a, b = graph.snap([160, 180]), graph.snap([200, 180])
        assert (graph.components[a] == graph.components[b]) == (width == 1)
        if width == 5:
            assert np.all(result['costs'][100:200, 179:182] == 0)


def test_combined_start_finish_is_not_a_checkpoint_or_forbidden_area():
    purple = np.zeros((500, 500), np.uint8)
    center, radius = (240, 230), 28
    for r in (radius, 20):
        cv2.circle(purple, center, r, 255, 2)
    vertices = np.array([[240, 195], [270, 248], [210, 248]], np.int32)
    cv2.polylines(purple, [vertices], True, 255, 3)
    controls = [{'id': 's', 'pixel': list(center), 'radiusPx': radius, 'required': True, 'label': '?'}]
    classify_course_symbols(purple, controls)
    assert controls[0]['role'] == 'start_finish'
    assert controls[0]['required'] is False
    assert controls[0]['label'] == '起终点'
    assert purple_restrictions(purple, controls)[230, 240] == 0


def test_ordinary_circle_and_crossing_line_do_not_become_start():
    purple = np.zeros((500, 500), np.uint8)
    cv2.circle(purple, (200, 250), 30, 255, 3)
    cv2.line(purple, (160, 260), (270, 230), 255, 3)
    controls = [{'id': 'c', 'pixel': [200, 250], 'radiusPx': 30, 'required': True, 'label': '31'}]
    classify_course_symbols(purple, controls)
    assert controls[0]['role'] == 'control'
    assert controls[0]['required'] is True


def test_course_circle_does_not_erase_forbidden_hatching_under_it():
    purple = np.zeros((500, 500), np.uint8)
    for x in range(120, 361, 5):
        cv2.line(purple, (x, 120), (x, 360), 255, 2)
    cv2.circle(purple, (240, 240), 40, 255, 3)
    control = {'id': 'c', 'pixel': [240, 240], 'radiusPx': 40, 'role': 'control'}
    result = purple_restrictions(purple, [control])
    assert result[240, 240] > 0
    assert result[200, 240] > 0


def test_white_courtyard_and_one_pixel_gate_survive_building_recognition():
    image = np.full((90, 100, 3), 250, np.uint8)
    image[15:75, 20:80] = 145
    image[30:60, 35:65] = 250
    image[43, 20:36] = 250  # real narrow opening, not on a coarse grid row
    result = analyze(image)
    assert result['blocked'][43, 22] == 0
    assert result['blocked'][45, 50] == 0
    assert result['blocked'][20, 45] > 0
    graph = build_graph(result['costs'], frame(100, 90), 'courtyard', step=4)
    a, b = graph.snap([10, 43]), graph.snap([50, 43])
    assert graph.components[a] == graph.components[b]


def test_native_shortcut_retained_even_if_coarse_network_already_connected():
    costs = np.ones((70, 100), np.float32)
    costs[:, 45] = 0
    costs[31, 45] = 1
    costs[57:65, 45] = 1  # long alternative which the old coarse graph sees
    registration = frame(100, 70)
    coarse = build_graph(costs, registration, 'shortcuts', step=4, adaptive=False)
    refined = build_graph(costs, registration, 'shortcuts', step=4)
    a, b = coarse.snap([34, 30]), coarse.snap([54, 30])
    old, _ = coarse.shortest_paths([a])
    assert old[0, b] > 50
    a, b = refined.snap([34, 30]), refined.snap([54, 30])
    distances, parents = refined.shortest_paths([a])
    path = refined.expand(parents[0], a, b)
    assert distances[0, b] < 25
    assert [45, 31] in refined.pixels[path].tolist()
    np.testing.assert_array_equal(refined.costs, costs)  # no obstacle was opened


def test_coarse_diagonal_cannot_cut_native_pixel_corner():
    costs = np.ones((25, 25), np.float32)
    costs[10, 11] = 0
    costs[11, 10] = 0
    graph = build_graph(costs, frame(25, 25), 'diagonal', step=4)
    a, b = graph.snap([10, 10]), graph.snap([14, 14])
    assert graph.weights[a, b] == 0
    distance, _ = graph.shortest_paths([a])
    assert distance[0, b] > math.sqrt(32)


def test_arbitrary_endpoint_connector_cannot_touch_a_wall_corner():
    costs = np.ones((20, 20), np.float32)
    costs[8, 9] = 0
    graph = build_graph(costs, frame(20, 20), 'endpoint-corner', step=4)
    assert not graph.clear_segment([7.25, 7.25], [10.75, 10.75])
    assert graph.clear_segment([7.25, 7.1], [7.25, 11.1])


def test_adaptive_cells_keep_native_connectivity_without_crossing_any_wall():
    costs = np.ones((45, 49), np.float32)
    costs[8:39, 13] = 0
    costs[24, 3:42] = 0
    costs[24, 31] = 1
    costs[30, 13] = 1
    graph = build_graph(costs, frame(49, 45), 'cell-boundaries', step=8)
    labels_count, labels = cv2.connectedComponents((costs > 0).astype(np.uint8), connectivity=4)
    node_labels = labels[np.rint(graph.pixels[:, 1]).astype(int), np.rint(graph.pixels[:, 0]).astype(int)]
    assert graph.component_count == labels_count-1
    row, col = graph.weights.nonzero()
    for a, b in zip(row, col):
        if a < b:
            assert node_labels[a] == node_labels[b]
            assert graph.clear_segment(graph.pixels[a], graph.pixels[b])
