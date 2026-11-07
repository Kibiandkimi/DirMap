import cv2
import numpy as np
import pytest

from dirmap.network_registration import registration_footprint


def forest_page():
    image = np.full((1200, 1400, 3), 255, np.uint8)
    cv2.rectangle(image, (8, 8), (1390, 170), (150, 255, 255), -1)
    cv2.rectangle(image, (8, 1130), (1390, 1190), (150, 255, 255), -1)
    cv2.rectangle(image, (4, 4), (1395, 1195), (150, 0, 0), 7)
    cv2.rectangle(image, (40, 400), (95, 850), (140, 240, 150), -1)
    cv2.putText(image, 'Legend', (35, 360), cv2.FONT_HERSHEY_SIMPLEX, .9, (0, 0, 0), 2)
    outline = np.array([[300, 300], [1150, 320], [1250, 850], [750, 1050], [330, 870]], np.int32)
    cv2.polylines(image, [outline], True, (0, 0, 0), 4)
    for points in ([[300, 300], [700, 650], [1250, 850]], [[1150, 320], [700, 650], [330, 870]],
                   [[750, 1050], [700, 650]]):
        cv2.polylines(image, [np.array(points)], False, (0, 0, 0), 3)
    for point in ((500, 480), (700, 700), (920, 480), (960, 850)):
        cv2.circle(image, point, 25, (180, 235, 198), -1)
    return image


@pytest.mark.parametrize('turned', [False, True])
def test_forest_linework_locates_the_map_instead_of_a_page_banner_or_legend(turned):
    image = forest_page()
    inside = np.array([[400, 450], [800, 650], [1000, 700]], float)
    outside = np.array([[700, 80], [60, 650], [700, 1160]], float)
    if turned:
        inside = np.column_stack((inside[:, 1], image.shape[1]-1-inside[:, 0]))
        outside = np.column_stack((outside[:, 1], image.shape[1]-1-outside[:, 0]))
        image = np.ascontiguousarray(np.rot90(image))
    original = image.copy()
    footprint = registration_footprint(image)
    assert footprint is not None
    for point in inside:
        assert cv2.pointPolygonTest(footprint.astype(np.float32), tuple(point), False) >= 0
    for point in outside:
        assert cv2.pointPolygonTest(footprint.astype(np.float32), tuple(point), False) < 0
    assert np.array_equal(image, original)


def test_a_genuine_narrow_map_with_linework_is_kept_at_the_paper_edge():
    image = np.full((700, 1500, 3), 255, np.uint8)
    cv2.rectangle(image, (15, 15), (1484, 145), (180, 235, 198), -1)
    points = np.array([[35, 35], [400, 120], [950, 35], [1460, 120]])
    cv2.polylines(image, [points], False, (0, 0, 0), 3)
    footprint = registration_footprint(image)
    assert footprint is not None
    assert cv2.pointPolygonTest(footprint.astype(np.float32), (750., 85.), False) >= 0
    assert cv2.pointPolygonTest(footprint.astype(np.float32), (750., 400.), False) < 0


def test_an_established_coloured_map_outline_keeps_its_measured_extent():
    image = np.full((1200, 1400, 3), 255, np.uint8)
    cv2.rectangle(image, (8, 8), (1390, 170), (150, 255, 255), -1)
    cv2.rectangle(image, (150, 250), (1250, 1050), (180, 235, 198), -1)
    footprint = registration_footprint(image)
    assert {tuple(point) for point in footprint} == {(150, 250), (1250, 250), (1250, 1050), (150, 1050)}


def test_nearby_blue_branding_does_not_extend_a_forest_maps_outline():
    image = forest_page()
    # Like a copyright banner, this nearly touches the map's south-east
    # boundary and would join its component after the density smoothing.
    cv2.rectangle(image, (900, 995), (1360, 1080), (150, 0, 0), -1)
    footprint = registration_footprint(image)
    assert footprint is not None
    assert cv2.pointPolygonTest(footprint.astype(np.float32), (800., 650.), False) >= 0
    assert cv2.pointPolygonTest(footprint.astype(np.float32), (1300., 1040.), False) < 0
