import math

import cv2
import numpy as np
import pytest

from dirmap import global_registration as G
from dirmap import pose_refinement as R
from dirmap.geometry import Registration, affine_points


def pose(metres_per_pixel=.8, degrees=2., shift=(-300., 340.)):
    theta = math.radians(degrees)
    rotation = np.array([[math.cos(theta), -math.sin(theta)], [math.sin(theta), math.cos(theta)]])
    linear = rotation @ np.diag([metres_per_pixel, -metres_per_pixel])
    return np.column_stack((linear, shift))


def perturbed(matrix, scale=1., degrees=0., shift=(0., 0.), about=(0., 0.)):
    theta = math.radians(degrees)
    linear = scale*np.array([[math.cos(theta), -math.sin(theta)], [math.sin(theta), math.cos(theta)]])
    about = np.asarray(about, float)
    correction = np.column_stack((linear, about-linear @ about+np.asarray(shift, float)))
    return R._compose(correction, np.asarray(matrix, float))


def place(seed=5, count=60):
    """Winding ways and rectangular roofs over a 900 m place (local metres)."""
    rng = np.random.default_rng(seed)
    reference = []
    for index in range(count):
        start = rng.uniform(-420, 420, 2)
        heading = np.cumsum(rng.normal(0, .3, 12))+rng.uniform(0, 2*math.pi)
        way = start+np.cumsum(np.column_stack((np.cos(heading), np.sin(heading)))*rng.uniform(15, 30), axis=0)
        reference.append({'id': f'w{index}', 'kind': 'road', 'tags': {'highway': 'footway'}, 'points': way.tolist()})
    for index in range(80):
        centre, size = rng.uniform(-400, 400, 2), rng.uniform(10, 30, 2)
        roof = np.array([[-1, -1], [1, -1], [1, 1], [-1, 1]])*size/2+centre
        reference.append({'id': f'b{index}', 'kind': 'building', 'tags': {'building': 'yes'}, 'points': roof.tolist()})
    return reference


def printed(reference, truth, kinds=('road', 'building'), clutter=0, seed=1):
    """Sheet linework (pixels) of the place under the true pose, plus unmapped ink."""
    rng = np.random.default_rng(seed)
    inverse = np.linalg.inv(truth[:, :2])
    to_pixels = lambda local: (np.asarray(local, float)-truth[:, 2]) @ inverse.T
    lines = {'road': [], 'building': []}
    for feature in reference:
        if feature['kind'] in kinds:
            points, _ = R._polyline(feature['points'], .8, closed=feature['kind'] == 'building')
            lines[feature['kind']].append(to_pixels(points+rng.normal(0, .3, points.shape)))
    for _ in range(clutter):
        start = rng.uniform(-420, 420, 2)
        heading = np.cumsum(rng.normal(0, .3, 8))+rng.uniform(0, 2*math.pi)
        way = start+np.cumsum(np.column_stack((np.cos(heading), np.sin(heading)))*20, axis=0)
        lines['road'].append(to_pixels(R._polyline(way, .8)[0]))
    empty = np.empty((0, 2))
    return {'ink': np.vstack(lines['road']+lines['building']) if lines['road'] or lines['building'] else empty,
            'roof': np.vstack(lines['building']) if lines['building'] else empty}


FOOTPRINT = np.array([[0, 0], [760, 0], [760, 820], [0, 820]], float)


def corner_error(reg, truth):
    return float(np.max(np.linalg.norm(reg.forward(FOOTPRINT, strict=False)[0]-affine_points(truth, FOOTPRINT), axis=1)))


def test_a_scale_and_rotation_bias_is_corrected_and_confirmed():
    reference, truth = place(), pose()
    sheet = printed(reference, truth, clutter=15)
    centre = affine_points(truth, FOOTPRINT).mean(axis=0)
    accepted = Registration(perturbed(truth, 1.016, 1., (3., -2.), centre))
    assert corner_error(accepted, truth) > 8
    refined, report = R.refine(accepted, FOOTPRINT, sheet, R.reference_linework(reference))
    assert report['decision'] == 'adopt', report
    assert corner_error(refined, truth) < 1.5
    assert report['gainPercent'] >= R.ADOPT_GAIN_PERCENT and set(report['layerGainsM']) == {'ink', 'roof'}
    assert R.unrefined(refined, report).affine == pytest.approx(accepted.affine, abs=1e-3)


def test_an_already_aligned_sheet_is_left_unchanged():
    reference, truth = place(), pose()
    refined, report = R.refine(Registration(truth), FOOTPRINT, printed(reference, truth, clutter=15),
                               R.reference_linework(reference))
    assert report['decision'] == 'keep' and report['reason'] == 'small_gain'
    assert report['moveP95M'] < 1.


def test_linework_kinds_that_disagree_do_not_refine_the_pose():
    reference, truth = place(), pose()
    centre = affine_points(truth, FOOTPRINT).mean(axis=0)
    other = perturbed(truth, 1.02, 0., (0., 0.), centre)
    # Ink and roofs printed under two different poses: the roofs stand
    # where the accepted pose puts them, the ink 1.2-1.6 m per 100 m off.
    sheet = {'ink': printed(reference, other, kinds=('road',))['ink'], 'roof': printed(reference, truth)['roof']}
    refined, report = R.refine(Registration(truth), FOOTPRINT, sheet, R.reference_linework(reference))
    assert report['gainPercent'] >= R.ADOPT_GAIN_PERCENT
    assert report['decision'] != 'adopt' and 'held_out_layer' in report['reason']


