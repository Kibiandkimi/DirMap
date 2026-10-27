#!/usr/bin/env python3
"""End-to-end check of a non-campus place on a synthetic 大沙河公园 sprint map.

The repository has no scanned orienteering map of the park. This renders one
from the park's own OSM reference in ISSprOM colours, rotated and scaled by a
known similarity transform, with purple control circles on park paths and a
combined start/finish symbol. The real upload → recognition → automatic registration →
planning pipeline then runs on it in a temporary data directory, and the
known transform gives every control's true position.

A rendering of the reference is the most favourable possible input for
matching that reference; this verifies the place's frame, reference and
pipeline, not recognition accuracy on a real printed park map.
"""
import argparse
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import time

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dirmap import sites, store  # noqa: E402

SITE = 'dashahe-park'
METRES_PER_PIXEL = .5
ROTATION_DEG = 2.5
HALF_EXTENT = (430, 520)          # local metres around the park origin
PURPLE = (200, 50, 205)           # BGR course overprint
PAVED, WATER, BUILDING = (207, 220, 246), (235, 205, 120), (128, 128, 128)
WIDTHS = {'motorway': 16, 'trunk': 14, 'primary': 14, 'secondary': 10, 'tertiary': 9, 'residential': 7, 'service': 5,
          'unclassified': 6, 'pedestrian': 5, 'footway': 3, 'cycleway': 3, 'path': 2.5, 'steps': 2.5}


def transform():
    theta = math.radians(ROTATION_DEG)
    rotation = np.array([[math.cos(theta), -math.sin(theta)], [math.sin(theta), math.cos(theta)]])
    flip = np.diag([1/METRES_PER_PIXEL, -1/METRES_PER_PIXEL])
    linear = rotation @ flip
    corners = np.array([[x, y] for x in (-HALF_EXTENT[0], HALF_EXTENT[0]) for y in (-HALF_EXTENT[1], HALF_EXTENT[1])]) @ linear.T
    offset = -corners.min(axis=0)+60
    size = np.ceil(corners.max(axis=0)+offset+60).astype(int)
    return linear, offset, (int(size[0]), int(size[1]))


def to_pixels(points, linear, offset):
    return np.asarray(points, float) @ linear.T+offset


