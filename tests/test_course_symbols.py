"""Standard course symbols must be recognized from their actual ink."""
from collections import Counter
import math

import cv2
import numpy as np
import pytest

from dirmap.symbols import classify_course_symbols, course_ink_mask, course_symbol
from dirmap.vision import analyze, detect_controls
from dirmap.geometry import Registration, affine_points
from dirmap.graph import build_graph


def triangle(ink, center, radius, stroke, angle=0, filled=False):
    # ISSprOM: a 6 mm triangle side against a 5 mm control diameter.
    radians = math.radians(angle) + np.arange(3) * 2 * np.pi / 3
    vertices = np.asarray(center) + 2.4 * radius / np.sqrt(3) * np.column_stack((np.cos(radians), np.sin(radians)))
    polygon = np.rint(vertices * 16).astype(np.int32)
    if filled:
        cv2.fillPoly(ink, [polygon], 255, cv2.LINE_AA, 4)
    else:
        cv2.polylines(ink, [polygon], True, 255, stroke, cv2.LINE_AA, 4)
    return vertices


def binary(ink):
    return (ink > 90).astype(np.uint8) * 255


@pytest.mark.parametrize('radius,stroke', [(12, 2), (22, 3), (40, 3), (60, 6)])
def test_two_finish_rings_do_not_invent_a_triangle(radius, stroke):
    ink = np.zeros((500, 500), np.uint8)
    for factor in (.8, 1.2):
        cv2.circle(ink, (4000, 4000), round(radius * factor * 16), 255, stroke, cv2.LINE_AA, 4)
    result = course_symbol(binary(ink), {'pixel': [250, 250], 'radiusPx': radius * 1.2})
    assert result['role'] == 'finish'
    assert result['triangle'] is None


@pytest.mark.parametrize('radius,stroke,angle', [(12, 2, 0), (22, 3, 37), (40, 3, 88), (60, 6, 17)])
def test_triangle_and_finish_rings_remain_a_combined_start_finish(radius, stroke, angle):
    ink = np.zeros((500, 500), np.uint8)
    for factor in (.8, 1.2):
        cv2.circle(ink, (4000, 4000), round(radius * factor * 16), 255, stroke, cv2.LINE_AA, 4)
    triangle(ink, [250, 250], radius, stroke, angle)
    result = course_symbol(binary(ink), {'pixel': [250, 250], 'radiusPx': radius * 1.2})
    assert result['role'] == 'start_finish'
    assert result['triangle'] is not None


def course_fixture(scale=1):
    ink = np.zeros((round(700 * scale), round(900 * scale)), np.uint8)
    radius, stroke = 22 * scale, max(1, round(3 * scale))
    for point in ((420, 140), (690, 370), (200, 530)):
        cv2.circle(ink, tuple(np.rint(np.asarray(point) * scale * 16).astype(int)), round(radius * 16), 255, stroke, cv2.LINE_AA, 4)
    return ink, radius, stroke


@pytest.mark.parametrize('angle,scale', [(0, 1), (37, .6), (88, 1.7)])
def test_standalone_start_has_the_triangle_centroid_and_no_phantom_ring(angle, scale):
    ink, radius, stroke = course_fixture(scale)
    center = np.array([140, 140]) * scale
    vertices = triangle(ink, center, radius, stroke, angle)
    # A course leg joins the outline, rather than being mistaken for a side.
    end = vertices[0] + (vertices[0] - center) * 2
    cv2.line(ink, tuple(np.rint(vertices[0]).astype(int)), tuple(np.rint(end).astype(int)), 255, max(1, stroke - 1))
    ink = binary(ink)
    controls = detect_controls(ink)
    classify_course_symbols(ink, controls)
    assert Counter(c['role'] for c in controls) == {'control': 3, 'start': 1}
    start = next(c for c in controls if c['role'] == 'start')
    np.testing.assert_allclose(start['pixel'], center, atol=2)
    assert start['label'] == '起点' and start['required'] is False
    assert start['symbolGeometry']['ringRadii'] == []
    # An unrelated ink mark at the radius of an unprinted circle survives.
    point = tuple(np.rint(center - radius * np.array([math.cos(math.radians(angle)), math.sin(math.radians(angle))])).astype(int))
    assert ink[point[1], point[0]] == 0
    fragment = np.zeros_like(ink); fragment[point[1], point[0]] = 255
    annotation = course_ink_mask(fragment, [start])
    assert annotation[point[1], point[0]] == 0


