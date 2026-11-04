#!/usr/bin/env python3
"""First-time automatic registration on maps of places DirMap has never seen.

Every classroom so far brought a map of a new place. This check imitates
that: for each published orienteering map it downloads the OpenStreetMap
reference of a viewport around the sheet (as a teacher adding a place would),
then runs recognition and the automatic geographic registration exactly once,
with no tuning per place. Each map comes from a RouteGadget server whose
organisers georeferenced it with a world file; that world file is only used
afterwards to measure the result and never enters matching.

World files are the organisers' own georeferencing (usually three manually
picked points), so they carry a few metres of error themselves. The check
classifies an outcome as correct, wrong (dangerous: a confident fit far from
the world file) or refused. It is not a field accuracy measurement.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time
import urllib.request

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

HOST = 'https://www.happyherts.routegadget.co.uk'
# Sprint, campus, urban and park maps by several mappers and symbol sets.
EVENTS = [433, 446, 343, 310, 274, 270, 385, 418, 434, 450, 387, 430, 406, 436, 365]
CORRECT_MEDIAN_M, CORRECT_P95_M = 10., 20.
TARGET_M_PER_PX = .32  # the classroom photos are about 0.3 m per pixel


def fetch(url, path, referer=None):
    if not path.exists():
        request = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 DirMap registration check',
                                                       **({'Referer': referer} if referer else {})})
        with urllib.request.urlopen(request, timeout=180) as response:
            body = response.read()
        path.write_bytes(body)
    return path


def events(cache):
    path = fetch(HOST+'/rg2/rg2api.php?type=events', cache/'events.json')
    return {e['id']: e for e in json.loads(path.read_text())['data']['events']}


def world(event):
    return np.array([[event['A'], event['B'], event['C']], [event['D'], event['E'], event['F']]], float)


def load_case(event, cache):
    from dirmap import sites
    from dirmap.geometry import Frame
    gif = fetch(f'{HOST}/kartat/{event["mapid"]}.gif', cache/f'{event["mapid"]}.gif', HOST+'/rg2/')
    capture = cv2.VideoCapture(str(gif))
    ok, image = capture.read()
    if not ok:
        raise RuntimeError(f'cannot decode map {event["mapid"]}')
    transform = world(event)
    h, w = image.shape[:2]
    corners = np.array([[0, 0, 1], [w, 0, 1], [0, h, 1], [w, h, 1]], float) @ transform.T
    west, south = corners.min(axis=0); east, north = corners.max(axis=0)
    centre = Frame((west+east)/2, (south+north)/2)
    metres = np.ptp(centre.to_local(corners), axis=0)
    m_per_px = math.sqrt(abs(np.linalg.det(transform[:, :2]*centre.scales[:, None])))
    # A teacher frames the place with some margin; the add-place dialog
    # accepts sides up to 3 km.
    margin = np.clip(metres*.25, 120, None)
    side = np.minimum(metres+2*margin, sites.MAX_SIDE_M)
    half = side/2/centre.scales
    bbox = [centre.lng-half[0], centre.lat-half[1], centre.lng+half[0], centre.lat+half[1]]
    reference_path = cache/f'reference-{event["mapid"]}.json'
    if not reference_path.exists():
        data, host = sites.download(sites.overpass_query(bbox))
        sites.save_reference(reference_path, bbox, sites.compact(data['elements']), host)
    factor = max(1., min(round(m_per_px/TARGET_M_PER_PX, 1), 4096/max(h, w)))
    if factor > 1:
        image = cv2.resize(image, None, fx=factor, fy=factor, interpolation=cv2.INTER_CUBIC)
    return {'image': image, 'frame': centre, 'reference': reference_path, 'factor': factor,
            'world': transform, 'metresPerSourcePixel': m_per_px, 'bbox': bbox}


def truth_error(reg, case, image):
    from dirmap.network_registration import map_footprint
    h, w = image.shape[:2]
    factor = min(1., 1400/max(h, w))
    footprint = map_footprint(cv2.resize(image, None, fx=factor, fy=factor, interpolation=cv2.INTER_AREA))
    xs, ys = np.meshgrid(np.linspace(.05, .95, 24)*w, np.linspace(.05, .95, 24)*h)
    grid = np.column_stack((xs.ravel(), ys.ravel()))
    if footprint is not None:
        outline = (footprint/factor).astype(np.float32)
        grid = grid[[cv2.pointPolygonTest(outline, tuple(p), False) >= 0 for p in grid]]
    source = np.column_stack((grid/case['factor'], np.ones(len(grid))))
    truth = case['frame'].to_local(source @ case['world'].T)
    predicted, inside = reg.forward(grid, strict=False)
    errors = np.linalg.norm(predicted-truth, axis=1)
    summary = lambda e: {'median': round(float(np.median(e)), 2), 'p95': round(float(np.percentile(e, 95)), 2),
                         'max': round(float(e.max()), 2)} if len(e) else None
    return {'all': summary(errors), 'insideHull': summary(errors[inside]), 'insideFraction': round(float(inside.mean()), 3)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache', type=Path, default=Path('/tmp/dirmap-new-areas'))
    parser.add_argument('--output', type=Path, default=ROOT/'evidence/runtime/new-area-registration.json')
    parser.add_argument('--events', type=int, nargs='*', default=None)
    parser.add_argument('--mode', choices=('service', 'global'), default='service',
                        help='service: the processing chain; global: only the global search route')
    args = parser.parse_args()
    args.cache.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault('DIRMAP_CACHE_DIR', str(args.cache/'dirmap-cache'))
    from dirmap import service
    from dirmap.reference import load_reference
    from dirmap.vision import analyze
    catalogue = events(args.cache)
    report = {'mode': args.mode, 'checkedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'scope': __doc__.strip(),
              'implementationSignature': service.implementation_signature(), 'source': HOST, 'cases': []}
    for event_id in dict.fromkeys(args.events or EVENTS):
        event = catalogue[event_id]
        case = load_case(event, args.cache)
        image = case['image']
        reference = load_reference(case['reference'], case['frame'])
        started = time.perf_counter()
        row = {'event': event_id, 'map': event['mapid'], 'name': event['name'], 'date': event['date'],
               'imageSize': list(image.shape[1::-1]), 'upscale': case['factor'],
               'referenceFeatures': len(reference), 'referenceSha256': hashlib.sha256(case['reference'].read_bytes()).hexdigest()}
        result = analyze(image)
        if args.mode == 'service':
            reg, quality, failures = service.geographic_registration(image, result, reference)
        else:
            from dirmap.global_registration import register
            from dirmap.sheet import cartographic_image
            try:
                reg, quality = register(image, cartographic_image(image, result['sheetPhotos']), result, reference)
                failures = []
            except ValueError as exc:
                reg, quality, failures = None, None, [str(exc)]
        row['elapsedSec'] = round(time.perf_counter()-started, 1)
        row['failures'] = failures
        if reg is None:
            row['outcome'] = 'refused'
        else:
            row['method'] = quality.get('method')
            row['reportedP95M'] = quality.get('p95ErrorM')
            row['truthErrorM'] = truth_error(reg, case, image)
            alternatives = (quality.get('poseSupport') or {}).get('alternatives', [])
            for alternative in alternatives:
                from dirmap.geometry import Registration
                alternative['truthMedianM'] = truth_error(Registration(np.array(alternative.pop('affine'))), case, image)['all']['median']
            row['alternatives'] = alternatives
            row['poseSupport'] = {k: v for k, v in (quality.get('poseSupport') or {}).items() if k != 'alternatives'}
            error = row['truthErrorM']['all']
            row['outcome'] = 'correct' if error['median'] <= CORRECT_MEDIAN_M and error['p95'] <= CORRECT_P95_M else 'wrong'
        report['cases'].append(row)
        print(json.dumps({k: row.get(k) for k in ('event', 'name', 'outcome', 'method', 'reportedP95M', 'truthErrorM', 'failures', 'elapsedSec')},
                         ensure_ascii=False), flush=True)
    outcomes = [row['outcome'] for row in report['cases']]
    report['summary'] = {key: outcomes.count(key) for key in ('correct', 'wrong', 'refused')}
    print(json.dumps(report['summary']))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')


if __name__ == '__main__':
    main()