def render(features, linear, offset, size, controls, start):
    width, height = size
    image = np.full((height, width, 3), 255, np.uint8)
    draw = lambda points: np.round(to_pixels(points, linear, offset)*4).astype(np.int32).reshape(-1, 1, 2)
    scale = 1/METRES_PER_PIXEL
    for feature in features:
        tags = feature['tags']
        if feature['kind'] == 'green' and (tags.get('landuse') in ('grass', 'meadow') or tags.get('leisure') == 'garden'):
            cv2.fillPoly(image, [draw(feature['points'])], (154, 221, 255), cv2.LINE_AA, shift=2)
        elif feature['kind'] == 'green' and tags.get('natural') in ('wood', 'scrub'):
            cv2.fillPoly(image, [draw(feature['points'])], (180, 235, 198), cv2.LINE_AA, shift=2)
    for feature in features:
        if feature['kind'] == 'road':
            width_m = WIDTHS.get(feature['tags'].get('highway'), 4)
            cv2.polylines(image, [draw(feature['points'])], False, PAVED, max(2, round(width_m*scale)), cv2.LINE_AA, shift=2)
    for feature in features:
        if feature['kind'] == 'pitch':
            cv2.fillPoly(image, [draw(feature['points'])], (190, 214, 240), cv2.LINE_AA, shift=2)
            cv2.polylines(image, [draw(feature['points'])], True, (0, 0, 0), 2, cv2.LINE_AA, shift=2)
        elif feature['kind'] == 'water':
            cv2.fillPoly(image, [draw(feature['points'])], WATER, cv2.LINE_AA, shift=2)
            cv2.polylines(image, [draw(feature['points'])], True, (0, 0, 0), 2, cv2.LINE_AA, shift=2)
        elif feature['kind'] == 'waterway':
            cv2.polylines(image, [draw(feature['points'])], False, WATER, 5, cv2.LINE_AA, shift=2)
    for feature in features:
        if feature['kind'] == 'building':
            cv2.fillPoly(image, [draw(feature['points'])], BUILDING, cv2.LINE_AA, shift=2)
            cv2.polylines(image, [draw(feature['points'])], True, (0, 0, 0), 2, cv2.LINE_AA, shift=2)
    # Blue magnetic-north guides, as printed on the campus sheets. They are
    # offset so that none runs under the start-to-31 course line: a guide
    # hidden for hundreds of pixels by overprint is too broken to be told
    # from a stream, which is a separate recognition limit.
    for x in np.arange(-HALF_EXTENT[0]+75, HALF_EXTENT[0]+1, 150):
        cv2.line(image, *[tuple(np.round(p).astype(int)) for p in to_pixels([[x, -HALF_EXTENT[1]], [x, HALF_EXTENT[1]]], linear, offset)],
                 (230, 160, 40), 2, cv2.LINE_AA)
    radius = round(12*scale)
    centres = to_pixels([c['local'] for c in controls], linear, offset)
    # A class loop starts and finishes at one place: the campus sheets'
    # combined symbol in ISSprOM proportions to the 5 mm control circle,
    # finish rings of 4 and 6 mm and a 6 mm start triangle around them.
    start_pixel = to_pixels([start], linear, offset)[0]
    triangle = np.array([[math.cos(a), math.sin(a)] for a in (-math.pi/2, math.pi/6, 5*math.pi/6)])*radius*6/5/math.sqrt(3)*2+start_pixel
    cv2.polylines(image, [np.round(triangle*4).astype(np.int32)], True, PURPLE, 3, cv2.LINE_AA, shift=2)
    for ring in (radius*.8, radius*1.2):
        cv2.circle(image, tuple(np.round(start_pixel).astype(int)), round(ring), PURPLE, 3, cv2.LINE_AA)
    previous = start_pixel
    for index, centre in enumerate(centres):
        direction = centre-previous
        length = np.linalg.norm(direction)
        if length > 2.8*radius:
            unit = direction/length
            a, b = previous+unit*radius*(1.45 if index == 0 else 1.15), centre-unit*radius*1.15
            cv2.line(image, tuple(np.round(a).astype(int)), tuple(np.round(b).astype(int)), PURPLE, 2, cv2.LINE_AA)
        cv2.circle(image, tuple(np.round(centre).astype(int)), radius, PURPLE, 3, cv2.LINE_AA)
        cv2.putText(image, str(31+index), tuple(np.round(centre+[radius*.8, -radius*.8]).astype(int)),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.1, PURPLE, 3, cv2.LINE_AA)
        previous = centre
    direction = start_pixel-previous
    unit = direction/np.linalg.norm(direction)
    a, b = previous+unit*radius*1.15, start_pixel-unit*radius*1.45
    cv2.line(image, tuple(np.round(a).astype(int)), tuple(np.round(b).astype(int)), PURPLE, 2, cv2.LINE_AA)
    return cv2.GaussianBlur(image, (3, 3), .6)


