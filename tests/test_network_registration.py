import cv2
import numpy as np
import pytest
from scipy.spatial import cKDTree

from dirmap.network_registration import road_network_register, boundary_path_matches
from dirmap.geometry import affine_points
from dirmap.reference import polygon_properties


def printed_network(angle, symmetric=False):
    image = np.full((660, 720, 3), 255, np.uint8)
    outline = np.array([[65, 105], [545, 65], [652, 245], [590, 575], [175, 615], [65, 430]], np.int32)
    if symmetric:
        outline = np.array([[70, 95], [590, 95], [590, 615], [70, 615]], np.int32)
    cv2.fillPoly(image, [outline], (155, 225, 185))
    radians = np.radians(angle)
    matrix = np.array([[np.cos(radians), np.sin(radians), -340], [np.sin(radians), -np.cos(radians), 260.]])*.7
    reference = [{'id': 'park', 'kind': 'green', 'tags': {'leisure': 'park'}, **polygon_properties(affine_points(matrix, outline))}]
    lines = []
    for x in [150, 240, 330, 420, 510]:
        for y in [175, 265, 355, 445]:
            lines.append([[x, y], [x if symmetric else x+12, y+44], [x, y+90]])
    for y in [175, 265, 355, 445, 535]:
        for x in [150, 240, 330, 420]:
            lines.append([[x, y], [x+90, y]])
    if not symmetric:
        lines += [[[90, 350], [220, 210], [540, 165]], [[160, 555], [580, 380]], [[460, 535], [585, 500], [600, 240]]]
    for index, line in enumerate(lines):
        cv2.polylines(image, [np.array(line)], False, (207, 222, 245), 8, cv2.LINE_AA)
        reference.append({'id': str(index), 'kind': 'road', 'tags': {'highway': 'footway'}, 'points': affine_points(matrix, line).tolist()})
    return image, reference, matrix


@pytest.mark.parametrize('angle', [27, -18, 71])
def test_road_registration_generalizes_without_any_water_buildings_or_control_labels(angle):
    image, reference, truth = printed_network(angle)
    registration, quality = road_network_register(image, reference)
    query = [[180, 210], [230, 400], [450, 530]]
    error = np.linalg.norm(affine_points(registration.affine, query)-affine_points(truth, query), axis=1)
    assert error.max() < 2 and quality['p95ErrorM'] < 3
    fit = {p['referenceId'] for p in quality['landmarks'] if p['purpose'] == 'fit'}
    validation = {p['referenceId'] for p in quality['landmarks'] if p['purpose'] == 'validation'}
    assert validation and fit and not fit & validation


@pytest.mark.parametrize('shade', [1., .65])
def test_symmetric_network_cannot_silently_choose_a_direction(shade):
    image, reference, _ = printed_network(10, symmetric=True)
    image = np.rint(image*shade).astype(np.uint8)
    with pytest.raises(ValueError, match='多个相似道路'):
        road_network_register(image, reference)


def test_boundary_matches_require_training_roads_and_mutual_pixel_correspondence():
    pixels = np.array([[x, y] for y in (0, 5) for x in range(131)], float)
    observed = np.array([[0, 0], [50, 0], [50, 2], [0, 2]], float)
    matrix = np.array([[1., 0, 0], [0, -1, 100]])
    roads = [('training', [[0, 100], [100, 100]])]
    matches = boundary_path_matches(matrix, pixels, cKDTree(pixels), observed, roads)
    assert matches and any(pixel[0] > 50 for pixel, *_ in matches)
    # The unreferenced parallel path is within 12 m, but its projected
    # reference point chooses the actual training path, so it adds no anchor.
    assert all(pixel[1] == 0 and pixel[0] <= 100 and key == 'training' and error == 0
               for pixel, local, key, error in matches)
    for pixel, local, *_ in matches:
        np.testing.assert_array_equal(affine_points(matrix, [pixel])[0], local)


