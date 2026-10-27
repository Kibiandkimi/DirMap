import cv2
import numpy as np

from dirmap.geometry import Registration, affine_points
from dirmap.reference import polygon_properties
from dirmap.registration import Matcher, extend_transfer_boundary, fit_observed_landmarks, partial_feature_corner_matches, recover_boundary_buildings, register_to_map


def test_outline_holdout_keeps_whole_objects_separate_and_uses_observed_boundary():
    pairs, corners = [], []
    for row in range(5):
        for col in range(5):
            key = f'building-{row}-{col}'
            point = np.array([col*30., row*30.])
            pairs.append({'referenceId': key, 'kind': 'building', 'pixel': point.tolist(), 'local': (point*[1, -1]).tolist()})
            for dx, dy in ((-4, -4), (4, -4), (4, 4), (-4, 4)):
                p = point+[dx, dy]
                corners.append({'referenceId': key, 'kind': 'building_corner', 'pixel': p.tolist(), 'local': (p*[1, -1]).tolist()})
    reg, quality = fit_observed_landmarks(pairs, corners)
    fit = {p['referenceId'] for p in quality['landmarks'] if p['purpose'] == 'fit'}
    holdout = {p['referenceId'] for p in quality['landmarks'] if p['purpose'] == 'validation'}
    assert fit and holdout and not fit & holdout
    assert quality['validationInsideHull'] == quality['validationCount']
    assert quality['p95ErrorM'] < 1e-5 and not quality['affineFallback']
    observed = {tuple(p['pixel']) for p in corners}
    assert all(tuple(p) in observed for p in reg.source)
    assert reg.forward([[-4, -4], [124, 124]])[1].all()
    assert not reg.forward([[-4.1, -4]])[1][0]


def test_clipped_building_uses_real_corners_and_excludes_the_image_cut():
    source = {'kind': 'building', **polygon_properties([[0, 20], [40, 20], [40, 60], [0, 60]])}
    target = {'id': 'real-building', 'kind': 'building', **polygon_properties([[-10, 20], [40, 20], [40, 60], [-10, 60]])}
    matcher = Matcher([source], [target])
    matches = partial_feature_corner_matches(matcher, np.array([[1, 0, 0], [0, 1, 0.]]), 100, 100)
    assert len(matches) == 2
    assert {tuple(p['pixel']) for p in matches} == {(40., 20.), (40., 60.)}
    assert all(p['pixel'] == p['local'] for p in matches)


def test_sift_duplicate_orientations_do_not_break_a_real_pixel_transfer():
    rng = np.random.default_rng(481)
    image = np.full((420, 420, 3), 240, np.uint8)
    for _ in range(180):
        p = tuple(int(v) for v in rng.integers(10, 410, 2))
        cv2.circle(image, p, int(rng.integers(2, 9)), (40, 40, 40), -1)
    source = [[0, 0], [419, 0], [419, 419], [0, 419]]
    matrix = [[.5, 0, 0], [0, -.5, 209.5]]
    base = Registration(matrix, source, affine_points(matrix, source), regularization=0)
    transferred, quality = register_to_map(image, image, base, 'base', {'p95ErrorM': 2.5})
    assert len(np.unique(transferred.source, axis=0)) == len(transferred.source)
    assert quality['validationCount'] > 0 and quality['p95ErrorM'] < 1e-3
    assert quality['inheritedGeographicP95M'] == 2.5
    predicted, valid = transferred.forward([[210, 210]])
    assert valid[0]
    np.testing.assert_allclose(predicted, [[105, 104.5]], atol=1e-3)


def test_indexed_inverse_uses_original_triangles_across_deformed_cell_boundaries():
    rng = np.random.default_rng(82)
    source = np.array([[x, y] for y in range(0, 241, 30) for x in range(0, 301, 30)], float)
    affine = np.array([[1.1, .07, 10], [.04, -.95, 220]])
    target = affine_points(affine, source)
    internal = (source[:, 0] > 0) & (source[:, 0] < 300) & (source[:, 1] > 0) & (source[:, 1] < 240)
    target[internal] += rng.uniform(-6, 6, (int(internal.sum()), 2))
    reg = Registration(affine, source, target, regularization=0)
    weights = rng.dirichlet([1, 1, 1], len(reg.tri.simplices))
    geographic = np.einsum('ij,ijk->ik', weights, reg.target[reg.tri.simplices])
    expected = np.einsum('ij,ijk->ik', weights, reg.source[reg.tri.simplices])
    np.testing.assert_allclose(reg.inverse(geographic), expected, atol=1e-7)
    np.testing.assert_allclose(reg.inverse(reg.target), reg.source, atol=1e-7)
    assert np.isnan(reg.inverse([[10000, 10000], [np.nan, 0]])).all()


