"""Grey-white lower-level texture must not erase real roofs or barriers."""
import cv2
import numpy as np
import pytest

from dirmap.hatching import lower_level_hatching
from dirmap.vision import analyze


def image_fixture():
    image = np.full((400, 400, 3), 250, np.uint8)
    y, x = np.indices(image.shape[:2])
    area = (x >= 50) & (x < 350) & (y >= 50) & (y < 350)
    stripes = area & ((x+y) % 12 < 4)
    image[stripes] = 175
    image[125:230, 125:245] = 175
    image[85:90, 90:95] = 20
    image[55:345, 274:277] = 20
    controls = [{'pixel': [35, 35], 'radiusPx': 26}, {'pixel': [365, 365], 'radiusPx': 26}]
    return image, stripes, controls


@pytest.mark.parametrize('scale', [.5, 1, 2])
def test_repeated_hatching_opens_while_roofs_and_black_features_stay_separate(scale):
    image, _, controls = image_fixture()
    image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_NEAREST)
    controls = [{**control, 'radiusPx': control['radiusPx']*scale} for control in controls]
    stripes, area, features = lower_level_hatching(image, controls)
    assert np.count_nonzero(stripes) > 200*scale*scale
    assert features and area.any()
    assert not stripes[round(135*scale):round(220*scale), round(135*scale):round(235*scale)].any()
    assert not stripes[round(85*scale):round(90*scale), round(90*scale):round(95*scale)].any()
    assert not stripes[round(60*scale):round(340*scale), round(275*scale)].any()


def test_single_diagonal_roof_or_black_stair_pattern_is_not_lower_hatching():
    for kind in ('roof', 'stairs', 'purple'):
        image = np.full((400, 400, 3), 250, np.uint8)
        if kind == 'roof':
            cv2.fillConvexPoly(image, np.array([[50, 160], [160, 50], [330, 220], [220, 330]], np.int32), (175, 175, 175))
        else:
            for offset in range(100, 350, 12):
                cv2.line(image, (offset, 60), (offset-120, 180), (20, 20, 20) if kind == 'stairs' else (180, 30, 165), 3)
        stripes, _, _ = lower_level_hatching(image, [{'radiusPx': 26}, {'radiusPx': 26}])
        assert not stripes.any()


def test_terrain_interpretation_keeps_buildings_pillars_and_walls_after_removing_hatching():
    image, _, controls = image_fixture()
    for control in controls:
        cv2.circle(image, tuple(control['pixel']), control['radiusPx'], (190, 35, 165), 3, cv2.LINE_AA)
    result = analyze(image)
    assert result['statistics']['lowerLevelHatchingPixels'] > 100
    assert result['semantics']['lower_level'].any()
    assert np.all(result['costs'][145:205, 145:220] == 0)
    assert np.all(result['costs'][85:90, 90:95] == 0)
    assert np.all(result['costs'][60:340, 275] == 0)
    y, x = np.indices((60, 60))
    region = result['costs'][250:310, 170:230]
    assert np.mean(region > 0) > .8


def test_confirmed_hatching_continues_into_an_adjacent_broken_band_only():
    image = np.full((420, 420, 3), 250, np.uint8)
    y, x = np.indices(image.shape[:2])
    pattern = (x+y) % 12 < 4
    # Scanned stripes broken into short dashes cannot confirm a band alone.
    broken = pattern & ((x-y) % 20 < 15)
    wide = (x >= 40) & (x < 260) & (y >= 40) & (y < 260)
    attached = (x >= 260) & (x < 400) & (y >= 120) & (y < 140)
    isolated = (x >= 40) & (x < 260) & (y >= 360) & (y < 380)
    image[wide & pattern] = 175
    image[(attached | isolated) & broken] = 175
    controls = [{'pixel': [20, 20], 'radiusPx': 26}, {'pixel': [400, 400], 'radiusPx': 26}]
    stripes, area, features = lower_level_hatching(image, controls)
    far = attached & (x >= 300)
    assert np.mean(stripes[far & broken] > 0) > .8
    assert not stripes[isolated].any() and not area[isolated].any()
    assert area[130, 380] and len(features) == 1


def test_registration_outlines_keep_the_hatched_building_footprint(monkeypatch):
    image, _, controls = image_fixture()
    for control in controls:
        cv2.circle(image, tuple(control['pixel']), control['radiusPx'], (190, 35, 165), 3, cv2.LINE_AA)
    result = analyze(image)
    assert result['statistics']['lowerLevelHatchingPixels'] > 100
    import dirmap.vision as vision
    monkeypatch.setattr(vision, 'lower_level_hatching', lambda image, controls: (
        np.zeros(image.shape[:2], np.uint8), np.zeros(image.shape[:2], np.uint8), []))
    baseline = analyze(image)
    assert result['features'] == baseline['features']
    assert np.count_nonzero(result['costs'] > 0) > np.count_nonzero(baseline['costs'] > 0)
