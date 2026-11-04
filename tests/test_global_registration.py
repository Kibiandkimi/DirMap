import math

import cv2
import numpy as np
import pytest

from dirmap import global_registration as G
from dirmap.geometry import Registration, affine_points
from dirmap.network_registration import road_network_register
from dirmap.vision import colour_masks, region_features

PAVED, BUILDING = (201, 222, 244), (128, 128, 128)


def place(seed=3, periodic=False):
    """A place of roofs and footpaths with no lake and no park outline."""
    rng = np.random.default_rng(seed)
    reference, roofs, ways = [], [], []
    if periodic:
        for x in range(-780, 781, 60):
            for y in range(-780, 781, 60):
                roofs.append(np.array([[x, y], [x+24, y], [x+24, y+24], [x, y+24]], float))
        for x in range(-810, 811, 60):
            ways.append(np.array([[x, -810], [x, 810]], float))
            ways.append(np.array([[-810, x], [810, x]], float))
    else:
        while len(roofs) < 140:
            centre = rng.uniform(-760, 760, 2)
            size = rng.uniform(10, 45, 2)
            turn = rng.uniform(0, math.pi)
            corners = np.array([[-1, -1], [1, -1], [1, 1], [-1, 1]])*size/2
            rotation = np.array([[math.cos(turn), -math.sin(turn)], [math.sin(turn), math.cos(turn)]])
            roof = corners @ rotation.T+centre
            if all(np.linalg.norm(roof.mean(axis=0)-other.mean(axis=0)) > 55 for other in roofs):
                roofs.append(roof)
        for _ in range(45):
            start = rng.uniform(-760, 760, 2)
            heading = np.cumsum(rng.normal(0, .35, 10))+rng.uniform(0, 2*math.pi)
            ways.append(start+np.cumsum(np.column_stack((np.cos(heading), np.sin(heading)))*rng.uniform(15, 30), axis=0))
    for index, roof in enumerate(roofs):
        reference.append({'id': f'b{index}', 'kind': 'building', 'tags': {'building': 'yes'}, 'points': roof.tolist(),
                          'center': roof.mean(axis=0).tolist(), 'area': float(cv2.contourArea(roof.astype(np.float32))),
                          'aspect': 1.})
    for index, way in enumerate(ways):
        reference.append({'id': f'w{index}', 'kind': 'road', 'tags': {'highway': 'footway'}, 'points': way.tolist()})
    from dirmap.reference import polygon_properties
    for feature in reference:
        if feature['kind'] == 'building':
            feature.update(polygon_properties(feature['points']))
    return reference


def sheet(reference, centre=(150., -120.), half=(330., 380.), metres_per_pixel=.8, degrees=2.):
    """A printed sheet of part of the place, and its true pixel -> metre pose."""
    theta = math.radians(degrees)
    rotation = np.array([[math.cos(theta), -math.sin(theta)], [math.sin(theta), math.cos(theta)]])
    linear = rotation @ np.diag([1/metres_per_pixel, -1/metres_per_pixel])
    window = np.array([[-half[0], -half[1]], [half[0], -half[1]], [half[0], half[1]], [-half[0], half[1]]])+centre
    corners = window @ linear.T
    offset = -corners.min(axis=0)+30
    size = np.ceil(corners.max(axis=0)+offset+30).astype(int)
    image = np.full((size[1], size[0], 3), 255, np.uint8)
    draw = lambda points: np.round((np.asarray(points) @ linear.T+offset)*4).astype(np.int32)
    cv2.fillPoly(image, [draw(window)], (180, 235, 198), cv2.LINE_AA, shift=2)
    for feature in reference:
        if feature['kind'] == 'road':
            cv2.polylines(image, [draw(feature['points'])], False, PAVED, round(4/metres_per_pixel), cv2.LINE_AA, shift=2)
    for feature in reference:
        if feature['kind'] == 'building':
            cv2.fillPoly(image, [draw(feature['points'])], BUILDING, cv2.LINE_AA, shift=2)
    outside = np.zeros(image.shape[:2], np.uint8)
    cv2.fillPoly(outside, [np.round(window @ linear.T+offset).astype(np.int32)], 255)
    image[outside == 0] = 255
    inverse = np.linalg.inv(linear)
    truth = np.column_stack((inverse, -inverse @ offset))
    features = region_features(colour_masks(image)['building'], 'building', image.shape[0]*image.shape[1], close=5)
    result = {'features': features, 'controls': [], 'sheetPhotos': [], 'width': image.shape[1], 'height': image.shape[0]}
    return image, result, truth


def outline_error(matrix, truth, result):
    corners = np.array([[0, 0], [result['width'], 0], [result['width'], result['height']], [0, result['height']]], float)
    return float(np.median(np.linalg.norm(affine_points(matrix, corners)-affine_points(truth, corners), axis=1)))


def test_search_finds_a_sheet_in_a_place_without_park_outline_or_water():
    reference = place()
    image, result, truth = sheet(reference)
    layers, footprint, _ = G.sheet_evidence(image, image, result)
    candidates = G.search(layers, footprint, reference, G.scale_range(footprint, [], reference), G.NORTH_UP_DEG)
    assert outline_error(candidates[0]['matrix'], truth, result) < 20
    assert candidates[0]['score'] > candidates[1]['score']
    # The park-outline route has nothing to start from here.
    with pytest.raises(ValueError):
        road_network_register(image, reference)