def test_copy_boundary_extends_only_with_observed_fitting_corners():
    matrix = [[1, 0, 0], [0, -1, 100]]
    source = [[0, 0], [80, 0], [80, 80], [0, 80]]
    target = affine_points(matrix, source)
    reg = Registration(matrix, source, target)
    landmarks = [{'pixel': p, 'local': q.tolist(), 'purpose': 'fit'} for p, q in zip(source, target)]
    landmarks.append({'pixel': [40, 40], 'local': [40, 60], 'purpose': 'validation'})
    extra = [{'pixel': p, 'local': affine_points(matrix, [p])[0].tolist(), 'purpose': 'fit', 'kind': 'building_corner'}
             for p in ([100, 0], [100, 100], [0, 100])]
    extra.append({'pixel': [-20, 0], 'local': [-20, 100], 'purpose': 'validation', 'kind': 'building_corner'})
    updated, quality = extend_transfer_boundary(reg, {'landmarks': landmarks}, {'landmarks': extra})
    assert updated.forward([[95, 95]])[1][0]
    assert not updated.forward([[-1, 40]])[1][0]
    assert quality['geographicBoundaryAnchors'] == 3
    assert quality['p95ErrorM'] < 1e-6


def test_joined_boundary_roofs_can_recover_from_separate_observed_outlines_without_using_holdouts():
    image = np.full((160, 170, 3), 250, np.uint8)
    matrix = np.array([[1., 0, 0], [0, -1, 160]])
    references = []
    for identifier, x0, x1 in [('boundary', 80, 110), ('holdout', 114, 144)]:
        cv2.rectangle(image, (x0, 30), (x1, 60), (140, 140, 140), -1)
        points = [[x0, 30], [x1, 30], [x1, 60], [x0, 60]]
        references.append({'id': identifier, 'kind': 'building',
                           **polygon_properties(affine_points(matrix, points))})
    source = [[0, 0], [90, 0], [90, 120], [0, 120]]
    target = affine_points(matrix, source)
    reg = Registration(matrix, source, target)
    landmarks = [{'referenceId': 'base', 'pixel': p, 'local': q.tolist(), 'purpose': 'fit'}
                 for p, q in zip(source, target)]
    landmarks.append({'referenceId': 'holdout', 'pixel': [129, 45], 'local': [129, 115], 'purpose': 'validation'})
    quality = {'landmarks': landmarks, 'rejectedObjectIds': ['boundary']}
    updated, report = recover_boundary_buildings(image, reg, quality, references)
    assert updated.forward([[108, 45]])[1][0]
    assert not updated.forward([[129, 45]])[1][0]
    assert report['recoveredBoundaryObjectIds'] == ['boundary']
    assert not report['rejectedObjectIds']
    assert {p['referenceId'] for p in report['landmarks'] if p['purpose'] == 'validation'} == {'holdout'}
    np.testing.assert_array_equal(updated.target[:len(target)], reg.target)
    assert quality['landmarks'] == landmarks


def test_a_sheet_without_matching_water_is_seeded_from_its_buildings():
    # A park sheet whose river runs off the paper: only roofs to go by.
    rng = np.random.default_rng(7)
    angle, scale = np.radians(17), .6
    rotate = np.array([[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]])
    linear = scale*rotate @ np.diag([1., -1.])
    shift = np.array([-180., 240.])
    truth = np.column_stack((linear, shift))
    reference, features = [], []
    for index in range(14):
        centre = rng.uniform(-260, 260, 2)
        size = rng.uniform(14, 48, 2)
        turn = rng.uniform(0, np.pi)
        corners = np.array([[-1, -1], [1, -1], [1, 1], [-1, 1]])*size/2 @ np.array(
            [[np.cos(turn), np.sin(turn)], [-np.sin(turn), np.cos(turn)]])+centre
        reference.append({'id': f'roof-{index}', 'kind': 'building', **polygon_properties(corners)})
        pixels = np.linalg.solve(linear, (corners-shift).T).T
        features.append({'kind': 'building', **polygon_properties(pixels)})
    reference.append({'id': 'pond', 'kind': 'water', **polygon_properties([[300, 300], [340, 300], [340, 330], [300, 330]])})
    width, height = 1000, 1000
    matcher = Matcher(features, reference)
    matrix, candidates = matcher.initial(width, height)
    assert matcher.seed_kind == 'building' and candidates > 0
    pixels = [f['center'] for f in features]
    np.testing.assert_allclose(affine_points(matrix, pixels), affine_points(truth, pixels), atol=2.5)
    # The water path is unchanged: with no source water it still reports
    # why, and a sheet with too few roofs keeps that water failure.
    try:
        Matcher(features[:3], reference).initial(width, height)
    except ValueError as error:
        assert '水体' in str(error)
    else:
        raise AssertionError('three roofs must not seed a registration')
