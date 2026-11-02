from pathlib import Path

import cv2
import numpy as np
import pytest

from dirmap.sheet import photo_regions, numbered_panels, fill_panel_gaps, cartographic_image, link_photos, reconcile_photos, save_photos, archived_photo
from dirmap.images import read_map_image
from dirmap.vision import colour_masks


def album():
    image = np.full((640, 1100, 3), 255, np.uint8)
    rng = np.random.default_rng(7)
    boxes = []
    for row in range(3):
        for col in range(2):
            x, y, w, h = 660+col*180, 70+row*160, 170, 140
            photo = rng.integers(40, 205, (h, w, 3), dtype=np.uint8)
            if row == 1 and col == 1:
                photo[:] = [110, 70, 40]  # a low-texture blue sign
                cv2.putText(photo, 'GATE', (8, 60), cv2.FONT_HERSHEY_SIMPLEX, .7, (235, 235, 235), 2)
            image[y:y+h, x:x+w] = photo
            boxes.append([x, y, x+w, y+h])
    cv2.rectangle(image, (50, 50), (560, 580), (125, 220, 156), -1)
    cv2.line(image, (110, 510), (490, 100), (207, 222, 245), 12)
    return image, boxes


def overlap(a, b):
    intersection = max(0, min(a[2], b[2])-max(a[0], b[0]))*max(0, min(a[3], b[3])-max(a[1], b[1]))
    return intersection/((a[2]-a[0])*(a[3]-a[1])+(b[2]-b[0])*(b[3]-b[1])-intersection)


@pytest.mark.parametrize('separate_numbers', [True, False])
def test_faint_joined_card_gutters_need_repeated_sizes_and_a_number_on_each_photo(separate_numbers):
    image = np.full((900, 1400, 3), 255, np.uint8)
    rng = np.random.default_rng(127)
    boxes = []
    for index in range(7):
        x, y, w, h = 30+index*180, 60, 170, 140
        grey = rng.integers(40, 205, (h, w), dtype=np.uint8)
        image[y:y+h, x:x+w] = grey[:, :, None]
        if separate_numbers or index not in (1, 4):
            cv2.putText(image, str(31+index), (x+w-44, y+h-12), cv2.FONT_HERSHEY_SIMPLEX,
                        .9, (202, 49, 189), 2, cv2.LINE_AA)
        boxes.append([x, y, x+w, y+h])
    # Compression spoils about one third of two gutters, joining each pair
    # into a wide photographic component. Three single cards remain as size
    # evidence, but they no longer form an adjacent group on their own.
    for index in (1, 4):
        x = boxes[index][2]
        image[60:200:3, x:x+10] = 130
    photos = photo_regions(image)
    if separate_numbers:
        assert len(photos) == 7
        assert all(max(overlap(box, p['pixelBox']) for p in photos) > .85 for box in boxes)
    else:
        # A wide scene with one number must not be turned into two photos.
        assert photos == []


def test_photo_layout_recovers_simple_signs_and_preserves_source_coordinates(tmp_path):
    image, expected = album(); original = image.copy()
    photos = photo_regions(image)
    assert len(photos) == 6
    assert all(max(overlap(box, p['pixelBox']) for p in photos) > .9 for box in expected)
    cartography = cartographic_image(image, photos)
    np.testing.assert_array_equal(original, image)
    np.testing.assert_array_equal(cartography[300, 300], image[300, 300])
    assert np.all(cartography[100, 700] == 255)
    save_photos(image, photos, tmp_path)
    assert archived_photo(tmp_path/'sheet-photos.zip', photos[0]['id']).startswith(b'\xff\xd8')


@pytest.mark.parametrize('gap', ['photograph', 'map', 'control'])
def test_regular_album_gaps_recover_photos_without_crop_boxes_over_a_map_or_control(gap):
    image = np.full((1100, 800, 3), 255, np.uint8)
    rng = np.random.default_rng(61)
    boxes = [[80, 40+i*150, 250, 180+i*150] for i in range(6)]
    for left, top, right, bottom in boxes:
        grey = rng.integers(40, 205, (bottom-top, right-left), dtype=np.uint8)
        image[top:bottom, left:right] = grey[:, :, None]
    controls = []
    if gap == 'map':
        image[190:330, 80:250] = (125, 220, 156)
        cv2.line(image, (95, 200), (220, 310), (207, 222, 245), 9)
    elif gap == 'control':
        controls = [{'pixel': [165, 260], 'radiusPx': 20}]
        cv2.circle(image, (165, 260), 20, (202, 49, 189), 3)
    known = [{'id': f'photo-{i}', 'pixelBox': boxes[i], 'label': None} for i in (0, 3, 4, 5)]
    completed = fill_panel_gaps(image, known, controls)
    expected = boxes if gap == 'photograph' else [box for i, box in enumerate(boxes) if i != 1]
    assert len(completed) == len(expected)
    assert all(max(overlap(box, p['pixelBox']) for p in completed) > .85 for box in expected)


