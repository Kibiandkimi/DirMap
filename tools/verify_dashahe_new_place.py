#!/usr/bin/env python3
"""The real 大沙河 sheet registered as if its place were new.

大沙河 was the first park sheet; its first automatic registration only
worked once a park outline seed was added. Later sheets of new places will
not match a park outline as conveniently. This reruns recognition and the
whole automatic registration chain on the supplied sheet under conditions a
new place brings: no park outline in OSM, only part of the park printed
(crops), a teacher's own viewport downloaded fresh from OSM instead of the
built-in reference, JPEG/exposure damage, and a sideways (90°) image.

The reviewed workbench registration of the unchanged sheet is the
comparison baseline. It is itself an automatic road fit (internal road P95
about 10 m), not survey truth: differences here measure consistency.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from verify_new_areas import acceptance_log  # noqa: E402  (same tools directory)
SOURCE_SHA256 = '21800411acc7cc83220de66d1c31b7b33b9ac20d1bcff0da699ea9c45dc8d4b8'
# A teacher's viewport: off-centre and larger than the park, about 2.6 km.
VIEWPORT = [113.9440, 22.5470, 113.9690, 22.5700]
CASES = [
    ('original', None, 'builtin'),
    ('no-park-outline', None, 'no-park'),
    ('fresh-viewport', None, 'viewport'),
    ('left-half', (0, 0, .55, 1), 'builtin'),
    ('right-half', (.45, 0, 1, 1), 'builtin'),
    ('top-half', (0, 0, 1, .55), 'builtin'),
    ('bottom-half', (0, .45, 1, 1), 'builtin'),
    ('centre', (.2, .2, .8, .8), 'builtin'),
    ('jpeg-60-no-park', None, 'no-park'),
    ('dim-80-no-park', None, 'no-park'),
    ('turned-90-no-park', None, 'no-park'),
]
STABLE_M = 12.


def main():
    from dirmap import service, sites, store
    from dirmap.geometry import Frame, Registration
    from dirmap.reference import load_reference
    from dirmap.vision import analyze, read_image
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--asset', default='upload-55496a067ba7')
    parser.add_argument('--cache', type=Path, default=Path('/tmp/dirmap-dashahe-new-place'))
    parser.add_argument('--output', type=Path, default=ROOT/'evidence/runtime/dashahe-as-new-place.json')
    parser.add_argument('--only', nargs='*')
    args = parser.parse_args()
    args.cache.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault('DIRMAP_CACHE_DIR', str(args.cache/'dirmap-cache'))
    state = store.get_state(args.asset); asset = store.get_asset(args.asset)
    assert state['sourceHash'] == SOURCE_SHA256
    frame = Frame.of(state)
    raw = read_image(asset['path'], (state['width'], state['height']))
    baseline = Registration.from_dict(state['registration'])
    controls = np.array([c['pixel'] for c in state['controls'] if c.get('role', 'control') == 'control'])
    builtin = sites.reference('dashahe-park', frame)
    viewport_path = args.cache/'viewport-reference.json'
    if not viewport_path.exists():
        data, host = sites.download_reference(VIEWPORT)
        sites.save_reference(viewport_path, VIEWPORT, sites.compact(data['elements']), host)
    references = {'builtin': builtin,
                  'no-park': [f for f in builtin if f.get('tags', {}).get('leisure') not in ('park', 'recreation_ground')],
                  'viewport': [f for f in load_reference(viewport_path, frame)
                               if f.get('tags', {}).get('leisure') not in ('park', 'recreation_ground')]}
    report = {'checkedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'scope': __doc__.strip(),
              'implementationSignature': service.implementation_signature(), 'sourceSha256': SOURCE_SHA256,
              'verifierSha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'baselineRegistrationStamp': service.registration_stamp(state),
              'baselineMethod': state['quality'].get('method'), 'stableWithinM': STABLE_M,
              'viewportReferenceSha256': hashlib.sha256(viewport_path.read_bytes()).hexdigest(), 'complete': False, 'cases': []}
    partial = args.output.with_name(args.output.stem+'.partial.json')
    for name, crop, reference_kind in CASES:
        if args.only and name not in args.only:
            continue
        image, offset = raw, np.zeros(2)
        if crop:
            h, w = raw.shape[:2]
            left, top, right, bottom = (round(crop[0]*w), round(crop[1]*h), round(crop[2]*w), round(crop[3]*h))
            image, offset = raw[top:bottom, left:right].copy(), np.array([left, top], float)
        if name.startswith('jpeg-'):
            image = cv2.imdecode(cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 60])[1], cv2.IMREAD_COLOR)
        elif name.startswith('dim-'):
            image = np.rint(image*.8).astype(np.uint8)
        inside = np.all((controls >= offset) & (controls < offset+image.shape[1::-1]), axis=1)
        points = controls[inside]-offset
        original = controls[inside]
        if name.startswith('turned-90'):
            # Counter-clockwise quarter turn: pixel (x, y) moves to (y, w-1-x).
            points = np.column_stack((points[:, 1], image.shape[1]-1-points[:, 0]))
            image = np.ascontiguousarray(np.rot90(image))
        started = time.perf_counter()
        result = {key: value for key, value in analyze(image).items()
                  if key in ('features', 'controls', 'sheetPhotos', 'width', 'height')}
        places = []
        reg, quality, failures = service.geographic_registration(image, result, references[reference_kind], places)
        row = {'case': name, 'reference': reference_kind, 'imageSize': list(image.shape[1::-1]),
               'printedControls': int(inside.sum()), 'elapsedSec': round(time.perf_counter()-started, 1), 'failures': failures}

        def against_baseline(fit):
            predicted, calibrated = fit.forward(points, strict=False)
            return np.linalg.norm(predicted-baseline.forward(original, strict=False)[0], axis=1), calibrated

        if reg is None:
            row['outcome'] = 'refused'
            # A refusal can still offer the distinct places it fitted for the
            # teacher to pick. Record whether the baseline place is offered.
            row['offeredPlaces'] = []
            for value, place, place_quality in places:
                difference, _ = against_baseline(place)
                row['offeredPlaces'].append({'support': round(float(value), 4), 'method': place_quality.get('method'),
                                             'p95ErrorM': place_quality.get('p95ErrorM'), 'acceptance': acceptance_log(place_quality),
                                             'differenceFromBaselineM': {'median': round(float(np.median(difference)), 2),
                                                                         'max': round(float(difference.max()), 2)}})
            row['baselinePlaceOffered'] = next((index+1 for index, offered in enumerate(row['offeredPlaces'])
                                                if offered['differenceFromBaselineM']['max'] <= STABLE_M), None)
        else:
            differences, calibrated = against_baseline(reg)
            row.update(method=quality.get('method'), reportedP95M=quality.get('p95ErrorM'),
                       globalSearch=quality.get('globalSearch'),
                       exposureCorrection=quality.get('exposureCorrection'),
                       poseSupport={k: v for k, v in (quality.get('poseSupport') or {}).items() if k != 'alternatives'},
                       acceptance=acceptance_log(quality),
                       alternatives=[{**a, 'differenceFromBaselineMaxM': round(float(against_baseline(Registration(np.array(a['affine'])))[0].max()), 2)}
                                     for a in (quality.get('poseSupport') or {}).get('alternatives', [])],
                       calibratedControls=int(calibrated.sum()),
                       differenceFromBaselineM={'median': round(float(np.median(differences)), 2),
                                                'max': round(float(differences.max()), 2)})
            row['outcome'] = 'consistent' if differences.max() <= STABLE_M else 'inconsistent'
        report['cases'].append(row)
        store.atomic_json(partial, report)
        print(json.dumps({k: row.get(k) for k in ('case', 'outcome', 'method', 'differenceFromBaselineM', 'calibratedControls',
                                                  'printedControls', 'baselinePlaceOffered', 'elapsedSec', 'failures')},
                         ensure_ascii=False), flush=True)
    outcomes = [row['outcome'] for row in report['cases']]
    report['summary'] = {key: outcomes.count(key) for key in ('consistent', 'inconsistent', 'refused')}
    refused = [row for row in report['cases'] if row['outcome'] == 'refused']
    report['summary']['refusedOfferingBaselinePlace'] = sum(bool(row.get('baselinePlaceOffered')) for row in refused)
    report['summary']['refusedWithoutOffer'] = sum(not row.get('offeredPlaces') for row in refused)
    report['complete'] = True
    print(json.dumps(report['summary']))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    partial.unlink(missing_ok=True)
    return int(report['summary']['inconsistent'] > 0)


if __name__ == '__main__':
    raise SystemExit(main())
