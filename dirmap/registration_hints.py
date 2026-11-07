"""Read optional printed evidence without supplying a geographic position.

A scale bar constrains metres per image pixel only when at least three
numbers agree with one another and a visible bar. Text boxes can exclude
lettering from the geometric evidence; OCR never supplies matching points.
"""
import csv
import io
import itertools
import os
import re
import shutil
import subprocess

import cv2
import numpy as np


def correct_exposure(image, cartography, result):
    """Recover globally dim print colours using observed neutral paper.

    Only under-range images whose brightest channel agrees with measured
    neutral paper qualify. A grey roof cannot brighten a normally exposed
    page. The source and its recognition/terrain results are not mutated.
    """
    peak = int(image.max())
    if not 128 <= peak < 235:
        return image, cartography, result, None
    from .network_registration import printed_paths
    from .vision import registration_features
    diagnostics = {}
    try:
        printed_paths(cartography, diagnostics=diagnostics, exposure_image=image)
    except ValueError:
        pass
    white = diagnostics.get('neutralWhiteValue')
    if (white is None or not 128 <= white < 235 or abs(peak-white) > 2
            or diagnostics.get('exposureReference') not in ('unmasked_map', 'neutral_page')):
        return image, cartography, result, None
    gain = 255/white
    corrected = cv2.convertScaleAbs(image, alpha=gain)
    corrected_map = cv2.convertScaleAbs(cartography, alpha=gain)
    features = registration_features(corrected_map, result['controls'])
    return corrected, corrected_map, {**result, 'features': features}, {
        'whiteValue': white, 'gain': round(gain, 4), 'reference': diagnostics['exposureReference']}


def printed_words(image, *, timeout=8):
    executable = os.environ.get('DIRMAP_TESSERACT') or shutil.which('tesseract')
    if not executable:
        return []
    ratio = min(1., 2400/max(image.shape[:2]))
    small = cv2.resize(image, None, fx=ratio, fy=ratio, interpolation=cv2.INTER_AREA)
    try:
        encoded = cv2.imencode('.png', small)[1].tobytes()
        process = subprocess.run([executable, 'stdin', 'stdout', '--psm', '11', 'tsv'],
                                 input=encoded, capture_output=True, timeout=timeout,
                                 env={**os.environ, 'OMP_THREAD_LIMIT': '1'})
    except (OSError, subprocess.TimeoutExpired):
        return []
    words = []
    for row in csv.DictReader(io.StringIO(process.stdout.decode('utf-8', errors='replace')), delimiter='\t'):
        text = row.get('text', '').strip()
        try:
            confidence = float(row.get('conf', -1))
            x, y, w, h = (int(row[key]) for key in ('left', 'top', 'width', 'height'))
        except (ValueError, KeyError, TypeError):
            continue
        if text and confidence >= 50 and w > 0 and h > 0:
            words.append({'text': text, 'confidence': confidence,
                          'box': (np.array([x, y, x+w, y+h], float)/ratio).tolist()})
    return words