def test_repeated_courts_roofs_and_hatching_on_a_real_map_are_not_an_album():
    image, _ = read_map_image(Path(__file__).resolve().parents[1]/'image_1791593207568.jpg')
    assert photo_regions(image) == []


def test_photo_links_follow_labels_and_manual_edits_survive_redetection():
    controls = [{'id': 'a', 'label': '31'}, {'id': 'b', 'label': '42'}]
    old = [{'id': 'photo-7', 'pixelBox': [10, 20, 80, 90], 'label': '42', 'labelSource': 'manual'}]
    photos = reconcile_photos([{'id': 'photo-1', 'pixelBox': [11, 20, 80, 91], 'label': '31', 'labelSource': 'tesseract'}], old, controls)
    assert photos[0]['id'] == 'photo-7' and photos[0]['controlId'] == 'b'
    controls[0]['label'] = '42'
    link_photos(photos, controls)
    assert photos[0]['controlId'] is None


def test_card_numbers_recover_isolated_panels_but_not_printed_instructions():
    image = np.full((900, 1400, 3), 255, np.uint8)
    rng = np.random.default_rng(81)
    purple = (202, 49, 189)
    boxes = [[600+i*185, 220, 770+i*185, 360] for i in range(4)] + [[30, 610, 200, 750], [1080, 610, 1250, 750]]
    for i, (x0, y0, x1, y1) in enumerate(boxes):
        grey = rng.integers(40, 205, (y1-y0, x1-x0), dtype=np.uint8)
        image[y0:y1, x0:x1] = grey[:, :, None]
        cv2.putText(image, str(31+i), (x1-45, y1-10), cv2.FONT_HERSHEY_SIMPLEX, 1., purple, 2, cv2.LINE_AA)
    controls = [{'pixel': [300, 400], 'radiusPx': 22}, {'pixel': [550, 520], 'radiusPx': 22}, {'pixel': [900, 500], 'radiusPx': 22}]
    for control in controls:
        cv2.circle(image, tuple(control['pixel']), 22, purple, 3)
    cv2.rectangle(image, (0, 0), (550, 65), (200, 165, 10), -1)
    cv2.putText(image, '31', (330, 90), cv2.FONT_HERSHEY_SIMPLEX, 1., purple, 2, cv2.LINE_AA)
    cv2.putText(image, '41', (330, 128), cv2.FONT_HERSHEY_SIMPLEX, 1., purple, 2, cv2.LINE_AA)
    cv2.rectangle(image, (50, 140), (540, 200), (200, 165, 10), -1)
    cv2.putText(image, '51', (330, 224), cv2.FONT_HERSHEY_SIMPLEX, 1., purple, 2, cv2.LINE_AA)
    cv2.putText(image, '61', (330, 258), cv2.FONT_HERSHEY_SIMPLEX, 1., purple, 2, cv2.LINE_AA)
    initial = photo_regions(image)
    completed = numbered_panels(image, initial, controls)
    assert len(completed) == 6
    assert all(max(overlap(box, p['pixelBox']) for p in completed) > .8 for box in boxes)
    assert all(p['pixelBox'][1] > 160 for p in completed)


def test_park_and_campus_olive_are_restricted_but_bright_green_is_traversable():
    # Sampled from the real park's olive areas and its three vegetation inks.
    image = np.array([[[24, 178, 161], [25, 175, 167], [78, 201, 187], [20, 100, 120],
                      [112, 232, 128], [24, 216, 48], [176, 240, 192]]], np.uint8)
    masks = colour_masks(image)
    np.testing.assert_array_equal(masks['olive'][0] > 0, [True, True, True, True, False, False, False])
    assert np.all(masks['green'][0, 4:] > 0)