def test_global_registration_overrules_a_confident_seed_in_the_wrong_place():
    reference = place()
    image, result, truth = sheet(reference)
    wrong = truth.copy()
    wrong[:, 2] += [420., 260.]
    seeded = (Registration(wrong), {'method': 'water_seed_observed_outline_ransac_mesh', 'p95ErrorM': 9.})
    reg, quality = G.register(image, image, result, reference, established=[seeded])
    assert reg is not seeded[0] and quality['method'].startswith('global_')
    assert outline_error(reg.affine, truth, result) < 5
    rejected = [a for a in quality['poseSupport']['alternatives'] if a['method'].startswith('water_seed')]
    assert rejected and not rejected[0]['agreesWithChosen']


def test_an_established_result_that_agrees_is_kept_unchanged():
    reference = place()
    image, result, truth = sheet(reference)
    seeded = (Registration(truth), {'method': 'building_seed_observed_outline_ransac_mesh', 'p95ErrorM': 2.})
    reg, quality = G.register(image, image, result, reference, established=[seeded])
    assert reg is seeded[0] and quality['method'] == 'building_seed_observed_outline_ransac_mesh'
    assert quality['poseSupport']['value'] > G.MINIMUM_SUPPORT


def test_a_repetitive_place_is_refused_rather_than_guessed():
    reference = place(periodic=True)
    image, result, _ = sheet(reference, metres_per_pixel=.6)
    with pytest.raises(ValueError, match='吻合度相近|同样吻合'):
        G.register(image, image, result, reference)


def test_block_correlation_measures_a_searched_pose_with_left_out_blocks():
    reference = place()
    image, result, truth = sheet(reference)
    layers, footprint, _ = G.sheet_evidence(image, image, result)
    seed = truth.copy()
    seed[:, 2] += [9., -7.]  # a coarse search pose
    seed[:, :2] *= 1.015
    reg, quality = G.layer_correlation_register(layers, footprint, reference, seed)
    assert quality['leaveOneOut'] and quality['fitCount'] >= 6
    assert quality['p95ErrorM'] < 8
    assert outline_error(reg.affine, truth, result) < 4


def test_choose_refuses_a_close_competitor_and_low_support(monkeypatch):
    footprint = np.array([[0, 0], [1000, 0], [1000, 1000], [0, 1000]], float)
    here = np.array([[.5, 0, 0], [0, -.5, 0]])
    there = np.array([[.5, 0, 300], [0, -.5, 0]])
    values = {0.: .50, 300.: .48}
    monkeypatch.setattr(G, 'support', lambda layers, footprint, reference, matrix: (values[float(matrix[0, 2])], {}))
    attempts = [(Registration(here), {'method': 'a'}), (Registration(there), {'method': 'b'})]
    with pytest.raises(ValueError, match='吻合度相近'):
        G.choose(attempts, {}, footprint, [])
    values[300.] = .30
    reg, quality = G.choose(attempts, {}, footprint, [])
    assert quality['method'] == 'a' and quality['poseSupport']['margin'] == pytest.approx(.2)
    # An unconfirmed searched pose elsewhere scoring as well also blocks it.
    with pytest.raises(ValueError, match='同样吻合'):
        G.choose(attempts, {}, footprint, [], searched=[{'score': .49, 'matrix': there}])
    values[0.] = .1
    values[300.] = .05
    with pytest.raises(ValueError, match='吻合度过低'):
        G.choose(attempts, {}, footprint, [])


def test_wide_tan_paving_is_read_only_when_beige_paving_is_absent():
    from dirmap.network_registration import printed_paths
    image = np.full((700, 700, 3), 255, np.uint8)
    cv2.rectangle(image, (60, 60), (640, 640), (60, 200, 255), -1)  # open land
    for x in range(120, 600, 80):
        cv2.line(image, (x, 80), (x, 620), (160, 192, 232), 9)    # S about 80 tan
        cv2.line(image, (80, x), (620, x), (160, 192, 232), 9)
    diagnostics = {}
    pixels, _, _ = printed_paths(image, diagnostics=diagnostics)
    assert len(pixels) >= 200 and diagnostics['pathSaturationMax'] == 100
    beige = image.copy()
    beige[np.all(image == (160, 192, 232), axis=2)] = PAVED
    diagnostics = {}
    printed_paths(beige, diagnostics=diagnostics)
    assert 'pathSaturationMax' not in diagnostics


def test_an_unexpected_fault_in_the_global_check_keeps_the_seeded_result(monkeypatch):
    from dirmap import service
    seeded = (Registration(np.array([[.5, 0, 0], [0, -.5, 0]])), {'method': 'water_seed_observed_outline_ransac_mesh'})
    monkeypatch.setattr(service, 'auto_register', lambda *args, **kwargs: seeded)

    def no_park(*args, **kwargs):
        raise ValueError('场地缺少可核对的公园轮廓与道路网络')

    def broken(*args, **kwargs):
        raise RuntimeError('unexpected')
    monkeypatch.setattr(service, 'road_network_register', no_park)
    monkeypatch.setattr(service.global_registration, 'register', broken)
    image = np.full((50, 50, 3), 255, np.uint8)
    result = {'features': [], 'controls': [], 'sheetPhotos': [], 'width': 50, 'height': 50}
    reg, quality, failures = service.geographic_registration(image, result, [])
    assert reg is seeded[0] and quality is seeded[1]
    assert any('全局核查出错' in failure for failure in failures)
