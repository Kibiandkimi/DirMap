#!/usr/bin/env python3
"""Check road-registration stability on the supplied Dashahe image.

Photo masks and printed control locations are held fixed from the saved
sheet so this isolates geographic registration. Dimming/JPEG/cropping here
do not measure end-to-end photo recognition or real-phone/GPS accuracy.
The unchanged image establishes the comparison; it is not survey truth.
Incomplete observed coverage is recorded separately from coordinate drift.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from dirmap import store, sites, service
    from dirmap.geometry import Frame
    from dirmap.network_registration import road_network_register
    from dirmap.sheet import cartographic_image
    from dirmap.vision import read_image
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--asset', default='upload-55496a067ba7')
    parser.add_argument('--output', type=Path, default=ROOT/'evidence/runtime/network-registration-variants.json')
    args = parser.parse_args()
    state = store.get_state(args.asset); asset = store.get_asset(args.asset)
    assert state['sourceHash'] == '21800411acc7cc83220de66d1c31b7b33b9ac20d1bcff0da699ea9c45dc8d4b8'
    raw = read_image(asset['path'], (state['width'], state['height']))
    reference = sites.reference('dashahe-park', Frame.of(state))
    controls = [c for c in state['controls'] if c.get('role', 'control') == 'control']
    assert sorted(c['label'] for c in controls) == [str(i) for i in range(31, 63)]
    original = np.array([c['pixel'] for c in controls])
    cases = ['original', 'white-border-1400', 'map-crop', 'jpeg-60']+[f'dim-{value}' for value in (.9, .85, .8, .75, .7, .65)]
    report = {'checkedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'scope': __doc__,
              'implementationSignature': service.implementation_signature(), 'sourceSha256': state['sourceHash'],
              'referenceSha256': hashlib.sha256(json.dumps(reference, sort_keys=True).encode()).hexdigest(),
              'maximumAdditionalShiftM': 4., 'cases': []}
    baseline = None
    for name in cases:
        image, points = raw, original.copy()
        if name.startswith('dim-'):
            image = np.rint(raw*float(name[4:])).astype(np.uint8)
        elif name.startswith('jpeg-'):
            ok, encoded = cv2.imencode('.jpg', raw, [cv2.IMWRITE_JPEG_QUALITY, int(name[5:])])
            assert ok
            image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
        cartography = cartographic_image(image, state['sheetPhotos'])
        if name == 'white-border-1400':
            image = cv2.copyMakeBorder(image, 1400, 1400, 1400, 1400, cv2.BORDER_CONSTANT, value=(255, 255, 255))
            cartography = cv2.copyMakeBorder(cartography, 1400, 1400, 1400, 1400, cv2.BORDER_CONSTANT, value=(255, 255, 255))
            points += 1400
        elif name == 'map-crop':
            image = image[200:2789, 780:2850]; cartography = cartography[200:2789, 780:2850]
            points -= [780, 200]
        started = time.perf_counter()
        row = {'case': name, 'imageSize': list(image.shape[1::-1])}
        try:
            reg, quality = road_network_register(cartography, reference, exposure_image=image)
            prediction, valid = reg.forward(points, strict=False)
            if name == 'original':
                baseline = prediction
            assert baseline is not None
            errors = np.linalg.norm(prediction-baseline, axis=1)
            fit = {p['referenceId'] for p in quality['landmarks'] if p['purpose'] == 'fit'}
            validation = {p['referenceId'] for p in quality['landmarks'] if p['purpose'] == 'validation'}
            assert fit and validation and not fit & validation
            row.update(registrationFound=True, calibratedControls=int(valid.sum()),
                uncoveredLabels=[c['label'] for c, inside in zip(controls, valid) if not inside],
                differenceFromOriginalM={'median': round(float(np.median(errors)), 3),
                    'p95': round(float(np.percentile(errors, 95)), 3), 'max': round(float(errors.max()), 3)},
                alignmentStable=bool(errors.max() <= report['maximumAdditionalShiftM']),
                fitObjectCount=quality['fitObjectCount'], fitCount=quality['fitCount'],
                internalRoadP95M=quality['p95ErrorM'], validationWays=quality['validationWays'],
                validationSeparateFromFit=True, exposure=quality.get('pathExtraction', {}),
                extraBoundarySamples=quality.get('boundaryPathSamples', 0), coverageComplete=bool(valid.all()))
        except ValueError as error:
            row.update(registrationFound=False, coverageComplete=False, error=str(error))
        row['elapsedSec'] = round(time.perf_counter()-started, 3)
        report['cases'].append(row)
        print(json.dumps({k: v for k, v in row.items() if k != 'validationWays'}, ensure_ascii=False), flush=True)
    report['allRegistrationsFound'] = all(row['registrationFound'] for row in report['cases'])
    report['allCoverageComplete'] = all(row['coverageComplete'] for row in report['cases'])
    report['allAlignmentsStable'] = all(row.get('alignmentStable', False) for row in report['cases'])
    report['allChecksPassed'] = report['allRegistrationsFound'] and report['allCoverageComplete'] and report['allAlignmentsStable']
    report['independentSurveyValidation'] = False
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    if not report['allChecksPassed']:
        raise SystemExit('Some variants still need observed boundary support; see the report.')


if __name__ == '__main__':
    main()