def choose_controls(features, count=8, spacing=80):
    park = next(f for f in features if f['kind'] == 'green' and '大沙河公园' in f['tags'].get('name', ''))
    outline = np.asarray(park['points'], np.float32)
    candidates = []
    for feature in features:
        if feature['kind'] == 'road' and feature['tags'].get('highway') in ('footway', 'path', 'pedestrian'):
            for point in feature['points']:
                if cv2.pointPolygonTest(outline, (float(point[0]), float(point[1])), True) > 25:
                    candidates.append(point)
    candidates.sort(key=lambda p: (round(p[1], 3), round(p[0], 3)))
    chosen = []
    for point in candidates:
        if all(math.dist(point, other) >= spacing for other in chosen):
            chosen.append(point)
    # A loop: walk around the park's centre by angle.
    centre = np.mean(chosen, axis=0)
    chosen.sort(key=lambda p: math.atan2(p[1]-centre[1], p[0]-centre[0]))
    picked = chosen[::max(1, len(chosen)//count)][:count]
    start = min(candidates, key=lambda p: p[1])  # southernmost park path
    return picked, start


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT/'evidence/runtime/dashahe-synthetic.json')
    parser.add_argument('--image', type=Path, default=Path(tempfile.gettempdir())/'dirmap-dashahe-synthetic.jpg')
    args = parser.parse_args()
    site = sites.get(SITE)
    frame = sites.frame(site)
    features = sites.reference(SITE)
    picked, start = choose_controls(features)
    controls = [{'label': str(31+i), 'local': list(p), 'lnglat': frame.to_lnglat(p).tolist()} for i, p in enumerate(picked)]
    linear, offset, size = transform()
    image = render(features, linear, offset, size, controls, start)
    ok, encoded = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 90])
    assert ok
    args.image.write_bytes(encoded.tobytes())

    with tempfile.TemporaryDirectory(prefix='dirmap-dashahe-') as temporary:
        os.environ['DIRMAP_CACHE_DIR'] = str(Path(temporary)/'cache')
        store.DATA = Path(temporary)/'data'
        os.environ.pop('DIRMAP_ADMIN_TOKEN', None)
        from fastapi.testclient import TestClient
        from dirmap.app import app

        def wait(client, job):
            while job['state'] in ('queued', 'running'):
                time.sleep(.5)
                job = client.get('/api/jobs/'+job['id']).json()
            if job['state'] != 'succeeded':
                raise SystemExit(f'{job["kind"]} failed: {job.get("message")}')
            return job['result']

        report = {'checkedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'site': SITE,
                  'scope': '由大沙河公园 OSM 参照按 ISSprOM 配色渲染的合成短距离图，已知旋转与比例；验证场地坐标系、参照与完整处理流程，'
                           '不代表真实印刷公园地图的识别精度。',
                  'synthetic': {'metresPerPixel': METRES_PER_PIXEL, 'rotationDeg': ROTATION_DEG, 'size': list(size),
                                'northGuideSpacingM': 150,
                                'controls': len(controls), 'jpegBytes': len(encoded)}}
        with TestClient(app) as client:
            started = time.perf_counter()
            uploaded = client.post('/api/maps/upload', params={'siteId': SITE},
                                   files={'file': ('大沙河公园合成图.jpg', encoded.tobytes(), 'image/jpeg')}).json()
            state = wait(client, client.post(f'/api/maps/{uploaded["id"]}/process').json())
            report['processingSec'] = round(time.perf_counter()-started, 1)
            report['registration'] = {key: state['quality'].get(key) for key in ('method', 'fitCount', 'p95ErrorM', 'medianErrorM', 'baseAssetId')} if state.get('quality') else None
            report['frameOrigin'] = state['frame']['origin']
            detected = [c for c in state['controls'] if c.get('role', 'control') == 'control']
            truth_pixels = to_pixels([c['local'] for c in controls], linear, offset)
            matched, errors = {}, []
            for control, pixel in zip(controls, truth_pixels):
                nearest = min(detected, key=lambda c: math.dist(c['pixel'], pixel), default=None)
                if nearest is None or math.dist(nearest['pixel'], pixel) > 8:
                    continue
                matched[control['label']] = nearest
                if nearest.get('lnglat'):
                    errors.append(float(np.linalg.norm(frame.to_local([nearest['lnglat']])[0]-control['local'])))
            report['controls'] = {'drawn': len(controls), 'detected': len(detected), 'matched': len(matched),
                                  'calibrated': sum(bool(c.get('calibrated')) for c in matched.values()),
                                  'positionErrorM': {'median': round(float(np.median(errors)), 2), 'max': round(max(errors), 2)} if errors else None,
                                  'labels': sorted(c['label'] for c in matched.values())}
            if not state.get('registration'):
                raise SystemExit('Automatic registration found no evidence: '+json.dumps(state.get('failures'), ensure_ascii=False))
            ids = [c['id'] for c in matched.values() if c.get('calibrated')]
            endpoints = [c for c in state['controls'] if c.get('role') == 'start_finish']
            report['startFinishRecognized'] = len(endpoints)
            start_pixel = (endpoints[0].get('approachPixel') or endpoints[0]['pixel']) if len(endpoints) == 1 else to_pixels([start], linear, offset)[0].tolist()
            plan = wait(client, client.post('/api/plans', json={
                'assetId': uploaded['id'], 'version': state['version'], 'controlIds': ids, 'startPixel': start_pixel, 'finishPixel': start_pixel,
                'mode': 'split_coverage', 'objective': 'min_max_finish_then_sum', 'timeLimitSec': 5,
                'teams': [{'id': f'第 {i} 队', 'people': 3, 'speedMps': 1.6} for i in (1, 2)]}).json())
            report['plan'] = {'status': plan['status'], 'teams': len(plan['routes']), 'targets': len(ids),
                              'unresolved': [u['message'] for u in plan.get('unresolved', [])],
                              'totalDistanceM': round(sum(r['distanceM'] for r in plan['routes']), 1),
                              'latestFinishSec': round(max((r['finishSec'] for r in plan['routes']), default=0), 1)}
            # Every route point must lie inside the park's map, in the park frame.
            points = np.array([p for r in plan['routes'] for p in r['localGeometry']])
            report['plan']['routeWithinMapExtent'] = bool(len(points) and np.all(np.abs(points) <= np.array(HALF_EXTENT)+60))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
