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

With --layout classroom the sheet is laid out like the 大沙河 class sheet
instead: the park printed in forest colours inside an irregular outline,
footpaths as black dashes, contours, magnetic north lines and a course
title lettered across the park, beside panels of checkpoint photos (cut
from the 大沙河 sheet; they show no linework), Chinese headings, the scale
note and a printed 0/150/300 m scale bar. The sheet then goes through the
same reading and size reduction as an upload.

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
from verify_new_areas import acceptance_log  # noqa: E402  (same tools directory)

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
        data, host = sites.download_reference(bbox)
        sites.save_reference(path, bbox, sites.compact(data['elements']), host)
    return rng, frame, path, bbox


def render(features, half, rng, classroom=False):
    """Sheet image and the true pixel -> local metre similarity."""
    metres_per_pixel = rng.uniform(.22, .34) if classroom else rng.uniform(.27, .40)
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
    if classroom:
        # A park outline, not a rectangle: two corners cut off unevenly.
        outline = []
        cut = set(rng.choice(4, 2, replace=False).tolist())
        for index, corner in enumerate(window):
            if index not in cut:
                outline.append(corner)
                continue
            before, after = window[index-1], window[(index+1) % 4]
            outline += [corner+(before-corner)*rng.uniform(.15, .35), corner+(after-corner)*rng.uniform(.15, .35)]
        window = np.array(outline)
        cv2.fillPoly(image, [draw(window)], (75, 185, 95), cv2.LINE_AA, shift=2)
    for feature in features:
        tags = feature['tags']
        if feature['kind'] == 'green':
            colour = (180, 235, 198) if tags.get('natural') in ('wood', 'scrub') or tags.get('landuse') == 'forest' else (154, 221, 255)
            cv2.fillPoly(image, [draw(feature['points'])], colour, cv2.LINE_AA, shift=2)
    for _ in range(45 if classroom else 25):  # vegetation and open land patches the reference lacks
        centre = rng.uniform(-1, 1, 2)*half
        blob = centre+rng.uniform(15, 90 if classroom else 60)*np.column_stack((np.cos(np.linspace(0, 2*math.pi, 12)), np.sin(np.linspace(0, 2*math.pi, 12))))*rng.uniform(.6, 1.4, (12, 1))
        cv2.fillPoly(image, [draw(blob)], [(80, 190, 60), (110, 215, 255), (180, 235, 198)][rng.integers(3)], cv2.LINE_AA, shift=2)
    for _ in range(45 if classroom else 18):  # contour-like brown strokes
        start = rng.uniform(-1, 1, 2)*half
        t = np.linspace(0, 1, 40)
        line = start+np.column_stack((t*rng.uniform(-300, 300), t*rng.uniform(-300, 300)+20*np.sin(t*rng.uniform(3, 9))))
        cv2.polylines(image, [draw(line)], False, (60, 110, 200), 2, cv2.LINE_AA, shift=2)
    dashed = classroom and rng.random() < .6

    def dashes(points, width):
        # A footpath dash of about 2 m every 3 m of ground.
        points = np.asarray(points, float)
        lengths = np.r_[0, np.cumsum(np.linalg.norm(np.diff(points, axis=0), axis=1))]
        for start in np.arange(0, lengths[-1], 3.):
            stops = np.linspace(start, min(start+2., lengths[-1]), 4)
            piece = np.column_stack((np.interp(stops, lengths, points[:, 0]), np.interp(stops, lengths, points[:, 1])))
            cv2.polylines(image, [draw(piece)], False, (20, 20, 20), width, cv2.LINE_AA, shift=2)
    drawn_paths = []
    for feature in features:
        if feature['kind'] != 'road':
            continue
        highway = feature['tags'].get('highway')
        if highway in MINOR and rng.random() < .25:
            continue
        width_m = WIDTHS.get(highway, 4)
        if dashed and highway in MINOR:
            dashes(feature['points'], max(2, round(.9*scale)))
        else:
            if classroom:  # paved ways have black edges
                cv2.polylines(image, [draw(feature['points'])], False, (20, 20, 20), max(2, round(width_m*scale))+4, cv2.LINE_AA, shift=2)
            cv2.polylines(image, [draw(feature['points'])], False, PAVED, max(2, round(width_m*scale)), cv2.LINE_AA, shift=2)
        if highway in MINOR:
            drawn_paths.extend(feature['points'])
    for _ in range(25 if classroom else 12):  # paths missing from OSM
        start = rng.uniform(-.9, .9, 2)*half
        heading = rng.uniform(0, 2*math.pi)
        steps = np.cumsum(rng.normal(0, .25, 8))+heading
        line = start+np.cumsum(np.column_stack((np.cos(steps), np.sin(steps)))*rng.uniform(10, 25), axis=0)
        if dashed:
            dashes(line, max(2, round(.9*scale)))
        else:
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
            cv2.fillPoly(image, [draw(feature['points'])], (90, 90, 90) if classroom else BUILDING, cv2.LINE_AA, shift=2)
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
    inverse = np.linalg.inv(linear)
    truth = np.column_stack((inverse, -inverse @ offset))
    info = {'metresPerPixel': round(metres_per_pixel, 4), 'rotationDeg': round(math.degrees(theta), 2),
            'controls': len(chosen), 'window': window}
    if classroom:
        return image, truth, {**info, 'pathStyle': 'dashed' if dashed else 'paved'}
    image = cv2.GaussianBlur(image, (3, 3), .6)
    ok, encoded = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 82])
    image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    return image, truth, info


