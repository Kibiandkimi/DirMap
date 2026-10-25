import copy

import cv2
import numpy as np

from dirmap.recognition import reconcile_controls
from dirmap.vision import colour_masks, detect_controls, refine_circle


def test_circle_centerline_refinement_is_independent_of_hough_bias_and_crossing_ink():
    ink = np.zeros((200, 240), np.uint8)
    expected = np.array([122.3, 101.7])
    cv2.circle(ink, tuple(np.rint(expected*16).astype(int)), round(39.4*16), 255, 3, cv2.LINE_AA, 4)
    cv2.line(ink, (130, 120), (220, 180), 255, 3, cv2.LINE_AA)
    for bias in ((-2, -1), (2, 1), (0, 0)):
        point, radius = refine_circle(ink, (expected+bias).tolist(), 40.4)
        np.testing.assert_allclose(point, expected, atol=.15)
        assert abs(radius-39.4) < .15


def test_photo_colours_and_page_margins_do_not_define_control_scale():
    image = np.full((1600, 2200, 3), 250, np.uint8)
    expected = [(500, 500), (1000, 800), (1450, 1100)]
    for i, point in enumerate(expected):
        cv2.circle(image, point, 16, (190, 35, 165), 2, cv2.LINE_AA)
        cv2.putText(image, str(31+i), (point[0]+20, point[1]-10), cv2.FONT_HERSHEY_SIMPLEX, .8, (190, 35, 165), 2)
    image[80:240, 70:250] = [95, 115, 105]
    cv2.circle(image, (140, 150), 21, (145, 100, 135), 7)  # photographed decoration
    for size in ((2200, 1600), (1100, 800)):
        resized = cv2.resize(image, size, interpolation=cv2.INTER_AREA)
        masks = colour_masks(resized)
        controls = detect_controls(masks['course_ink'])
        assert len(controls) == 3
        scale = size[0]/2200
        for point, control in zip(expected, controls):
            np.testing.assert_allclose(np.array(control['pixel'])/scale, point, atol=3)


def test_reanalysis_keeps_teacher_point_and_removes_only_unconfirmed_false_candidates():
    manual = {'id': 'manual-43', 'label': '43', 'pixel': [100, 100], 'radiusPx': 20,
              'state': 'accepted', 'role': 'control', 'required': True, 'approachPixel': [99, 100]}
    previous = {'controls': [manual,
        {'id': 'old-false', 'label': '起点', 'pixel': [10, 10], 'radiusPx': 12, 'state': 'candidate', 'role': 'start', 'labelSource': 'course_symbol'},
        {'id': 'old-31', 'label': '31', 'pixel': [170, 80], 'radiusPx': 20, 'state': 'candidate', 'labelSource': 'tesseract', 'required': False}]}
    before = copy.deepcopy(previous)
    detected = [dict(manual, id='c1', pixel=[101, 100], radiusPx=17, state='candidate'),
                {'id': 'c2', 'label': '31', 'pixel': [170, 81], 'radiusPx': 20, 'state': 'candidate', 'required': True}]
    result, report = reconcile_controls(detected, previous)
    assert next(c for c in result if c['label'] == '43') == manual
    assert next(c for c in result if c['label'] == '31')['id'] == 'old-31'
    assert next(c for c in result if c['label'] == '31')['required'] is False
    assert len(result) == 2 and previous == before
    assert report['preservedTeacherControls'] == ['manual-43']


def test_deleted_automatic_candidate_is_not_resurrected():
    point = {'id': 'c1', 'label': '31', 'pixel': [100, 100], 'radiusPx': 20, 'state': 'candidate'}
    result, _ = reconcile_controls([point], {'controls': [], 'recognitionExclusions': [point]})
    assert not result
