import math

import cv2
import numpy as np
import pytest

from dirmap import global_registration as G
from dirmap.geometry import Registration, affine_points
from dirmap.network_registration import road_network_register
from dirmap.vision import colour_masks, region_features, registration_features, water_under_overprint

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
    # A cropped refinement window must not inflate the score used to
    # veto a fitted result elsewhere in the place.
    for candidate in candidates:
        assert candidate['score'] == pytest.approx(G.support(layers, footprint, reference, candidate['matrix'])[0], abs=5e-5)
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


def test_a_place_with_two_identical_quarters_offers_both_to_the_teacher():
    base = place()
    image, result, truth = sheet(base, half=(260., 300.))
    # Copy the sheet's quarter of the place 900 m east, beside the place.
    window = lambda f: all(abs(x-150) < 330 and abs(y+120) < 370 for x, y in f['points'])
    copied = [{**f, 'id': 'copy-'+f['id'], 'points': (np.asarray(f['points'])+[900, 0]).tolist(),
               **({'center': (np.asarray(f['center'])+[900, 0]).tolist()} if 'center' in f else {})}
              for f in base if window(f)]
    with pytest.raises(G.AmbiguousPlace) as refusal:
        G.register(image, image, result, base+copied)
    places = refusal.value.places
    assert 2 <= len(places) <= G.OFFERED_PLACES
    assert all(value >= G.MINIMUM_SUPPORT for value, *_ in places)
    errors = sorted(outline_error(reg.affine, truth, result) for _, reg, _ in places)
    shifted = truth.copy(); shifted[:, 2] += [900, 0]
    assert errors[0] < 8
    assert any(outline_error(reg.affine, shifted, result) < 8 for _, reg, _ in places)


def test_an_unrelated_place_is_refused_after_all_image_evidence_retries():
    image, result, _ = sheet(place(seed=7))
    with pytest.raises(ValueError):
        G.register(image, image, result, place(seed=23))


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


@pytest.mark.parametrize('degrees', [91., 37.])
def test_a_rotated_sheet_is_found_after_the_north_up_search_refuses(degrees):
    reference = place()
    image, result, truth = sheet(reference, degrees=degrees)
    reg, quality = G.register(image, image, result, reference)
    assert outline_error(reg.affine, truth, result) < 6
    assert any(abs(candidate['rotationDeg']) > 45 for candidate in quality['globalSearch']['candidates'])


def test_ink_outside_the_map_does_not_become_evidence_when_blurred():
    footprint = np.array([[0, 0], [64, 0], [64, 64], [0, 64]], float)
    layers = {'building': [np.array([[66, 20], [68, 20], [68, 30], [66, 30]], float)],
              'water': [], 'path': np.empty((0, 2))}
    drawn, *_ = G._draw(layers, footprint, np.eye(2), 1., ['building'])
    assert not drawn


def test_a_candidate_with_no_local_buildings_still_counts_the_building_layer():
    footprint = np.array([[0, 0], [400, 0], [400, 400], [0, 400]], float)
    square = np.array([[0, 0], [20, 0], [20, 20], [0, 20]], float)
    layers = {'building': [square+[x, 40] for x in (40, 150, 260)],
              'water': [square*[1, 2]+[x, 200] for x in (50, 160, 270)],
              'path': np.empty((0, 2))}
    reference = [{'kind': kind, 'points': (points*[1, -1]).tolist()}
                 for kind in ('building', 'water') for points in layers[kind]]
    reference += [{'kind': 'water', 'points': (points*[1, -1]+[1000, 0]).tolist()}
                  for points in layers['water']]
    wrong = np.array([[1, 0, 1000], [0, -1, 0]], float)
    value, measured = G.support(layers, footprint, reference, wrong)
    assert measured['building'] == 0
    assert measured['water'] > .8
    assert value < measured['water']-.2


def test_deep_paving_and_dashed_paths_supply_fallback_evidence_without_solid_roofs():
    from dirmap.network_registration import printed_paths
    image = np.full((700, 700, 3), (180, 235, 198), np.uint8)
    footprint = np.array([[0, 0], [699, 0], [699, 699], [0, 699]], float)
    for y in (80, 160, 240):
        cv2.line(image, (50, y), (650, y), (120, 160, 210), 8)
    with pytest.raises(ValueError):
        printed_paths(image)
    pixels, *_ = G.expanded_paths(image, footprint)
    assert len(pixels) > 1000
    assert np.mean(np.min(np.abs(pixels[:, 1, None]-[80, 160, 240]), axis=1) <= 3) > .98
    for y in (380, 450, 520):
        for x in range(50, 650, 8):
            cv2.line(image, (x, y), (x+5, y), (0, 0, 0), 2)
    cv2.rectangle(image, (280, 570), (410, 670), (0, 0, 0), -1)
    pixels, *_ = G.expanded_paths(image, footprint, linear_ink=True)
    assert np.count_nonzero(np.min(np.abs(pixels[:, 1, None]-[380, 450, 520]), axis=1) <= 3) > 1000
    assert not np.any((pixels[:, 0] > 280) & (pixels[:, 0] < 410) & (pixels[:, 1] > 570))
    with pytest.raises(G.NoEvidence):
        G.expanded_paths(np.full_like(image, (120, 160, 210)), footprint)


