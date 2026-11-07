#!/usr/bin/env python3
"""First-time automatic registration on maps of places DirMap has never seen.

Every classroom so far brought a map of a new place. This check imitates
that: for each published orienteering map it downloads the OpenStreetMap
reference of a viewport around the sheet (as a teacher adding a place would),
then runs recognition and the automatic geographic registration exactly once,
with no tuning per place. Each map comes from a RouteGadget server whose
organisers georeferenced it with a world file. The file frames the teacher's
viewport and, by default, normalises resolution toward classroom-photo
scale. Use --native-resolution to retain original pixels instead. Matching
receives neither the world-file transform nor its landmarks; the resulting
pose is measured against that transform afterwards.

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
from urllib.parse import urlparse

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


def load_case(event, cache, *, native_resolution=False):
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
        data, host = sites.download_reference(bbox)
        sites.save_reference(reference_path, bbox, sites.compact(data['elements']), host)
    factor = 1. if native_resolution else max(1., min(round(m_per_px/TARGET_M_PER_PX, 1), 4096/max(h, w)))
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
    full_grid = grid.copy()
    if footprint is not None:
        outline = (footprint/factor).astype(np.float32)
        x, y, bw, bh = cv2.boundingRect(outline)
        banner = ((bw > 5*bh and bw > .75*w and (y < .06*h or y+bh > .94*h))
                  or (bh > 5*bw and bh > .75*h and (x < .06*w or x+bw > .94*w)))
        # This legacy colour-only measurement region is independent of
        # registration's new ink footprint. A page banner is not a map:
        # retain the whole image grid rather than measuring only its title.
        if not banner:
            grid = grid[[cv2.pointPolygonTest(outline, tuple(p), False) >= 0 for p in grid]]
    source = np.column_stack((grid/case['factor'], np.ones(len(grid))))
    truth = case['frame'].to_local(source @ case['world'].T)
    predicted, inside = reg.forward(grid, strict=False)
    errors = np.linalg.norm(predicted-truth, axis=1)
    summary = lambda e: {'median': round(float(np.median(e)), 2), 'p95': round(float(np.percentile(e, 95)), 2),
                         'max': round(float(e.max()), 2)} if len(e) else None
    full_truth = case['frame'].to_local(np.column_stack((full_grid/case['factor'], np.ones(len(full_grid)))) @ case['world'].T)
    full_errors = np.linalg.norm(reg.forward(full_grid, strict=False)[0]-full_truth, axis=1)
    return {'all': summary(errors), 'insideHull': summary(errors[inside]), 'insideFraction': round(float(inside.mean()), 3),
            'wholeImage': summary(full_errors), 'evaluationSamples': len(grid)}


def main():
    global HOST
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default=HOST, help='RouteGadget server; non-default hosts use a separate cache directory')
    parser.add_argument('--cache', type=Path, default=Path('/tmp/dirmap-new-areas'))
    parser.add_argument('--output', type=Path, default=ROOT/'evidence/runtime/new-area-registration.json')
    parser.add_argument('--events', type=int, nargs='*', default=None)
    parser.add_argument('--native-resolution', action='store_true', help='Keep original image pixels; no world-file-based resampling')
    parser.add_argument('--mode', choices=('service', 'global'), default='service',
                        help='service: the processing chain; global: only the global search route')
    args = parser.parse_args()
    if args.host.rstrip('/') != HOST:
        parsed = urlparse(args.host)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc:
            parser.error('--host must be an HTTP(S) server URL')
        HOST = args.host.rstrip('/')
        args.cache = args.cache/parsed.netloc
    args.cache.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault('DIRMAP_CACHE_DIR', str(args.cache/'dirmap-cache'))
    from dirmap import service
    from dirmap.reference import load_reference
    from dirmap.store import atomic_json
    from dirmap.vision import analyze
    catalogue = events(args.cache)
    report = {'mode': args.mode, 'checkedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'scope': __doc__.strip(),
              'implementationSignature': service.implementation_signature(),
              'verifierSha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), 'source': HOST,
              'inputResolution': 'native' if args.native_resolution else 'classroom_scale',
              'complete': False, 'cases': []}
    partial = args.output.with_name(args.output.stem+'.partial.json')
    for event_id in dict.fromkeys(args.events or EVENTS):
        event = catalogue[event_id]
        case = load_case(event, args.cache, native_resolution=args.native_resolution)
        image = case['image']
        reference = load_reference(case['reference'], case['frame'])
        started = time.perf_counter()
        row = {'event': event_id, 'map': event['mapid'], 'name': event['name'], 'date': event['date'],
               'imageSize': list(image.shape[1::-1]), 'upscale': case['factor'],
               'sourceSha256': hashlib.sha256((args.cache/f'{event["mapid"]}.gif').read_bytes()).hexdigest(),
               'worldFile': case['world'].tolist(),
               'referenceFeatures': len(reference), 'referenceSha256': hashlib.sha256(case['reference'].read_bytes()).hexdigest()}
        # Matching does not need the full-resolution terrain masks/costs.
        # Drop them before matching and before analysing the next sheet.
        result = {key: value for key, value in analyze(image).items()
                  if key in ('features', 'controls', 'sheetPhotos', 'width', 'height')}
        places = []
        if args.mode == 'service':
            reg, quality, failures = service.geographic_registration(image, result, reference, places)
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
            # A refusal can still offer the distinct places it fitted; the
            # teacher picks one. Record whether the true place is offered.
            row['offeredPlaces'] = [{'support': round(float(value), 4), 'method': q.get('method'), 'p95ErrorM': q.get('p95ErrorM'),
                                     'truthErrorM': truth_error(place, case, image)['all']} for value, place, q in places]
            row['correctPlaceOffered'] = next((index+1 for index, offered in enumerate(row['offeredPlaces'])
                                               if offered['truthErrorM']['median'] <= CORRECT_MEDIAN_M
                                               and offered['truthErrorM']['p95'] <= CORRECT_P95_M), None)
        else:
            row['registration'] = reg.as_dict()
            row['validationLandmarks'] = quality.get('landmarks', [])
            row['method'] = quality.get('method')
            row['reportedP95M'] = quality.get('p95ErrorM')
            row['globalSearch'] = quality.get('globalSearch')
            row['exposureCorrection'] = quality.get('exposureCorrection')
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
        atomic_json(partial, report)
        print(json.dumps({k: row.get(k) for k in ('event', 'name', 'outcome', 'method', 'reportedP95M', 'truthErrorM', 'correctPlaceOffered',
                                                  'failures', 'elapsedSec')},
                         ensure_ascii=False), flush=True)
    outcomes = [row['outcome'] for row in report['cases']]
    report['summary'] = {key: outcomes.count(key) for key in ('correct', 'wrong', 'refused')}
    refused = [row for row in report['cases'] if row['outcome'] == 'refused']
    report['summary']['refusedOfferingCorrectPlace'] = sum(bool(row.get('correctPlaceOffered')) for row in refused)
    report['summary']['refusedWithoutOffer'] = sum(not row.get('offeredPlaces') for row in refused)
    report['complete'] = True
    print(json.dumps(report['summary']))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    partial.unlink(missing_ok=True)
    return int(report['summary']['wrong'] > 0)


if __name__ == '__main__':
    raise SystemExit(main())
