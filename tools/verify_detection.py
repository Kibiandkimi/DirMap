#!/usr/bin/env python3
"""Verify raw detections on the four supplied sheets and image perturbations.

No saved controls or teacher corrections are used. These are development
images and a visual label/approximate-position inventory, not a field survey
or proof of generalization to arbitrary orienteering maps.
"""
from collections import Counter
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cv2
import numpy as np
from dirmap import store
from dirmap.images import read_map_image
from dirmap.ocr import label_controls
from dirmap.symbols import classify_course_symbols
from dirmap.vision import colour_masks, detect_controls


def check(image, name, labels, expected_positions=None, start_position=None, tolerance_px=None):
    started = time.perf_counter()
    masks = colour_masks(image)
    controls = detect_controls(masks['course_ink'], image)
    classify_course_symbols(masks['course_ink'], controls)
    ocr = label_controls(masks['purple'], controls, masks['course_text'])
    normal = [c for c in controls if c['role'] == 'control']
    actual = Counter(c['label'] for c in normal)
    roles = Counter(c['role'] for c in controls)
    position_errors = {}
    if expected_positions:
        for label, point in expected_positions.items():
            matching = [c for c in normal if c['label'] == label]
            position_errors[label] = float(np.linalg.norm(np.array(matching[0]['pixel'])-point)) if len(matching) == 1 else None
    if start_position is not None:
        matching = [c for c in controls if c['role'] == 'start_finish']
        position_errors['start_finish'] = float(np.linalg.norm(np.array(matching[0]['pixel'])-start_position)) if len(matching) == 1 else None
    passed = (actual == Counter(labels) and roles == {'control': len(labels), 'start_finish': 1}
              and all(error is not None and error <= tolerance_px for error in position_errors.values()))
    result = {'case': name, 'workingSize': list(image.shape[1::-1]), 'controlCount': len(normal),
              'labels': sorted(actual.elements()), 'roleCounts': dict(roles), 'passed': passed,
              'positionErrorsPx': position_errors, 'positionTolerancePx': tolerance_px,
              'ocr': ocr, 'elapsedSec': round(time.perf_counter()-started, 3),
              'controls': [{key: c.get(key) for key in ('label', 'role', 'pixel', 'radiusPx', 'labelConfidence')} for c in controls]}
    print({k: result[k] for k in ('case', 'controlCount', 'roleCounts', 'passed', 'elapsedSec')}, flush=True)
    return result


def main():
    fixture = store.read_json(store.ROOT/'evidence/new-map-fixture.json')
    override = sys.argv[1] if len(sys.argv) > 1 else os.environ.get('DIRMAP_RECOGNITION_SOURCE')
    path = Path(override) if override else store.ROOT/fixture['sourceFile']
    assert store.file_hash(path) == fixture['sourceHash'], 'Fixture describes a different input image'
    inventory = store.read_json(store.ROOT/'evidence/course-inventory.json')['maps']
    report = {'auditedAt': datetime.now(timezone.utc).isoformat(), 'scope': __doc__, 'cases': [],
              'distinctMapCount': 4, 'savedTeacherControlsUsed': False, 'sourceHash': fixture['sourceHash']}
    for item in inventory:
        image, _ = read_map_image(store.get_asset(item['id'])['path'])
        report['cases'].append(check(image, item['id'], item['labels']))
    image, info = read_map_image(path)
    size = np.array(image.shape[1::-1])
    positions = {label: np.array(point)*size for label, point in fixture['centres'].items()}
    start = np.array(fixture['startFinishCentre'])*size
    tolerance = max(size)*fixture['normalisedTolerance']
    report['imageFrame'] = info
    report['cases'].append(check(image, 'new-original', fixture['labels'], positions, start, tolerance))
    half = cv2.resize(image, None, fx=.5, fy=.5, interpolation=cv2.INTER_AREA)
    scale = np.array(half.shape[1::-1])/size
    report['cases'].append(check(half, 'new-half-resolution', fixture['labels'],
        {label: (point+.5)*scale-.5 for label, point in positions.items()}, (start+.5)*scale-.5, tolerance*min(scale)))
    margin = round(max(size)*.1)
    bordered = cv2.copyMakeBorder(image, margin, margin, margin, margin, cv2.BORDER_CONSTANT, value=(255, 255, 255))
    report['cases'].append(check(bordered, 'new-white-margin', fixture['labels'],
        {label: point+margin for label, point in positions.items()}, start+margin, tolerance))
    encoded = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 55])[1]
    compressed = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    report['cases'].append(check(compressed, 'new-jpeg-quality-55', fixture['labels'], positions, start, tolerance))
    report['allChecksPassed'] = all(case['passed'] for case in report['cases'])
    report['originalFileUnchanged'] = store.file_hash(path) == fixture['sourceHash']
    report['finishedAt'] = datetime.now(timezone.utc).isoformat()
    store.atomic_json(store.ROOT/'evidence/runtime/new-map-recognition.json', report)
    assert report['allChecksPassed'] and report['originalFileUnchanged'], 'See new-map-recognition.json for failures'


if __name__ == '__main__':
    main()
