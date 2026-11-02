import os
import shutil
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from dirmap import store
from dirmap import ocr as ocr_module
from dirmap.ocr import label_controls
from dirmap.vision import analyze, read_image


def test_empty_ocr_output_does_not_count_old_numbers_or_overwrite_teacher_labels(monkeypatch):
    monkeypatch.setenv('DIRMAP_TESSERACT', 'test-no-digits')
    monkeypatch.setattr('dirmap.ocr.subprocess.run', lambda *a, **k: SimpleNamespace(stdout='level\ttext\tconf\n'))
    controls = [
        {'id': 'auto', 'pixel': [20, 20], 'radiusPx': 6, 'label': '31', 'labelSource': 'tesseract', 'labelConfidence': .98, 'state': 'candidate'},
        {'id': 'manual', 'pixel': [50, 20], 'radiusPx': 6, 'label': '老师的编号', 'labelSource': 'manual', 'state': 'candidate'},
        {'id': 'accepted', 'pixel': [50, 50], 'radiusPx': 6, 'label': '40', 'labelSource': 'tesseract', 'state': 'accepted'},
    ]
    result = label_controls(np.zeros((80, 80), np.uint8), controls)
    assert result['recognized'] == 0 and result['preserved'] == 2
    assert controls[0]['labelSource'] == 'unread' and 'labelConfidence' not in controls[0]
    assert controls[1]['label'] == '老师的编号' and controls[2]['label'] == '40'


@pytest.mark.parametrize('label', ['6', '36'])
def test_confident_digit_fragment_uses_adjacent_printed_glyphs_without_inventing_a_prefix(label, monkeypatch):
    monkeypatch.setenv('DIRMAP_TESSERACT', 'test-partial-number')
    mask = np.zeros((220, 300), np.uint8)
    cv2.circle(mask, (100, 115), 23, 255, 2)
    digit = np.zeros_like(mask)
    cv2.putText(digit, '6', (154, 80), cv2.FONT_HERSHEY_SIMPLEX, 1., 255, 2)
    mask |= digit
    if label == '36':
        cv2.putText(mask, '3', (130, 80), cv2.FONT_HERSHEY_SIMPLEX, 1., 255, 2)
    y, x = np.nonzero(digit)
    box = [int(x.min()), int(y.min()), int(x.max()+1), int(y.max()+1)]
    def partial_global(image, *args, **kwargs):
        if image.shape == mask.shape:
            return [{'label': '6', 'confidence': 97., 'box': box,
                     'center': np.array([(box[0]+box[2])/2, (box[1]+box[3])/2])}]
        # A close crop resolves the full number, but the algorithm may only
        # request it when the extra printed glyph is actually present.
        return [{'label': '36', 'confidence': 96., 'box': [131, 58, 176, 82],
                 'center': np.array([153.5, 70.])}]
    monkeypatch.setattr(ocr_module, 'number_words', partial_global)
    controls = [{'id': 'c1', 'pixel': [100, 115], 'radiusPx': 23, 'label': '待核',
                 'labelSource': 'unread', 'state': 'candidate', 'role': 'control'}]
    label_controls(mask, controls)
    assert controls[0]['label'] == label


@pytest.mark.skipif(not (os.environ.get('DIRMAP_TESSERACT') or shutil.which('tesseract')), reason='Actual low-resolution OCR requires optional Tesseract')
def test_low_resolution_sheet_numbers_are_recovered_from_fresh_detections():
    image = read_image(store.get_asset('szu-2')['path'])
    result = analyze(image)
    assert all(c['label'].startswith('待核') for c in result['controls'] if c['role'] == 'control')
    ocr = label_controls(result['masks']['purple'], result['controls'], result['masks']['course_text'])
    assert ocr['recognized'] == 6 and ocr['preserved'] == 0
    assert sorted(c['label'] for c in result['controls'] if c['role'] == 'control') == [str(i) for i in range(31, 37)]
