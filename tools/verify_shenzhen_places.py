#!/usr/bin/env python3
"""First registration of degraded sprint sheets of Shenzhen places DirMap never saw.

The next classes go to parks like 大沙河公园: a new place each time, drawn in
the same sprint style. For each listed place this downloads the OSM features
around it, renders a sheet in that style and deliberately damages it the way
real sheets differ from OSM: a quarter of the footpaths missing, paths that
OSM lacks, a fifth of the roofs missing, smooth mapping distortion of a few
metres, vegetation and contour clutter, course circles and JPEG compression,
at a random scale and a small rotation. The teacher's place is framed around
the sheet with an uneven margin. Recognition and automatic registration then
run exactly as for an upload, and the known rendering transform measures the
result.

Rendering from OSM is still more favourable than a real print. This checks
that the search and its checks generalise across places and OSM content; it
is not a field accuracy measurement.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Centre (lng, lat) and sheet half-extent (east, north metres).
PLACES = [
    ('莲花山公园', (114.0565, 22.5545), (450, 550)),
    ('荔枝公园', (114.1000, 22.5440), (350, 420)),
    ('中山公园（南山）', (113.9220, 22.5430), (400, 450)),
    ('人才公园', (113.9520, 22.5125), (400, 450)),
    ('香蜜公园', (114.0290, 22.5520), (320, 380)),
    ('洪湖公园', (114.1170, 22.5650), (400, 450)),
    ('南方科技大学', (113.9970, 22.5990), (500, 600)),
    ('深圳湾公园（红树林段）', (113.9950, 22.5230), (550, 400)),
    ('笔架山公园', (114.0815, 22.5625), (450, 550)),
    ('东湖公园', (114.1550, 22.5650), (450, 500)),
]
PAVED, WATER, BUILDING, PURPLE = (201, 222, 244), (235, 205, 120), (128, 128, 128), (200, 50, 205)
WIDTHS = {'motorway': 16, 'trunk': 14, 'primary': 14, 'secondary': 10, 'tertiary': 9, 'residential': 7, 'service': 5,
          'unclassified': 6, 'pedestrian': 5, 'footway': 3, 'cycleway': 3, 'path': 2.5, 'steps': 2.5}
MINOR = ('footway', 'path', 'pedestrian', 'steps', 'cycleway', 'track')
CORRECT_MEDIAN_M, CORRECT_P95_M = 10., 20.


def place_case(name, centre, half, cache):
    from dirmap import sites
    from dirmap.geometry import Frame
    rng = np.random.default_rng(int(hashlib.sha256(name.encode()).hexdigest()[:8], 16))
    frame = Frame(*centre)
    # An uneven margin: the teacher's viewport is never centred on the sheet.
    margin = np.tile(half, 2)*rng.uniform(.25, .6, 4)
    west, south = frame.to_lnglat([-half[0]-margin[0], -half[1]-margin[1]])
    east, north = frame.to_lnglat([half[0]+margin[2], half[1]+margin[3]])
    bbox = [west, south, east, north]
    path = cache/f'reference-{hashlib.sha256(name.encode()).hexdigest()[:12]}.json'
    if not path.exists():
        data, host = sites.download(sites.overpass_query(bbox))
        sites.save_reference(path, bbox, sites.compact(data['elements']), host)
    return rng, frame, path, bbox


def render(features, half, rng):
    """Sheet image and the true pixel -> local metre similarity."""
    metres_per_pixel = rng.uniform(.27, .40)
    theta = math.radians(rng.uniform(-3, 3))
    rotation = np.array([[math.cos(theta), -math.sin(theta)], [math.sin(theta), math.cos(theta)]])
    linear = rotation @ np.diag([1/metres_per_pixel, -1/metres_per_pixel])
    corners = np.array([[x, y] for x in (-half[0], half[0]) for y in (-half[1], half[1])]) @ linear.T
    offset = -corners.min(axis=0)+40
    size = np.ceil(corners.max(axis=0)+offset+40).astype(int)
    width, height = int(size[0]), int(size[1])
    # Smooth mapping distortion of about 3 m, as a mapper's own survey has.
    phases = rng.uniform(0, 2*math.pi, 4)
    shift = lambda p: p+3*np.column_stack((np.sin(p[:, 1]/170+phases[0])+.5*np.sin(p[:, 0]/90+phases[1]),
                                           np.sin(p[:, 0]/150+phases[2])+.5*np.sin(p[:, 1]/110+phases[3])))/1.5
    draw = lambda points: np.round((shift(np.asarray(points, float)) @ linear.T+offset)*4).astype(np.int32).reshape(-1, 1, 2)
    scale = 1/metres_per_pixel
    image = np.full((height, width, 3), 255, np.uint8)
    window = np.array([[-half[0], -half[1]], [half[0], -half[1]], [half[0], half[1]], [-half[0], half[1]]])
    for feature in features:
        tags = feature['tags']
        if feature['kind'] == 'green':
            colour = (180, 235, 198) if tags.get('natural') in ('wood', 'scrub') or tags.get('landuse') == 'forest' else (154, 221, 255)
            cv2.fillPoly(image, [draw(feature['points'])], colour, cv2.LINE_AA, shift=2)
    for _ in range(25):  # vegetation and open land patches the reference lacks
        centre = rng.uniform(-1, 1, 2)*half
        blob = centre+rng.uniform(15, 60)*np.column_stack((np.cos(np.linspace(0, 2*math.pi, 12)), np.sin(np.linspace(0, 2*math.pi, 12))))*rng.uniform(.6, 1.4, (12, 1))
        cv2.fillPoly(image, [draw(blob)], [(80, 190, 60), (110, 215, 255), (180, 235, 198)][rng.integers(3)], cv2.LINE_AA, shift=2)
    for _ in range(18):  # contour-like brown strokes
        start = rng.uniform(-1, 1, 2)*half
        t = np.linspace(0, 1, 40)
        line = start+np.column_stack((t*rng.uniform(-300, 300), t*rng.uniform(-300, 300)+20*np.sin(t*rng.uniform(3, 9))))
        cv2.polylines(image, [draw(line)], False, (60, 110, 200), 2, cv2.LINE_AA, shift=2)
    drawn_paths = []
    for feature in features:
        if feature['kind'] != 'road':
            continue
        highway = feature['tags'].get('highway')
        if highway in MINOR and rng.random() < .25:
            continue
        width_m = WIDTHS.get(highway, 4)
        cv2.polylines(image, [draw(feature['points'])], False, PAVED, max(2, round(width_m*scale)), cv2.LINE_AA, shift=2)
        if highway in MINOR:
            drawn_paths.extend(feature['points'])
    for _ in range(12):  # paths missing from OSM
        start = rng.uniform(-.9, .9, 2)*half
        heading = rng.uniform(0, 2*math.pi)
        steps = np.cumsum(rng.normal(0, .25, 8))+heading
        line = start+np.cumsum(np.column_stack((np.cos(steps), np.sin(steps)))*rng.uniform(10, 25), axis=0)
        cv2.polylines(image, [draw(line)], False, PAVED, max(2, round(3*scale)), cv2.LINE_AA, shift=2)
    for feature in features:
        if feature['kind'] == 'water':
            cv2.fillPoly(image, [draw(feature['points'])], WATER, cv2.LINE_AA, shift=2)
        elif feature['kind'] == 'waterway':
            cv2.polylines(image, [draw(feature['points'])], False, WATER, 5, cv2.LINE_AA, shift=2)
        elif feature['kind'] == 'pitch':
            cv2.fillPoly(image, [draw(feature['points'])], (110, 200, 245), cv2.LINE_AA, shift=2)
    for feature in features:
        if feature['kind'] == 'building' and rng.random() >= .2:
            cv2.fillPoly(image, [draw(feature['points'])], BUILDING, cv2.LINE_AA, shift=2)
            cv2.polylines(image, [draw(feature['points'])], True, (0, 0, 0), 2, cv2.LINE_AA, shift=2)
    # Clip to the sheet window, then print the course.
    mask = np.zeros(image.shape[:2], np.uint8)
    cv2.fillPoly(mask, [np.round(window @ linear.T+offset).astype(np.int32)], 255)
    image[mask == 0] = 255
    radius = round(12*scale)
    inside = [p for p in drawn_paths if abs(p[0]) < half[0]-40 and abs(p[1]) < half[1]-40]
    chosen = []
    for index in rng.permutation(len(inside)):
        if all(math.dist(inside[index], other) > 70 for other in chosen):
            chosen.append(inside[index])
        if len(chosen) >= 15:
            break
    for number, point in enumerate(chosen):
        centre = np.asarray(point) @ linear.T+offset
        cv2.circle(image, tuple(np.round(centre).astype(int)), radius, PURPLE, 3, cv2.LINE_AA)
        cv2.putText(image, str(31+number), tuple(np.round(centre+[radius*.8, -radius*.8]).astype(int)),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.1, PURPLE, 3, cv2.LINE_AA)
    image = cv2.GaussianBlur(image, (3, 3), .6)
    ok, encoded = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 82])
    image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    inverse = np.linalg.inv(linear)
    truth = np.column_stack((inverse, -inverse @ offset))
    return image, truth, {'metresPerPixel': round(metres_per_pixel, 4), 'rotationDeg': round(math.degrees(theta), 2),
                          'controls': len(chosen), 'window': window}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache', type=Path, default=Path('/tmp/dirmap-sz-places'))
    parser.add_argument('--output', type=Path, default=ROOT/'evidence/runtime/shenzhen-new-places.json')
    parser.add_argument('--only', nargs='*')
    args = parser.parse_args()
    args.cache.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault('DIRMAP_CACHE_DIR', str(args.cache/'dirmap-cache'))
    from dirmap import service
    from dirmap.geometry import affine_points
    from dirmap.reference import load_reference
    from dirmap.vision import analyze
    report = {'checkedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'scope': __doc__.strip(),
              'implementationSignature': service.implementation_signature(), 'cases': []}
    for name, centre, half in PLACES:
        if args.only and name not in args.only:
            continue
        try:
            rng, frame, path, bbox = place_case(name, centre, half, args.cache)
        except ValueError as exc:
            report['cases'].append({'place': name, 'outcome': 'reference_unavailable', 'error': str(exc)[:200]})
            print(json.dumps(report['cases'][-1], ensure_ascii=False), flush=True)
            continue
        reference = load_reference(path, frame)
        image, truth, info = render(reference, half, rng)
        cv2.imwrite(str(args.cache/f'{hashlib.sha256(name.encode()).hexdigest()[:12]}.jpg'), image)
        started = time.perf_counter()
        result = analyze(image)
        reg, quality, failures = service.geographic_registration(image, result, reference)
        row = {'place': name, 'imageSize': list(image.shape[1::-1]), **{k: v for k, v in info.items() if k != 'window'},
               'referenceFeatures': len(reference), 'detectedControls': len(result['controls']),
               'elapsedSec': round(time.perf_counter()-started, 1), 'failures': failures}
        if reg is None:
            row['outcome'] = 'refused'
        else:
            h, w = image.shape[:2]
            xs, ys = np.meshgrid(np.linspace(.05, .95, 20)*w, np.linspace(.05, .95, 20)*h)
            grid = np.column_stack((xs.ravel(), ys.ravel()))
            local = affine_points(truth, grid)
            grid = grid[(np.abs(local[:, 0]) <= half[0]) & (np.abs(local[:, 1]) <= half[1])]
            errors = np.linalg.norm(reg.forward(grid, strict=False)[0]-affine_points(truth, grid), axis=1)
            row.update(method=quality.get('method'), reportedP95M=quality.get('p95ErrorM'),
                       poseSupport={k: v for k, v in (quality.get('poseSupport') or {}).items() if k != 'alternatives'},
                       truthErrorM={'median': round(float(np.median(errors)), 2), 'p95': round(float(np.percentile(errors, 95)), 2),
                                    'max': round(float(errors.max()), 2)})
            row['outcome'] = ('correct' if row['truthErrorM']['median'] <= CORRECT_MEDIAN_M
                              and row['truthErrorM']['p95'] <= CORRECT_P95_M else 'wrong')
        report['cases'].append(row)
        print(json.dumps({k: row.get(k) for k in ('place', 'outcome', 'method', 'truthErrorM', 'elapsedSec', 'failures')},
                         ensure_ascii=False), flush=True)
    outcomes = [row['outcome'] for row in report['cases']]
    report['summary'] = {key: outcomes.count(key) for key in ('correct', 'wrong', 'refused', 'reference_unavailable')}
    print(json.dumps(report['summary']))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')


if __name__ == '__main__':
    main()