def test_a_far_unconfirmed_correction_marks_the_pose_unreliable():
    reference, truth = place(), pose()
    centre = affine_points(truth, FOOTPRINT).mean(axis=0)
    accepted = perturbed(truth, 1.035, 1.5, (6., 0.), centre)
    # Only ink is printed where the place is; roofs agree with the
    # accepted (wrong) pose, so no held-out kind confirms the move.
    sheet = {'ink': printed(reference, truth, kinds=('road',))['ink'], 'roof': printed(reference, accepted)['roof']}
    _, report = R.refine(Registration(accepted), FOOTPRINT, sheet, R.reference_linework(reference))
    assert report['moveP95M'] >= R.UNRELIABLE_MOVE_M
    assert report['decision'] == 'unreliable', report


def test_a_correction_keeps_the_measured_mesh_and_undoes_exactly():
    source = np.array([[100, 100], [600, 120], [580, 700], [120, 690], [350, 400]], float)
    base = pose()
    target = affine_points(base, source)+[[1, 0], [0, 1], [-1, 0], [0, -1], [.5, .5]]
    reg = Registration(base, source, target, regularization=0)
    correction = np.array([[1.01, -.01, 2.], [.01, 1.01, -3.]])
    moved = R.corrected(reg, correction)
    assert moved.forward(source)[0] == pytest.approx(affine_points(correction, reg.forward(source)[0]))
    back = R.unrefined(moved, {'correction': correction.tolist()})
    assert back.forward(source)[0] == pytest.approx(reg.forward(source)[0])


def test_sheet_linework_reads_thin_black_strokes_and_water_edges_not_solid_fills():
    image = np.full((400, 400, 3), 255, np.uint8)
    cv2.line(image, (40, 60), (360, 60), (0, 0, 0), 2)
    cv2.rectangle(image, (60, 120), (160, 220), (0, 0, 0), -1)
    cv2.rectangle(image, (220, 220), (340, 340), (235, 205, 120), -1)
    footprint = np.array([[0, 0], [400, 0], [400, 400], [0, 400]], float)
    lines = R.sheet_linework(image, footprint, {'path': np.empty((0, 2))}, pose(metres_per_pixel=.8))
    ink = lines['ink']
    assert len(ink) and np.all(np.abs(ink[:, 1]-61) < 3)
    water = lines['water']
    assert len(water) and np.all(np.minimum.reduce([np.abs(water[:, 0]-220), np.abs(water[:, 0]-340),
                                                     np.abs(water[:, 1]-220), np.abs(water[:, 1]-340)]) < 3)


def test_an_unreliable_refinement_refuses_and_offers_the_accepted_fit(monkeypatch):
    from dirmap import service
    seeded = (Registration(pose()), {'method': 'water_seed_observed_outline_ransac_mesh', 'p95ErrorM': 3.})
    monkeypatch.setattr(service, 'auto_register', lambda *args, **kwargs: seeded)

    def no_park(*args, **kwargs):
        raise ValueError('场地缺少可核对的公园轮廓与道路网络')

    def nothing(*args, **kwargs):
        raise G.NoEvidence('参照与原图没有共同的建筑、水体或道路可供搜索')

    def unreliable(image, cartography, result, reference, reg, quality):
        raise G.UnreliablePose('原配准不可靠', (.4, reg, {**quality, 'denseRefinement': {'decision': 'unreliable'}}))
    monkeypatch.setattr(service, 'road_network_register', no_park)
    monkeypatch.setattr(service.global_registration, 'register', nothing)
    monkeypatch.setattr(service.global_registration, 'dense_refinement', unreliable)
    image = np.full((400, 400, 3), 255, np.uint8)
    result = {'features': [], 'controls': [], 'sheetPhotos': [], 'width': 400, 'height': 400}
    places = []
    reg, quality, failures = service.geographic_registration(image, result, [], places)
    assert reg is None and any('不可靠' in failure for failure in failures)
    assert len(places) == 1 and places[0][1] is seeded[0]


def test_a_refined_pose_that_loses_whole_sheet_support_is_not_applied(monkeypatch):
    accepted = Registration(pose())
    elsewhere = Registration(perturbed(pose(), shift=(40., 0.)))
    monkeypatch.setattr(R, 'refine', lambda *args, **kwargs: (elsewhere, {'decision': 'adopt', 'correction': np.eye(2, 3).tolist()}))
    scores = {accepted.affine.tobytes(): (.5, {'building': .5, 'path': .5}), elsewhere.affine.tobytes(): (.3, {'building': .3, 'path': .3})}
    monkeypatch.setattr(G, 'support', lambda layers, footprint, reference, matrix, **kwargs: scores[np.asarray(matrix).tobytes()])
    image = np.full((300, 300, 3), 255, np.uint8)
    result = {'features': [], 'controls': [], 'sheetPhotos': [], 'width': 300, 'height': 300}
    reg, quality = G.dense_refinement(image, image, result, [], accepted, {'method': 'global_search_road_network'})
    assert reg is accepted
    assert quality['denseRefinement']['decision'] == 'keep' and 'support' in quality['denseRefinement']['reason']