def test_extra_road_coverage_does_not_override_a_checked_object_fit(monkeypatch):
    footprint = np.array([[0, 0], [500, 0], [500, 500], [0, 500]], float)
    matrix = np.array([[1, 0, 0], [0, -1, 0]], float)
    small = footprint*.6
    objects = Registration(matrix, small, affine_points(matrix, small))
    roads = Registration(matrix, footprint, affine_points(matrix, footprint))
    monkeypatch.setattr(G, 'support', lambda *args: (.6, {}))
    controls = [[100, 100], [450, 450]]
    assert objects.forward(controls)[1].sum() < roads.forward(controls)[1].sum()
    chosen, _ = G.choose([(roads, {'method': 'global_search_road_network', 'p95ErrorM': 17}),
                          (objects, {'method': 'global_seed_observed_outline_ransac_mesh',
                                     'p95ErrorM': 4, 'validationObjectCount': 4})],
                         {}, footprint, [], controls=controls)
    assert chosen is objects
    chosen, _ = G.choose([(roads, {'method': 'global_search_road_network'}),
                          (objects, {'method': 'global_search_layer_correlation'})],
                         {}, footprint, [], controls=controls)
    assert chosen is roads
    chosen, _ = G.choose([(roads, {'method': 'global_search_road_network', 'p95ErrorM': 9}),
                          (objects, {'method': 'global_seed_observed_outline_ransac_mesh',
                                     'p95ErrorM': 4, 'validationObjectCount': 4})],
                         {}, footprint, [], controls=controls)
    assert chosen is roads


def test_a_nearby_preferred_fit_must_pass_its_own_support_check(monkeypatch):
    footprint = np.array([[0, 0], [500, 0], [500, 500], [0, 500]], float)
    matrix = np.array([[1, 0, 0], [0, -1, 0]], float)
    good = Registration(matrix)
    shifted = matrix.copy()
    shifted[0, 2] = 10
    weak = Registration(shifted)
    values = {0: .6, 10: .1}
    monkeypatch.setattr(G, 'support', lambda layers, footprint, reference, matrix: (values[matrix[0, 2]], {}))
    attempts = [(good, {'method': 'global_seed_observed_outline_ransac_mesh'}), (weak, {'method': 'prior'})]
    chosen, _ = G.choose(attempts, {}, footprint, [], preferred=[attempts[1]])
    assert chosen is good
    values[10] = .55
    chosen, quality = G.choose(attempts, {}, footprint, [], preferred=[attempts[1]])
    assert chosen is weak
    assert quality['poseSupport']['value'] == .55


def test_a_misaligned_fit_of_the_right_place_yields_to_a_better_supported_search_pose():
    # Look-alike roofs can carry a seeded fit some tens of metres off and
    # still pass its own held-out check. Being near the searched pose of
    # its place must not exempt it from refitting from that pose.
    reference = place()
    image, result, truth = sheet(reference)
    near = truth.copy()
    near[:, 2] += [28., -12.]
    seeded = (Registration(near), {'method': 'water_seed_observed_outline_ransac_mesh', 'p95ErrorM': 9.})
    reg, quality = G.register(image, image, result, reference, established=[seeded])
    assert reg is not seeded[0]
    assert outline_error(reg.affine, truth, result) < 5
    assert quality['poseSupport']['value'] > G.support(*G.sheet_evidence(image, image, result)[:2], reference, near)[0]


