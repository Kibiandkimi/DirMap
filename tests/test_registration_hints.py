import cv2
import numpy as np
import pytest

from dirmap.registration_hints import bar_windows, correct_exposure, scale_bar, without_text


def ruler(below=False, bare=False):
    image = np.full((220, 520, 3), 255, np.uint8)
    for index in range(6):
        cv2.rectangle(image, (80+60*index, 108), (140+60*index, 124),
                      (120, 120, 120) if index % 2 else (220, 255, 255), -1)
    cv2.rectangle(image, (80, 108), (440, 124), (0, 0, 0), 1)
    y = 140 if below else 80
    words = [{'text': str(index*100)+('' if bare else 'm'), 'confidence': 95,
              'box': [80+120*index-20, y, 80+120*index+20, y+14]} for index in range(4)]
    return image, words


def test_metric_labels_and_visible_bar_measure_scale_without_geography():
    image, words = ruler()
    hint = scale_bar(image, words)
    assert hint['metresPerPixel'] == pytest.approx(100/120)
    assert hint['labels'] == 4


def test_bare_numbers_need_a_nearby_metric_unit():
    image, words = ruler(below=True, bare=True)
    assert scale_bar(image, words) is None
    words.append({'text': 'metres', 'confidence': 95, 'box': [240, 161, 280, 175]})
    assert scale_bar(image, words)['metresPerPixel'] == pytest.approx(100/120)


def test_numbers_without_a_bar_and_inconsistent_ticks_are_not_scale_evidence():
    image, words = ruler()
    assert scale_bar(np.full_like(image, 255), words) is None
    words = words[:3]
    words[-1] = {**words[-1], 'text': '310m'}
    assert scale_bar(image, words) is None


def test_conflicting_printed_scales_do_not_choose_one_arbitrarily():
    image, words = ruler()
    second = [{**word, 'text': f'{index*200}m',
               'box': (np.asarray(word['box'])+[0, 220, 0, 220]).tolist()} for index, word in enumerate(words)]
    assert scale_bar(np.vstack((image, image)), words+second) is None


def test_scale_bar_windows_keep_edges_connected_by_wide_filled_blocks():
    image, _ = ruler()
    # This bar exceeds half the image width. A larger paper margin makes
    # it a plausible small ruler, including wide solid-grey intervals.
    image = cv2.copyMakeBorder(image, 0, 0, 0, 300, cv2.BORDER_CONSTANT, value=(255, 255, 255))
    assert any(x0 <= 80 and x1 >= 440 and y0 <= 108 and y1 >= 124
               for x0, y0, x1, y1 in bar_windows(image))


def test_word_filter_keeps_source_pixels_and_nontext_features():
    image = np.full((100, 200, 3), 255, np.uint8)
    square = np.array([[0, 0], [12, 0], [12, 12], [0, 12]])
    text = {'kind': 'building', 'points': (square+[20, 20]).tolist()}
    roof = {'kind': 'building', 'points': (square+[120, 20]).tolist()}
    for feature in (text, roof):
        cv2.fillPoly(image, [np.array(feature['points'])], (120, 120, 120))
    original = image.copy()
    clean, features = without_text(image, [text, roof], [{'text': 'legend', 'confidence': 95, 'box': [18, 18, 70, 35]}])
    assert features == [roof]
    assert np.array_equal(image, original)
    assert np.all(clean[20:32, 20:32] == 255)
    assert np.array_equal(clean[:, 100:], original[:, 100:])


def test_a_dark_region_cannot_brighten_an_otherwise_normal_page():
    image = np.full((100, 100, 3), 180, np.uint8)
    image[:10] = 255
    result = {'controls': [], 'features': []}
    corrected, cartography, observed, info = correct_exposure(image, image, result)
    assert corrected is image and cartography is image and observed is result
    assert info is None


def test_a_measured_ruler_supplies_scale_but_not_geographic_roofs(monkeypatch):
    from dirmap import registration_hints as hints
    from dirmap.vision import registration_features
    image, words = ruler()
    # A real nearby roof lies inside the old broad ruler-search window,
    # but outside its observed ink. Removing the ruler must keep this roof.
    cv2.rectangle(image, (190, 132), (285, 154), (128, 128, 128), -1)
    original = image.copy()
    features = registration_features(image, [])
    assert len([f for f in features if f['kind'] == 'building']) >= 4
    monkeypatch.setattr(hints, 'ruler_words', lambda image: words)
    clean, observed, measured = hints.prepare_scale_hints(image, {'features': features})
    roofs = [f for f in observed['features'] if f['kind'] == 'building']
    assert measured['scaleBar']['metresPerPixel'] == pytest.approx(100/120)
    assert len(roofs) == 1 and roofs[0]['center'][1] > 130
    assert np.all(clean[109:124, 80:440] == 255)
    assert np.array_equal(clean[132:155, 190:286], original[132:155, 190:286])
    assert np.array_equal(image, original)