def class_photos(asset_id='upload-55496a067ba7'):
    """The checkpoint photos of the 大沙河 class sheet, as photo clutter."""
    from dirmap import store
    from dirmap.vision import read_image
    state, asset = store.get_state(asset_id), store.get_asset(asset_id)
    if not state or not asset:
        raise SystemExit('--layout classroom needs the 大沙河 sheet for its photos')
    sheet = read_image(asset['path'], (state['width'], state['height']))
    return [sheet[y0:y1, x0:x1].copy() for x0, y0, x1, y1 in (photo['pixelBox'] for photo in state['sheetPhotos'])]


def classroom_sheet(body, truth, info, rng, name, photos):
    """Lay the park out between photo panels with headings and a scale bar."""
    from PIL import Image, ImageDraw, ImageFont
    font = lambda size: ImageFont.truetype('/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc', size)
    h, w = body.shape[:2]
    mpp = info['metresPerPixel']
    left, right, top, bottom = round(.3*w), round(.45*w), round(.16*h), round(.08*h)
    canvas = np.full((h+top+bottom, w+left+right, 3), 255, np.uint8)
    canvas[top:top+h, left:left+w] = body
    unit = max(12, round(h/70))

    def panel(x0, y0, x1, y1, columns, rows, first):
        cv2.rectangle(canvas, (x0, y0), (x1, y1), PURPLE, max(3, unit//5))
        tile_w, tile_h = (x1-x0)//columns, (y1-y0)//rows
        for index in range(columns*rows):
            photo = photos[rng.integers(len(photos))]
            tile = cv2.resize(photo, (tile_w-6, tile_h-6), interpolation=cv2.INTER_AREA)
            x, y = x0+3+(index % columns)*tile_w, y0+3+(index//columns)*tile_h
            canvas[y:y+tile_h-6, x:x+tile_w-6] = tile
            cv2.putText(canvas, str(first+index), (x+tile_w-6-2*unit, y+tile_h-12), cv2.FONT_HERSHEY_SIMPLEX, unit/22, PURPLE, max(2, unit//6), cv2.LINE_AA)

    panel(round(.02*w), top+round(.05*h), left-round(.03*w), top+round(.7*h), 2, 5, 31)
    panel(left+w+round(.03*w), top, w+left+right-round(.02*w), top+h, 3, 7, 41)
    # Magnetic north lines across the park, and a printed scale bar.
    for x in np.linspace(.2, .8, 3)*w+left:
        cv2.arrowedLine(canvas, (round(x), top+h), (round(x), top-unit), (220, 170, 40), max(2, unit//8), cv2.LINE_AA, tipLength=.02)
    bar = 300/mpp
    x0, y0 = left+round(.1*w), top+h+round(.025*h)
    for k in range(2):
        cv2.rectangle(canvas, (round(x0+k*bar/2), y0), (round(x0+(k+1)*bar/2), y0+unit//2), (220, 170, 40) if k == 0 else (240, 200, 90), -1)
    picture = Image.fromarray(cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB))
    pen = ImageDraw.Draw(picture)
    purple, blue = (205, 50, 200), (40, 170, 220)
    for text, x in (('0', x0), ('150', x0+bar/2), ('300m', x0+bar)):
        pen.text((round(x), y0+unit), text, fill=(0, 0, 0), font=font(unit))
    pen.text((round(.15*w), round(.02*h)), '必到点：31-40，要求团队成员一起以照片为背景拍照打卡；', fill=purple, font=font(2*unit))
    pen.text((round(.15*w), round(.07*h)), '自由点：41-61，团队之间分工完成，以照片为背景拍照打卡。', fill=purple, font=font(2*unit))
    pen.text((left+w, round(.01*h)), name, fill=blue, font=font(5*unit))
    pen.text((left+w, round(.1*h)), '比例尺：1：4000   等高距：2米', fill=blue, font=font(2*unit))
    pen.text((round(.02*w), top+round(.78*h)), '紧急联系电话：\n曾老师 13510243488\n李老师 13728684222', fill=(40, 80, 200), font=font(round(1.6*unit)))
    pen.text((left+round(.25*w), top+round(.45*h)), '深圳大学户外运动课堂', fill=purple, font=font(3*unit))
    canvas = cv2.cvtColor(np.asarray(picture), cv2.COLOR_RGB2BGR)
    canvas = cv2.GaussianBlur(canvas, (3, 3), .6)
    # The body moved by (left, top) within the sheet.
    truth = np.column_stack((truth[:, :2], truth[:, 2]-truth[:, :2] @ [left, top]))
    return canvas, truth


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache', type=Path, default=Path('/tmp/dirmap-sz-places'))
    parser.add_argument('--output', type=Path, default=ROOT/'evidence/runtime/shenzhen-new-places.json')
    parser.add_argument('--only', nargs='*')
    parser.add_argument('--layout', choices=('sprint', 'classroom'), default='sprint')
    args = parser.parse_args()
    args.cache.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault('DIRMAP_CACHE_DIR', str(args.cache/'dirmap-cache'))
    from dirmap import service
    from dirmap.geometry import affine_points
    from dirmap.reference import load_reference
    from dirmap.store import atomic_json
    from dirmap.vision import analyze, read_image
    photos = class_photos() if args.layout == 'classroom' else None
    report = {'checkedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'scope': __doc__.strip(), 'layout': args.layout,
              'implementationSignature': service.implementation_signature(),
              'verifierSha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), 'complete': False, 'cases': []}
    partial = args.output.with_name(args.output.stem+'.partial.json')
    for name, centre, half in PLACES:
        if args.only and name not in args.only:
            continue
        try:
            rng, frame, path, bbox = place_case(name, centre, half, args.cache)
        except ValueError as exc:
            report['cases'].append({'place': name, 'outcome': 'reference_unavailable', 'error': str(exc)[:200]})
            atomic_json(partial, report)
            print(json.dumps(report['cases'][-1], ensure_ascii=False), flush=True)
            continue
        reference = load_reference(path, frame)
        image, truth, info = render(reference, half, rng, classroom=args.layout == 'classroom')
        sheet = args.cache/f'{hashlib.sha256(name.encode()).hexdigest()[:12]}-{args.layout}.jpg'
        if args.layout == 'classroom':
            image, truth = classroom_sheet(image, truth, info, rng, name, photos)
            cv2.imwrite(str(sheet), image, [cv2.IMWRITE_JPEG_QUALITY, 85])
            # Read back as an upload is: decoded and reduced to working size.
            full = image.shape[1::-1]
            image = read_image(sheet)
            factor = full[0]/image.shape[1]
            truth = np.column_stack((truth[:, :2]*factor, truth[:, 2]))
            info['sheetSize'] = list(full)
        else:
            cv2.imwrite(str(sheet), image)
        started = time.perf_counter()
        result = {key: value for key, value in analyze(image).items()
                  if key in ('features', 'controls', 'sheetPhotos', 'width', 'height')}
        places = []
        reg, quality, failures = service.geographic_registration(image, result, reference, places)
        row = {'place': name, 'imageSize': list(image.shape[1::-1]), **{k: v for k, v in info.items() if k != 'window'},
               'referenceSha256': hashlib.sha256(path.read_bytes()).hexdigest(),
               'referenceFeatures': len(reference), 'detectedControls': len(result['controls']),
               'elapsedSec': round(time.perf_counter()-started, 1), 'failures': failures}
        h, w = image.shape[:2]
        xs, ys = np.meshgrid(np.linspace(.05, .95, 20)*w, np.linspace(.05, .95, 20)*h)
        grid = np.column_stack((xs.ravel(), ys.ravel()))
        local = affine_points(truth, grid)
        outline = info['window'].astype(np.float32).reshape(-1, 1, 2)
        grid = grid[[cv2.pointPolygonTest(outline, (float(x), float(y)), False) >= 0 for x, y in local]]

        def truth_error(fit):
            errors = np.linalg.norm(fit.forward(grid, strict=False)[0]-affine_points(truth, grid), axis=1)
            return {'median': round(float(np.median(errors)), 2), 'p95': round(float(np.percentile(errors, 95)), 2),
                    'max': round(float(errors.max()), 2)}

        if reg is None:
            row['outcome'] = 'refused'
            row['offeredPlaces'] = [{'support': round(float(value), 4), 'method': q.get('method'), 'p95ErrorM': q.get('p95ErrorM'),
                                     'acceptance': acceptance_log(q), 'truthErrorM': truth_error(place)}
                                    for value, place, q in places]
            row['correctPlaceOffered'] = next((index+1 for index, offered in enumerate(row['offeredPlaces'])
                                               if offered['truthErrorM']['median'] <= CORRECT_MEDIAN_M
                                               and offered['truthErrorM']['p95'] <= CORRECT_P95_M), None)
        else:
            from dirmap.geometry import Registration
            alternatives = (quality.get('poseSupport') or {}).get('alternatives', [])
            for alternative in alternatives:
                alternative['truthMedianM'] = truth_error(Registration(np.array(alternative['affine'])))['median']
            row.update(method=quality.get('method'), reportedP95M=quality.get('p95ErrorM'),
                       exposureCorrection=quality.get('exposureCorrection'),
                       poseSupport={k: v for k, v in (quality.get('poseSupport') or {}).items() if k != 'alternatives'},
                       acceptance=acceptance_log(quality), alternatives=alternatives,
                       registration=reg.as_dict(), validationLandmarks=quality.get('landmarks', []),
                       truthTransform=np.asarray(truth).tolist(), truthErrorM=truth_error(reg))
            row['outcome'] = ('correct' if row['truthErrorM']['median'] <= CORRECT_MEDIAN_M
                              and row['truthErrorM']['p95'] <= CORRECT_P95_M else 'wrong')
        report['cases'].append(row)
        atomic_json(partial, report)
        print(json.dumps({k: row.get(k) for k in ('place', 'outcome', 'method', 'truthErrorM', 'elapsedSec', 'failures')},
                         ensure_ascii=False), flush=True)
    outcomes = [row['outcome'] for row in report['cases']]
    report['summary'] = {key: outcomes.count(key) for key in ('correct', 'wrong', 'refused', 'reference_unavailable')}
    report['complete'] = True
    print(json.dumps(report['summary']))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    partial.unlink(missing_ok=True)
    return int(report['summary']['correct'] != len(outcomes))


if __name__ == '__main__':
    raise SystemExit(main())