def test_a_fit_clearly_less_supported_than_a_search_pose_of_its_place_is_refused(monkeypatch):
    footprint = np.array([[0, 0], [1000, 0], [1000, 1000], [0, 1000]], float)
    fit = np.array([[.5, 0, 0], [0, -.5, 0]])
    near = np.array([[.5, 0, 30], [0, -.5, 0]])  # within a search pose's error, not the same pose
    monkeypatch.setattr(G, 'support', lambda layers, footprint, reference, matrix: (.48, {}))
    attempts = [(Registration(fit), {'method': 'water_seed_observed_outline_ransac_mesh'})]
    with pytest.raises(ValueError, match='未通过最终位置核查'):
        G.choose(attempts, {}, footprint, [], searched=[{'score': .77, 'matrix': near}])
    # A coarse pose that is not clearly better is no evidence against it.
    reg, _ = G.choose(attempts, {}, footprint, [], searched=[{'score': .5, 'matrix': near}])
    assert reg is attempts[0][0]
    assert G.covered({'score': .5, 'matrix': near}, [(fit, .48)], footprint)
    assert not G.covered({'score': .77, 'matrix': near}, [(fit, .48)], footprint)


def test_offered_places_report_the_selected_fits_own_support(monkeypatch):
    footprint = np.array([[0, 0], [500, 0], [500, 500], [0, 500]], float)
    matrix = np.array([[1, 0, 0], [0, -1, 0]], float)
    approximate = Registration(matrix)
    matrix = matrix.copy(); matrix[0, 2] = 5
    precise = Registration(matrix)
    values = {0: .6, 5: .35}
    monkeypatch.setattr(G, 'support', lambda layers, footprint, reference, matrix: (values[matrix[0, 2]], {}))
    offered = G.distinct_places([(approximate, {'p95ErrorM': 10}), (precise, {'p95ErrorM': 3})],
                                [({}, footprint)], [])
    assert len(offered) == 1 and offered[0][1] is precise
    assert offered[0][0] == .35


def test_dim_paving_is_not_used_as_building_evidence():
    from dirmap import service
    reference = place()
    image, result, truth = sheet(reference)
    dim = np.rint(image*.8).astype(np.uint8)
    original = dim.copy()
    result = {**result, 'features': registration_features(dim, result['controls'])}
    reg, quality, _ = service.geographic_registration(dim, result, reference)
    assert reg is not None
    assert outline_error(reg.affine, truth, result) < 6
    assert quality['exposureCorrection']['whiteValue'] == pytest.approx(204, abs=1)
    assert np.array_equal(dim, original)


def test_enlarging_purple_hatching_does_not_fragment_a_lake_into_false_landmarks():
    image = np.full((700, 700, 3), 255, np.uint8)
    cv2.rectangle(image, (80, 100), (260, 260), (225, 165, 65), -1)
    for x in range(80, 261, 15):
        cv2.line(image, (x, 100), (x, 260), (180, 50, 180), 3)
    for y in range(100, 261, 15):
        cv2.line(image, (80, y), (260, y), (180, 50, 180), 3)
    areas = []
    for factor in (1, 2, 3):
        enlarged = cv2.resize(image, None, fx=factor, fy=factor, interpolation=cv2.INTER_CUBIC)
        masks = colour_masks(enlarged)
        original_water, original_purple = masks['water'].copy(), masks['purple'].copy()
        water = [f for f in registration_features(enlarged, [], masks) if f['kind'] == 'water']
        assert len(water) == 1
        areas.append(water[0]['area']/factor**2)
        assert np.array_equal(masks['water'], original_water) and np.array_equal(masks['purple'], original_purple)
    assert min(areas) > 26000 and max(areas)/min(areas) < 1.02


def test_water_recovery_does_not_fill_visible_land_or_distant_purple_ink():
    water = np.zeros((150, 250), np.uint8)
    water[30:120, 20:100] = 255
    water[30:120, 110:190] = 255
    purple = np.zeros_like(water)
    purple[65:70, 20:190] = 255
    purple[10:140, 210:230] = 255
    water[purple > 0] = 0
    repaired = water_under_overprint(water, purple)
    assert repaired[67, 50] == 255 and repaired[67, 150] == 255
    assert np.all(repaired[30:65, 100:110] == 0)
    assert np.all(repaired[:, 210:230] == 0)


def test_black_road_rims_cannot_be_closed_into_grey_roof_landmarks():
    image = np.full((600, 800, 3), 255, np.uint8)
    cv2.rectangle(image, (60, 60), (150, 120), BUILDING, -1)
    cv2.rectangle(image, (250, 200), (650, 500), (0, 0, 0), 2, cv2.LINE_AA)
    cv2.putText(image, 'ROAD', (300, 140), cv2.FONT_HERSHEY_SIMPLEX, 2., (0, 0, 0), 2, cv2.LINE_AA)
    masks = colour_masks(image)
    before = masks['building'].copy()
    grey = [f for f in registration_features(image, [], masks) if f['kind'] == 'building' and f.get('ink') != 'black']
    assert len(grey) == 1 and np.allclose(grey[0]['center'], [105, 90], atol=1)
    assert np.array_equal(masks['building'], before)