def test_an_absent_way_crossing_two_printed_paths_does_not_add_boundary_anchors():
    pixels = np.array([[x, y] for x in (30, 80) for y in range(-20, 21)], float)
    observed = np.array([[-10, -10], [10, -10], [10, 10], [-10, 10]], float)
    matrix = np.array([[1., 0, 0], [0, 1, 0]])
    roads = [('partly-observed', [[0, 0], [100, 0]])]
    assert not boundary_path_matches(matrix, pixels, cKDTree(pixels), observed, roads)


def test_an_unsupported_partial_way_cannot_remove_a_trusted_boundary_match():
    pixels = np.array([[x, 0] for x in range(101)], float)
    observed = np.array([[0, 0], [50, 0], [50, 2], [0, 2]], float)
    matrix = np.array([[1., 0, 0], [0, -1, 100]])
    trusted = ('trusted', [[0, 100], [100, 100]])
    partial = ('partial', [[100, 80], [100, 120]])
    tree = cKDTree(pixels)
    before = boundary_path_matches(matrix, pixels, tree, observed, [trusted], {'trusted'})
    after = boundary_path_matches(matrix, pixels, tree, observed, [partial, trusted], {'trusted'})
    assert max(pixel[0] for pixel, *_ in before) == 100
    assert {tuple(pixel) for pixel, *_ in after} == {tuple(pixel) for pixel, *_ in before}


@pytest.mark.parametrize('border,shade', [(700, 1.), (0, .8), (0, .65), (700, .65)])
def test_blank_margins_and_exposure_do_not_change_a_road_networks_geographic_position(border, shade):
    image, reference, truth = printed_network(27)
    image = np.rint(image*shade).astype(np.uint8)
    image = cv2.copyMakeBorder(image, border, border, border, border, cv2.BORDER_CONSTANT, value=(255, 255, 255))
    registration, quality = road_network_register(image, reference)
    original = np.array([[180, 210], [230, 400], [450, 530], [500, 320]])
    projected, valid = registration.forward(original+border)
    assert valid.all()
    np.testing.assert_allclose(projected, affine_points(truth, original), atol=2.)
    assert quality['p95ErrorM'] < 3
    assert np.all(registration.source >= border)


def test_a_dim_page_without_printed_paths_cannot_use_its_outline_as_registration():
    image, reference, _ = printed_network(27)
    image[:] = np.rint(np.array([155, 225, 185])*.65).astype(np.uint8)
    with pytest.raises(ValueError, match='印刷道路不足'):
        road_network_register(image, reference)


def test_erased_photo_panels_do_not_set_the_original_maps_exposure():
    image, reference, truth = printed_network(27)
    image[125:160, 80:130] = 255  # a white clearing within the coloured map
    original = np.rint(image*.8).astype(np.uint8)
    masked = original.copy(); masked[110:150, 300:400] = 255
    registration, quality = road_network_register(masked, reference, exposure_image=original)
    points = [[180, 210], [230, 400], [450, 530]]
    prediction, valid = registration.forward(points)
    assert valid.all()
    np.testing.assert_allclose(prediction, affine_points(truth, points), atol=2.)
    assert quality['pathExtraction']['neutralWhiteValue'] == 204
    assert quality['pathExtraction']['exposureReference'] == 'unmasked_map'


def test_reference_ways_missing_from_print_do_not_pull_the_matched_network():
    image, reference, truth = printed_network(27)
    # A later reference has extra paths absent from this edition of the map.
    # They must not shift the printed paths that still agree with its survey.
    extra = [{**road, 'id': f'z{i:02}',
              'points': (np.asarray(road['points'])+[35, 21]).tolist()}
             for i, road in enumerate(reference[1:25])]
    registration, quality = road_network_register(image, reference+extra)
    query = [[180, 210], [230, 400], [450, 530]]
    error = np.linalg.norm(affine_points(registration.affine, query)-affine_points(truth, query), axis=1)
    assert error.max() < 2
    fit = {p['referenceId'] for p in quality['landmarks'] if p['purpose'] == 'fit'}
    checks = {p['referenceId'] for p in quality['landmarks'] if p['purpose'] == 'validation'}
    assert checks and not fit & checks