def bar_windows(image):
    """Small, pale windows around paired horizontal scale-bar edges."""
    ratio = min(1., 1400/max(image.shape[:2]))
    small = cv2.resize(image, None, fx=ratio, fy=ratio, interpolation=cv2.INTER_AREA)
    ink = (small.max(axis=2) < 180).astype(np.uint8)
    edges_only = ink & (1-cv2.erode(ink, np.ones((3, 1), np.uint8)))
    edges = []
    for source in (ink, edges_only):
        lines = cv2.morphologyEx(source, cv2.MORPH_OPEN, np.ones((1, 40), np.uint8))
        _, _, stats, _ = cv2.connectedComponentsWithStats(lines, 8)
        edges.extend(r[:4] for r in stats[1:] if 45 <= r[2] <= small.shape[1]*.5 and r[3] <= 5)
    windows = []
    for a, b in itertools.combinations(sorted(edges, key=lambda e: e[1]), 2):
        x, y, w, h = map(int, a)
        xx, yy, ww, hh = map(int, b)
        gap = yy-y
        if not 3 <= gap <= min(35, w*.25) or abs(x-xx) > 5 or abs(x+w-xx-ww) > 5:
            continue
        margin = max(18, gap*3)
        left, top = max(0, x-margin), max(0, y-margin)
        right, bottom = min(small.shape[1], x+w+margin), min(small.shape[0], yy+hh+margin)
        crop = small[top:bottom, left:right]
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        pale = float(np.mean((hsv[..., 1] < 45) & (hsv[..., 2] > 200)))
        if pale < .5:
            continue
        box = np.array([left, top, right, bottom], float)/ratio
        if any(np.linalg.norm((box[:2]+box[2:])/2-(old[:2]+old[2:])/2) < margin/ratio for _, old in windows):
            continue
        windows.append((pale*w, box))
    return [box.tolist() for _, box in sorted(windows, key=lambda item: -item[0])[:8]]


def ruler_words(image):
    """OCR only likely rulers, so dense forest symbols cannot consume its budget."""
    words = []
    for box in bar_windows(image):
        x0, y0, x1, y1 = np.rint(box).astype(int)
        crop = image[y0:y1, x0:x1]
        factor = max(1., min(2., 180/max(1, crop.shape[0])))
        crop = cv2.resize(crop, None, fx=factor, fy=factor, interpolation=cv2.INTER_CUBIC)
        for word in printed_words(crop, timeout=2):
            words.append({**word, 'box': (np.asarray(word['box'])/factor+[x0, y0, x0, y0]).tolist()})
    return words


def scale_bar(image, words):
    """Measure a horizontal metric bar, or return None on weak/conflicting evidence."""
    numbers, units = [], []
    for word in words:
        text = word['text'].strip('.,:;').lower()
        box = np.asarray(word['box'], float)
        if text in ('m', 'metres', 'meters', '米'):
            units.append(box)
        match = re.fullmatch(r'(\d+(?:\.\d+)?|o)(m|km|米)?', text)
        if not match:
            continue
        value = 0. if match[1] == 'o' else float(match[1])
        value *= 1000 if match[2] == 'km' else 1
        if value <= 10000:
            numbers.append({'value': value, 'unit': bool(match[2]), 'box': box,
                            'x': float((box[0]+box[2])/2), 'y': float((box[1]+box[3])/2),
                            'height': float(box[3]-box[1])})
    if not units and not any(n['unit'] for n in numbers):
        return None
    matches = []
    for first, last in itertools.combinations(sorted(numbers, key=lambda n: n['x']), 2):
        height = max(first['height'], last['height'])
        span = last['x']-first['x']
        if (span < 6*height or last['value'] <= first['value']
                or abs(last['y']-first['y']) > height*.6):
            continue
        slope = (last['value']-first['value'])/span
        group = [n for n in numbers if first['x'] <= n['x'] <= last['x']
                 and abs(n['y']-first['y']) <= height*.6
                 and .5 <= n['height']/height <= 1.5
                 and abs((n['value']-first['value'])/slope-(n['x']-first['x'])) <= max(2., span*.025)]
        if len(group) < 3 or len({n['value'] for n in group}) != len(group):
            continue
        if not any(n['unit'] for n in group) and not any(
                first['x']-height <= (box[0]+box[2])/2 <= last['x']+height
                and abs((box[1]+box[3])/2-first['y']) <= 3*height for box in units):
            continue
        # Numerals alone (course labels, a table, contour annotations) are
        # not a ruler. Look for its continuous edge and alternating fill
        # immediately above or below the labels.
        left, right = max(0, round(first['x'])), min(image.shape[1], round(last['x']))
        top = min(n['box'][1] for n in group)
        bottom = max(n['box'][3] for n in group)
        bar = None
        for y0, y1 in ((round(top-3*height), round(top-1)), (round(bottom+1), round(bottom+3*height))):
            y0, y1 = max(0, y0), min(image.shape[0], y1)
            crop = image[y0:y1, left:right]
            if crop.size == 0:
                continue
            value = crop.max(axis=2)
            dark = value < max(50., float(np.percentile(value, 90))*.7)
            density = dark.mean(axis=1)
            if density.max() < .7:
                continue
            transitions = np.diff(np.r_[0, density >= .2, 0].astype(np.int8))
            for start, end in zip(np.flatnonzero(transitions == 1), np.flatnonzero(transitions == -1)):
                if density[start:end].max() < .7:
                    continue
                for row in dark[start:end][(density[start:end] > .2) & (density[start:end] < .8)]:
                    # One contiguous band must contain both the edge and
                    # the filled intervals. A nearby roof is not part of it.
                    width = max(3, round(span*.035))
                    smooth = cv2.morphologyEx(row.astype(np.uint8)[None, :], cv2.MORPH_OPEN,
                                              np.ones((1, width), np.uint8)).ravel()
                    starts = np.flatnonzero(np.diff(np.r_[0, smooth, 0].astype(int)) == 1)
                    if len(starts) >= 1:
                        bar = [left, max(0, y0+int(start)-1), right, min(image.shape[0], y0+int(end)+1)]
                        break
                if bar is not None:
                    break
            if bar is not None:
                break
        if bar is None:
            continue
        x = np.array([n['x'] for n in group])
        metres = np.array([n['value'] for n in group])
        slope, intercept = np.polyfit(x, metres, 1)
        residual = float(np.max(np.abs((metres-intercept)/slope-x)))
        matches.append({'metresPerPixel': float(slope), 'labels': len(group),
                        'spanPx': float(span), 'residualPx': residual, 'pixelBox': bar})
    if not matches:
        return None
    scales = [m['metresPerPixel'] for m in matches]
    if max(scales) > min(scales)*1.12:
        return None  # multiple printed scales or contradictory OCR
    return max(matches, key=lambda m: (m['labels'], m['spanPx']))