def test_solid_triangles_letter_counters_and_hatched_areas_do_not_become_starts():
    ink, radius, stroke = course_fixture()
    triangle(ink, [140, 140], radius, stroke, filled=True)
    cv2.putText(ink, 'A4', (610, 130), cv2.FONT_HERSHEY_SIMPLEX, 2.5, 255, 4, cv2.LINE_AA)
    cv2.polylines(ink, [np.array([[90, 330], [180, 330], [90, 390]])], True, 255, stroke)
    area = np.zeros_like(ink)
    triangle(area, [680, 560], 65, stroke, filled=True)
    stripes = np.zeros_like(ink)
    for y in range(440, 680, 8):
        cv2.line(stripes, (560, y), (800, y), 255, 2)
    ink |= cv2.bitwise_and(area, stripes)
    triangle(ink, [680, 560], 65, stroke)
    controls = detect_controls(binary(ink))
    classify_course_symbols(binary(ink), controls)
    assert Counter(c['role'] for c in controls) == {'control': 3}


@pytest.mark.parametrize('angle', [0, 37])
def test_small_scan_gap_keeps_the_start_without_filling_the_gap_as_annotation(angle):
    ink, radius, stroke = course_fixture()
    center = np.array([140., 140.])
    vertices = triangle(ink, center, radius, stroke, angle)
    middle = (vertices[1] + vertices[2]) / 2
    along = (vertices[2] - vertices[1]) / np.linalg.norm(vertices[2] - vertices[1])
    normal = np.array([-along[1], along[0]])
    gap = np.array([middle + a * along * 1.5 + b * normal * 5 for a, b in ((-1, -1), (1, -1), (1, 1), (-1, 1))])
    cv2.fillPoly(ink, [np.rint(gap * 16).astype(np.int32)], 0, cv2.LINE_8, 4)
    ink = binary(ink)
    controls = detect_controls(ink)
    classify_course_symbols(ink, controls)
    starts = [c for c in controls if c['role'] == 'start']
    assert len(starts) == 1
    np.testing.assert_allclose(starts[0]['pixel'], center, atol=2)
    annotation = course_ink_mask(ink, starts)
    assert not np.any(annotation[ink == 0])


def test_recognized_separate_start_and_finish_are_connected_to_the_course_graph():
    ink, radius, stroke = course_fixture()
    triangle(ink, [140, 140], radius, stroke, 22)
    for factor in (.8, 1.2):
        cv2.circle(ink, (760 * 16, 580 * 16), round(radius * factor * 16), 255, stroke, cv2.LINE_AA, 4)
    image = np.full((*ink.shape, 3), 255, np.uint8)
    alpha = ink.astype(float)[..., None] / 255
    image[:] = np.rint(image * (1 - alpha) + np.array([200, 50, 205]) * alpha).astype(np.uint8)
    result = analyze(image)
    assert Counter(c['role'] for c in result['controls']) == {'control': 3, 'start': 1, 'finish': 1}
    height, width = ink.shape
    pixels = [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]]
    affine = [[1, 0, 0], [0, -1, height - 1]]
    registration = Registration(affine, pixels, affine_points(affine, pixels), regularization=0)
    graph = build_graph(result['costs'], registration, 'separate-symbols', step=4)
    nodes = [graph.snap(c['pixel']) for c in result['controls']]
    assert len({graph.components[n] for n in nodes}) == 1