def without_text(image, features, words, *, excluded_boxes=()):
    """Discard high-confidence words and validated non-geographic graphics."""
    mask = np.zeros(image.shape[:2], np.uint8)
    for box in excluded_boxes:
        x0, y0, x1, y1 = np.rint(box).astype(int)
        mask[max(0, y0):min(mask.shape[0], y1+1), max(0, x0):min(mask.shape[1], x1+1)] = 1
    for word in words:
        if word['confidence'] < 65 or sum(c.isalpha() for c in word['text']) < 3:
            continue
        x0, y0, x1, y1 = np.rint(word['box']).astype(int)
        mask[max(0, y0):min(mask.shape[0], y1+1), max(0, x0):min(mask.shape[1], x1+1)] = 1
    if not mask.any():
        return image, features
    retained = []
    for feature in features:
        outline = np.rint(feature['points']).astype(np.int32)
        x, y, w, h = cv2.boundingRect(outline)
        inside = np.zeros((h, w), np.uint8)
        cv2.fillPoly(inside, [outline-[x, y]], 1)
        if np.mean(mask[y:y+h, x:x+w][inside > 0]) < .3:
            retained.append(feature)
    if len(retained) == len(features) and not np.any(image[mask > 0] != 255):
        return image, features
    clean = image.copy()
    clean[mask > 0] = 255
    return clean, retained


def prepare_scale_hints(cartography, result):
    """Keep a measured ruler's scale while excluding its ink from landmarks.

    Alternating grey intervals otherwise masquerade as a row of roofs and
    can distort the first seeded fit, before the global checks even start.
    """
    ruler = ruler_words(cartography)
    hint = scale_bar(cartography, ruler)
    if hint is not None:
        cartography, features = without_text(cartography, result['features'], [], excluded_boxes=[hint['pixelBox']])
        result = {**result, 'features': features}
    return cartography, result, {'ruler': ruler, 'scaleBar': hint}
