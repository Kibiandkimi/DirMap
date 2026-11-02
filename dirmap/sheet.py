"""Separate photographic control cards from the cartographic part of a sheet.

Pictures have continuous colour texture inside rectangular panels; map fill,
letters and contour strokes do not. Gutters can be white or course-coloured.
All boxes are returned in the working-image pixel frame, without changing it.
"""
import cv2
import numpy as np
import os
from pathlib import Path
import shutil
import tempfile
import zipfile

from .ocr import number_words


def _course_hue(image, controls, scale=1.):
    colours = []
    angles = np.arange(128)*2*np.pi/128
    directions = np.column_stack((np.cos(angles), np.sin(angles)))
    for control in controls:
        pixels = (np.asarray(control['pixel'])+directions*control['radiusPx'])*scale
        x, y = np.rint(pixels).astype(int).T
        keep = (x >= 0) & (x < image.shape[1]) & (y >= 0) & (y < image.shape[0])
        colours.extend(image[y[keep], x[keep]].tolist())
    if not colours:
        return None
    values = cv2.cvtColor(np.asarray(colours, np.uint8).reshape(-1, 1, 3), cv2.COLOR_BGR2HSV).reshape(-1, 3)
    keep = (values[:, 0] >= 120) & (values[:, 0] <= 175) & (values[:, 1] >= 100) & (values[:, 2] >= 100)
    return float(np.median(values[keep, 0])) if keep.sum() >= 32 else None


def _texture(image):
    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)
    scores = []
    step = 24
    for y in range(0, lab.shape[0]-step+1, step):
        for x in range(0, lab.shape[1]-step+1, step):
            q = (lab[y:y+step, x:x+step]//16).astype(np.int32)
            _, counts = np.unique(q[:, :, 0]*256+q[:, :, 1]*16+q[:, :, 2], return_counts=True)
            p = counts/(step*step)
            scores.append(float(-np.sum(p*np.log2(p))))
    return float(np.median(scores)) if scores else 0.


def _adjacent_panels(candidates):
    photos = []
    for candidate in candidates:
        x0, y0, x1, y1 = candidate['box']
        peers = [p for p in candidates if .72 < (p['box'][2]-p['box'][0])/(x1-x0) < 1.4
                 and .65 < (p['box'][3]-p['box'][1])/(y1-y0) < 1.55]
        if sum(p['texture'] >= 2.8 for p in peers) >= 3:
            photos.append(candidate)
    groups, remaining = [], set(range(len(photos)))
    while remaining:
        group, pending = [], [remaining.pop()]
        while pending:
            index = pending.pop(); group.append(index)
            a = photos[index]['box']; aw, ah = a[2]-a[0], a[3]-a[1]
            for other in list(remaining):
                b = photos[other]['box']; bw, bh = b[2]-b[0], b[3]-b[1]
                overlap_x = min(a[2], b[2])-max(a[0], b[0])
                overlap_y = min(a[3], b[3])-max(a[1], b[1])
                across = overlap_y >= .65*min(ah, bh) and -max(12, min(aw, bw)*.16) <= overlap_x <= 0
                down = overlap_x >= .65*min(aw, bw) and -max(12, min(ah, bh)*.16) <= overlap_y <= 0
                if (across or down) and .7 < aw/bw < 1.4 and .65 < ah/bh < 1.55:
                    remaining.remove(other); pending.append(other)
        if len(group) >= 3 and sum(photos[i]['texture'] >= 2.8 for i in group) >= 2:
            groups.extend(group)
    return [photos[i] for i in groups]


def _split_joined_panels(candidates, separator, purple, image):
    """Recover faint gutters only with repeated card sizes and separate labels.

    JPEG blending can join two cards into one wide component. Do not lower
    the general gutter threshold: a panoramic photograph or a white sign
    within it should stay whole. Each proposed child needs its own small
    course-coloured number in the lower corner as well as photo texture.
    """
    def has_number(box):
        x0, y0, x1, y1 = box; w, h = x1-x0, y1-y0
        crop = purple[y0+round(h*.55):y1, x0+round(w*.35):x1].astype(np.uint8)
        _, _, stats, _ = cv2.connectedComponentsWithStats(crop, 8)
        glyphs = [s for s in stats[1:] if h*.07 < s[3] < h*.35 and .1 < s[2]/s[3] < 1.2 and s[4] > s[3]*s[3]*.07]
        return any(-a[3]*.15 <= b[0]-a[0]-a[2] <= a[3]*.75 and
                   abs(b[1]+b[3]/2-a[1]-a[3]/2) < a[3]*.3 and .8 < b[3]/a[3] < 1.25
                   for a in glyphs for b in glyphs if a is not b)

    result = []
    for candidate in candidates:
        x0, y0, x1, y1 = candidate['box']; w, h = x1-x0, y1-y0
        widths = [p['box'][2]-p['box'][0] for p in candidates if p['texture'] >= 2.8
                  and .8 < (p['box'][3]-p['box'][1])/h < 1.25
                  and .27 < (p['box'][2]-p['box'][0])/w < .65]
        if len(widths) < 3:
            result.append(candidate); continue
        typical = max(widths, key=lambda width: sum(.85 < other/width < 1.18 for other in widths))
        peers = [width for width in widths if .85 < width/typical < 1.18]
        count = round(w/float(np.median(peers)))
        if len(peers) < 3 or count not in (2, 3) or abs(w/count-np.median(peers)) > np.median(peers)*.16:
            result.append(candidate); continue
        values = separator[y0:y1, x0:x1].mean(axis=0)
        cuts = [0]
        for i in range(1, count):
            expected = w*i/count
            positions = np.arange(max(cuts[-1]+1, round(expected-w/count*.1)), min(w, round(expected+w/count*.1)+1))
            chosen = int(positions[np.argmax(values[positions]-np.abs(positions-expected)*.002)])
            if values[chosen] < .5:
                break
            cuts.append(chosen)
        if len(cuts) != count:
            result.append(candidate); continue
        cuts.append(w)
        children = []
        for left, right in zip(cuts[:-1], cuts[1:]):
            box = [x0+left, y0, x0+right, y1]
            texture = _texture(image[y0:y1, box[0]:box[2]])
            if texture < 1.3 or not has_number(box):
                break
            children.append({'box': box, 'texture': texture})
        result.extend(children if len(children) == count else [candidate])
    return result


def photo_regions(image):
    height, width = image.shape[:2]
    scale = min(1., 1920/max(width, height))
    small = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else image
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    b, g, r = (a.astype(np.int16) for a in cv2.split(small))
    white = (hsv[:, :, 1] < 35) & (hsv[:, :, 2] > 208)
    purple = (r-g > 22) & (b-g > 18) & (r > 85) & (b > 75)
    separator = white | purple
    contours, _ = cv2.findContours((~separator).astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    minimum = max(40, min(small.shape[:2])*.046)

    def split(box):
        x, y, w, h = box
        area = separator[y:y+h, x:x+w]
        candidates = []
        for axis, values in ((0, area.mean(axis=1)), (1, area.mean(axis=0))):
            margin = int(minimum)
            if len(values) <= 2*margin:
                continue
            index = int(np.argmax(values[margin:-margin]))+margin
            if values[index] >= .74:
                candidates.append((float(values[index]), axis, index))
        if not candidates:
            return [box]
        _, axis, index = max(candidates)
        children = ((x, y, w, index), (x, y+index+1, w, h-index-1)) if axis == 0 else (
            (x, y, index, h), (x+index+1, y, w-index-1, h))
        return [part for child in children for part in split(child)]

    candidates = []
    for contour in contours:
        box = tuple(int(v) for v in cv2.boundingRect(contour))
        x, y, w, h = box
        if min(w, h) < minimum or cv2.contourArea(contour) < w*h*.58:
            continue
        for bx, by, bw, bh in split(box):
            if not .55 < bw/bh < 2.6 or bw*bh > small.shape[0]*small.shape[1]*.09:
                continue
            texture = _texture(small[by:by+bh, bx:bx+bw])
            if texture < 1.3:
                continue
            candidates.append({'box': [bx, by, bx+bw, by+bh], 'texture': texture})
    # Fragmented white signs can produce overlapping parent/child rectangles.
    distinct = []
    for candidate in sorted(candidates, key=lambda p: -(p['box'][2]-p['box'][0])*(p['box'][3]-p['box'][1])):
        x0, y0, x1, y1 = candidate['box']
        if any(max(0, min(x1, p['box'][2])-max(x0, p['box'][0])) *
               max(0, min(y1, p['box'][3])-max(y0, p['box'][1])) >
               .65*(x1-x0)*(y1-y0) for p in distinct):
            continue
        distinct.append(candidate)
    candidates = distinct
    # A solitary textured building or hill is not enough to identify an album.
    # Repeated panel sizes provide independent layout evidence.
    # A map also contains textured rectangles (courts, hatching, roofs).
    # Cards must form an adjacent album, not just have similar sizes at
    # unrelated places on the page.
    photos = _adjacent_panels(candidates)
    if not photos:
        photos = _adjacent_panels(_split_joined_panels(candidates, separator, purple, small))
    # Low-texture signs and adjoining cards may have no closed nonwhite
    # component. Recover cells only in an observed repeated panel layout,
    # with straight gutters on all sides and photographic content inside.
    for seed in list(photos):
        x0, y0, x1, y1 = seed['box']
        w, h = x1-x0, y1-y0
        peers = [p for p in photos if .8 < (p['box'][2]-p['box'][0])/w < 1.25
                 and .8 < (p['box'][3]-p['box'][1])/h < 1.25
                 and abs(p['box'][0]-x0) < w*4.2 and abs(p['box'][1]-y0) < h*8]
        if len(peers) < 4:
            continue

        def intervals(axis, tolerance):
            groups = []
            for p in sorted(peers, key=lambda p: p['box'][axis]):
                start, end = p['box'][axis], p['box'][axis+2]
                group = next((g for g in groups if abs(np.median([q[0] for q in g])-start) < tolerance), None)
                if group is None:
                    groups.append([(start, end)])
                else:
                    group.append((start, end))
            return [(int(round(np.median([q[0] for q in g]))), max(q[1] for q in g)) for g in groups]

        for left, right in intervals(0, w*.18):
            for top, bottom in intervals(1, h*.18):
                if any(abs((p['box'][0]+p['box'][2]-left-right)/2) < w*.4 and
                       abs((p['box'][1]+p['box'][3]-top-bottom)/2) < h*.4 for p in photos):
                    continue
                if not (0 <= left < right <= small.shape[1] and 0 <= top < bottom <= small.shape[0]):
                    continue
                pad = max(4, round(min(w, h)*.06))
                edges = [separator[max(0, top-pad):min(small.shape[0], top+pad+1), left:right].mean(axis=1).max(),
                         separator[max(0, bottom-pad):min(small.shape[0], bottom+pad+1), left:right].mean(axis=1).max(),
                         separator[top:bottom, max(0, left-pad):min(small.shape[1], left+pad+1)].mean(axis=0).max(),
                         separator[top:bottom, max(0, right-pad):min(small.shape[1], right+pad+1)].mean(axis=0).max()]
                gutters = (min(edges) >= .58 and np.mean(edges) >= .8) or (
                    min(sorted(edges)[1:]) >= .9 and np.mean(sorted(edges)[1:]) >= .95)
                texture = _texture(small[top:bottom, left:right])
                gutters |= min(edges) >= .35 and np.mean(edges) >= .75 and texture >= 2.8
                if not gutters or separator[top:bottom, left:right].mean() > .65:
                    continue
                if texture >= 1.1 or (min(edges) >= .95 and separator[top:bottom, left:right].mean() < .25):
                    photos.append({'box': [left, top, right, bottom], 'texture': texture, 'layoutRecovered': True})
    photos = [{'pixelBox': [int(round(v/scale)) for v in p['box']],
               'confidence': round(min(.98, .76+max(0, p['texture']-2.8)*.12), 3),
               'layoutRecovered': p.get('layoutRecovered', False)} for p in photos]
    photos.sort(key=lambda p: (p['pixelBox'][1], p['pixelBox'][0]))
    for index, photo in enumerate(photos):
        photo.update(id=f'photo-{index+1}', label=None, labelSource='unread')
    return photos


def cartographic_image(image, photos):
    """Mask photo pixels for recognition, retaining the original coordinate frame."""
    if not photos:
        return image
    result = image.copy()
    mask = photograph_mask(image.shape, photos)
    result[cv2.dilate(mask, np.ones((5, 5), np.uint8)) > 0] = 255
    return result


def fill_panel_gaps(image, photos, controls=()):
    """Fill interior album gaps after card labels have supplied more panels.

    Two observed row/column steps establish a pitch. Only gaps bracketed by
    actual photographs are considered; blank paper and cartography still
    need independent photographic texture and at least three frame edges.
    """
    if len(photos) < 4:
        return photos
    factor = min(1., 1920/max(image.shape[:2]))
    small = cv2.resize(image, None, fx=factor, fy=factor, interpolation=cv2.INTER_AREA) if factor < 1 else image
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    b, g, r = (v.astype(np.int16) for v in cv2.split(small))
    purple = (r-g > 22) & (b-g > 18) & (r > 85) & (b > 75)
    separator = purple | ((hsv[:, :, 1] < 35) & (hsv[:, :, 2] > 208))
    boxes = [np.asarray(p['pixelBox'])*factor for p in photos]
    for _ in range(2):
        proposals, seen = [], set()
        for axis in (0, 1):
            across = 1-axis
            for box in boxes:
                size = box[2:]-box[:2]
                peers = [i for i, p in enumerate(boxes) if np.all((p[2:]-p[:2])/size > .8)
                         and np.all((p[2:]-p[:2])/size < 1.25)
                         and abs((p[across]+p[across+2]-box[across]-box[across+2])/2) < size[across]*.15]
                key = axis, tuple(peers)
                if len(peers) < 4 or key in seen:
                    continue
                seen.add(key)
                family = np.array([boxes[i] for i in peers])
                size = np.median(family[:, 2:]-family[:, :2], axis=0)
                positions = np.sort(family[:, axis])
                steps = np.diff(positions)
                steps = steps[(steps > size[axis]*.8) & (steps < size[axis]*1.5)]
                if len(steps) < 2 or np.ptp(steps) > size[axis]*.15:
                    continue
                pitch = float(np.median(steps))
                for position in positions:
                    for sign in (-1, 1):
                        following = position+sign*pitch
                        if following <= positions[0]+size[axis]*.4 or following >= positions[-1]-size[axis]*.4:
                            continue
                        start = np.median(family[:, :2], axis=0); start[axis] = following
                        proposals.append(np.rint(np.r_[start, start+size]).astype(int))
        added = 0
        for initial in proposals:
            center = (initial[:2]+initial[2:])/2
            if any(np.all(center >= p[:2]) and np.all(center <= p[2:]) for p in boxes):
                continue
            if np.any(initial[:2] < 0) or np.any(initial[2:] > small.shape[1::-1]):
                continue
            left, top, right, bottom = initial; w, h = right-left, bottom-top
            fitted, edges = [], []
            for axis, value, span in ((0, left, w), (1, top, h), (0, right, w), (1, bottom, h)):
                positions = np.arange(max(0, round(value-span*.1)), min(small.shape[1-axis], round(value+span*.1)+1))
                values = separator[top:bottom, positions].mean(axis=0) if axis == 0 else separator[positions, left:right].mean(axis=1)
                best = int(np.argmax(values-np.abs(positions-value)*.002))
                fitted.append(int(positions[best])); edges.append(float(values[best]))
            x0, y0, x1, y1 = fitted
            if not (.8*w < x1-x0 < 1.2*w and .8*h < y1-y0 < 1.2*h) or sum(v >= .65 for v in edges) < 3:
                continue
            if any(x0-c['radiusPx']*factor*1.2 <= c['pixel'][0]*factor <= x1+c['radiusPx']*factor*1.2
                   and y0-c['radiusPx']*factor*1.2 <= c['pixel'][1]*factor <= y1+c['radiusPx']*factor*1.2 for c in controls):
                continue
            crop = small[y0:y1, x0:x1].copy(); crop[purple[y0:y1, x0:x1]] = 255
            if _texture(crop) < 1.3:
                continue
            identifier = 1
            while any(p['id'] == f'photo-{identifier}' for p in photos):
                identifier += 1
            photos.append({'id': f'photo-{identifier}', 'pixelBox': [int(round(v/factor)) for v in fitted],
                           'layoutRecovered': True, 'confidence': .78, 'label': None, 'labelSource': 'unread'})
            boxes.append(np.asarray(fitted, float)); added += 1
        if not added:
            break
    return photos


def numbered_panels(image, photos, controls):
    """Complete less regular albums using their repeated printed card labels.

    Some cards touch the map, or two photographs have no white gutter. A
    known album supplies label size and panel proportions; standalone labels
    of that size propose additional crops. A crop must have photographic
    content and rectangular edges, and cannot overlap a course circle.
    """
    if len(photos) < 3:
        return photos
    factor = min(1., 1920/max(image.shape[:2]))
    small = cv2.resize(image, None, fx=factor, fy=factor, interpolation=cv2.INTER_AREA) if factor < 1 else image
    b, g, r = (v.astype(np.int16) for v in cv2.split(small))
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    hue = _course_hue(small, controls, factor)
    purple = (r-g > 22) & (b-g > 18) & (r > 85) & (b > 75)
    if hue is not None:
        purple &= np.abs(hsv[:, :, 0].astype(float)-hue) <= 9
    separator = purple | ((hsv[:, :, 1] < 35) & (hsv[:, :, 2] > 208))
    _, labels, stats, _ = cv2.connectedComponentsWithStats(purple.astype(np.uint8), 8)
    boxes = [np.array(photo['pixelBox'])*factor for photo in photos]
    heights = []
    for x0, y0, x1, y1 in boxes:
        for x, y, w, h, area in stats[1:]:
            if (x >= x0+(x1-x0)*.4 and x+w <= x1+2 and y >= y0+(y1-y0)*.6 and y+h <= y1+2
                    and (y1-y0)*.09 < h < (y1-y0)*.4 and .1 < w/h < 1.1 and area > h*h*.07):
                heights.append(h)
    if len(heights) < 3:
        return photos
    typical = max(heights, key=lambda h: (sum(abs(value-h) < h*.12 for value in heights), h))
    # Retain large photographic purple objects; only text-sized components
    # are excluded from the texture evidence below.
    text_parts = np.array([False]+[typical*.6 <= s[3] <= typical*1.8 and s[2] < s[3]*2 for s in stats[1:]])
    typography = text_parts[labels]
    glyphs = [s[:4] for s in stats[1:] if typical*.8 <= s[3] <= typical*1.22 and .1 < s[2]/s[3] < 1.1 and s[4] > s[3]*s[3]*.07]
    groups, used = [], set()
    for i, (x, y, w, h) in sorted(enumerate(glyphs), key=lambda item: (item[1][1], item[1][0])):
        if i in used:
            continue
        row = [(x, y, w, h)]; used.add(i); end = x+w
        for j, (xx, yy, ww, hh) in sorted(enumerate(glyphs), key=lambda item: item[1][0]):
            if j not in used and -h*.15 <= xx-end <= h*.75 and abs(yy+hh/2-y-h/2) <= h*.25:
                row.append((xx, yy, ww, hh)); used.add(j); end = xx+ww
        if 2 <= len(row) <= 3:
            rect = np.asarray(row)
            left, top = rect[:, :2].min(axis=0)
            right, bottom = (rect[:, :2]+rect[:, 2:]).max(axis=0)
            groups.append(np.array([left, top, right, bottom], float))
    examples = []
    for photo, box in zip(photos, boxes):
        options = [group for group in groups if group[0] > box[0]+(box[2]-box[0])*.4 and group[2] <= box[2]+2
                   and group[1] > box[1]+(box[3]-box[1])*.6 and group[3] <= box[3]+2]
        if options:
            group = min(options, key=lambda g: np.linalg.norm(box[2:]-g[2:]))
            photo['numberPixelBox'] = [int(round(v/factor)) for v in group]
            examples.append([*(box[2:]-box[:2]), *np.maximum(2, box[2:]-group[2:])])
    if len(examples) < 2:
        return photos
    panel_w, panel_h, pad_x, pad_y = np.median(examples, axis=0)
    for group in groups:
        middle = (group[:2]+group[2:])/2
        if any(np.all(middle >= box[:2]) and np.all(middle <= box[2:]) for box in boxes):
            continue
        w, h = panel_w, panel_h  # label font size can change independently
        right, bottom = group[2:]+[pad_x, pad_y]
        initial = np.rint([right-w, bottom-h, right, bottom]).astype(int)
        if np.any(initial[:2] < 0) or np.any(initial[2:] > small.shape[1::-1]):
            continue  # infer a complete card, not a clipped piece of a title
        initial[:2] = np.maximum(0, initial[:2]); initial[2:] = np.minimum(small.shape[1::-1], initial[2:])
        left, top, right, bottom = initial
        if right <= left or bottom <= top:
            continue
        fitted, edges = [], []
        for side, (axis, value, span) in enumerate(((0, left, w), (1, top, h), (0, right, w), (1, bottom, h))):
            lo = max(0, int(value-span*.12)); hi = min(small.shape[1-axis]-1, int(value+span*.12))
            positions = np.arange(lo, hi+1)
            if side == 2:
                positions = positions[positions >= group[2]+2]
            elif side == 3:
                positions = positions[positions >= group[3]+2]
            if not len(positions):
                break
            values = separator[top:bottom, positions].mean(axis=0) if axis == 0 else separator[positions, left:right].mean(axis=1)
            best = int(np.argmax(values-np.abs(positions-value)*.002))
            fitted.append(int(positions[best])); edges.append(float(values[best]))
        if len(fitted) != 4:
            continue
        x0, y0, x1, y1 = fitted
        if not (.65*w < x1-x0 < 1.35*w and .65*h < y1-y0 < 1.35*h):
            continue
        # Ordinary map labels sit just beyond their circles; include that
        # margin when excluding crops, so a label cannot erase its own map.
        if any(x0-c['radiusPx']*factor*1.2 <= c['pixel'][0]*factor <= x1+c['radiusPx']*factor*1.2 and
               y0-c['radiusPx']*factor*1.2 <= c['pixel'][1]*factor <= y1+c['radiusPx']*factor*1.2 for c in controls):
            continue
        # Large printed instructions and a coloured page banner can look
        # textured too. Course typography is not photographic evidence.
        clean = small[y0:y1, x0:x1].copy()
        course_ink = typography[y0:y1, x0:x1]
        clean[course_ink] = 255
        texture = _texture(clean)
        content = ((hsv[y0:y1, x0:x1, 2] < 248) | (hsv[y0:y1, x0:x1, 1] > 20)) & ~course_ink
        if content.mean() < .4 or not ((sum(v >= .55 for v in edges) >= 3 and texture >= 1.) or
                                       (sum(v >= .55 for v in edges) >= 2 and texture >= 2.6)):
            continue
        box = np.array(fitted, float)
        if any(max(0, min(box[2], p[2])-max(box[0], p[0]))*max(0, min(box[3], p[3])-max(box[1], p[1])) >
               .5*(box[2]-box[0])*(box[3]-box[1]) for p in boxes):
            continue
        boxes.append(box)
        photos.append({'id': f'photo-{len(photos)+1}', 'pixelBox': [int(round(v/factor)) for v in fitted],
                       'numberPixelBox': [int(round(v/factor)) for v in group],
                       'confidence': .78, 'layoutRecovered': True, 'label': None, 'labelSource': 'unread'})
    return photos


def photograph_mask(shape, photos):
    mask = np.zeros(shape[:2], np.uint8)
    for photo in photos:
        x0, y0, x1, y1 = photo['pixelBox']
        mask[y0:y1, x0:x1] = 255
    return mask


def label_photos(image, photos, controls=()):
    """Read the printed card number, independently of the map's control order."""
    executable = os.environ.get('DIRMAP_TESSERACT') or shutil.which('tesseract')
    if not executable or not photos:
        return
    hue = _course_hue(image, controls)
    normal = [c for c in controls if c.get('role', 'control') == 'control']
    known = {c.get('label') for c in normal}
    known = known if len(known) == len(normal) and len(normal) >= 3 and all(isinstance(s, str) and s.isdigit() for s in known) else None
    pending, examples = [], {}
    with tempfile.TemporaryDirectory(prefix='dirmap-photo-ocr-') as temporary:
        output = Path(temporary)/'number.png'
        for photo in photos:
            x0, y0, x1, y1 = photo['pixelBox']
            w, h = x1-x0, y1-y0
            # Course numbers are commonly overprinted in a lower corner.
            # Keep a little of the gutter, where other sheets put captions.
            left, top = max(0, x0), max(0, y0+round(h*.5))
            right, bottom = min(image.shape[1], x1+2), min(image.shape[0], y1+round(h*.08))
            if photo.get('numberPixelBox'):
                nx0, ny0, nx1, ny1 = photo['numberPixelBox']
                margin = max(3, round((ny1-ny0)*.12))
                left, top = max(0, nx0-margin), max(0, ny0-margin)
                right, bottom = min(image.shape[1], nx1+margin), min(image.shape[0], ny1+margin)
            crop = image[top:bottom, left:right]
            b, g, r = (v.astype(np.int16) for v in cv2.split(crop))
            ink = (r-g > 22) & (b-g > 18) & (r > 85) & (b > 75)
            broad = ink.astype(np.uint8)*255
            if hue is not None:
                ink &= np.abs(cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)[:, :, 0].astype(float)-hue) <= 9
            ink = ink.astype(np.uint8)*255
            # Remove only long rules, never a digit's shorter upright stroke.
            for kernel in (np.ones((1, max(20, round(w*.7))), np.uint8), np.ones((max(20, round(h*.32)), 1), np.uint8)):
                ink &= cv2.bitwise_not(cv2.morphologyEx(ink, cv2.MORPH_OPEN, kernel))
            count, components, stats, _ = cv2.connectedComponentsWithStats(ink, 8)
            keep = np.array([False]+[s[2] >= max(2, h*.025) and h*.07 <= s[3] <= h*.32 and s[4] >= h*.15 for s in stats[1:]])
            ink = keep[components].astype(np.uint8)*255
            words = number_words(ink, executable, output, (left, top), scale=max(1, 180/max(1, ink.shape[1])))
            if known:
                words = [word for word in words if word['label'] in known or word['confidence'] >= 90]
            if not words:
                ys, xs = np.nonzero(ink)
                if not len(xs):
                    continue
                xa, xb, ya, yb = xs.min(), xs.max()+1, ys.min(), ys.max()+1
                words = number_words(ink[ya:yb, xa:xb], executable, output, (left+xa, top+ya),
                                     scale=max(1, 48/(yb-ya)), psm=7)
                if known:
                    words = [word for word in words if word['label'] in known or word['confidence'] >= 90]
            if not words and photo.get('numberPixelBox'):
                # Hue can shift along a thin, translucent serif over a photo.
                # Its measured text box permits a wider colour retry without
                # collecting similarly coloured decoration elsewhere.
                words = number_words(broad, executable, output, (left, top), scale=1.5, psm=7)
                if known:
                    words = [word for word in words if word['label'] in known or word['confidence'] >= 90]
            words = [p for p in words if x0 <= p['center'][0] <= x1 and y0 <= p['center'][1] <= y1+h*.08]
            labels = {p['label'] for p in words}
            if len(labels) == 1:
                word = max(words, key=lambda p: p['confidence'])
                photo.update(label=word['label'], labelSource='tesseract', labelConfidence=round(word['confidence']/100, 3))
                xa, ya, xb, yb = np.rint(np.array(word['box'])-[left, top, left, top]).astype(int)
                glyphs = _glyphs(ink[max(0, ya):yb, max(0, xa):xb])
                if len(glyphs) == len(word['label']):
                    for digit, glyph in zip(word['label'], glyphs):
                        examples.setdefault(digit, []).append(glyph)
            else:
                pending.append((photo, _glyphs(ink)))
    # Printed card numbers share a typeface. When OCR is uncertain, use
    # glyphs it confidently read elsewhere on this sheet, not sequential
    # numbering or a guessed association with the nearest control.
    for photo, glyphs in pending:
        if not 1 <= len(glyphs) <= 3 or len(examples) < 5:
            continue
        digits, confidence = [], []
        for glyph in glyphs:
            scores = sorted(((max(_glyph_score(glyph, other) for other in templates), digit)
                             for digit, templates in examples.items()), reverse=True)
            if scores[0][0] < .86 or scores[0][0]-scores[1][0] < .08:
                break
            confidence.append(scores[0][0]); digits.append(scores[0][1])
        if len(digits) == len(glyphs) and digits[0] != '0':
            photo.update(label=''.join(digits), labelSource='sheet_glyphs', labelConfidence=round(min(confidence), 3))


def _glyphs(ink):
    if not ink.size:
        return []
    _, labels, stats, _ = cv2.connectedComponentsWithStats(ink, 8)
    glyphs = []
    for index, (x, y, w, h, area) in sorted(enumerate(stats[1:], 1), key=lambda item: item[1][0]):
        if area < 6 or h < 5 or w < 2 or w > h*1.15:
            continue
        crop = (labels[y:y+h, x:x+w] == index).astype(np.uint8)
        shape = (max(1, round(w*32/h)), 32)
        resized = cv2.resize(crop, shape, interpolation=cv2.INTER_NEAREST)
        canvas = np.zeros((36, 44), np.uint8)
        left = (44-shape[0])//2
        canvas[2:34, left:left+shape[0]] = resized
        glyphs.append(canvas)
    return glyphs


def _glyph_score(a, b):
    return max(float(2*np.sum(a & np.roll(b, (dy, dx), (0, 1)))/max(1, int(a.sum())+int(b.sum())))
               for dx in (-1, 0, 1) for dy in (-1, 0, 1))


def link_photos(photos, controls):
    for photo in photos:
        matches = [c['id'] for c in controls if c['label'] == photo.get('label')
                   and c.get('role', 'control') == 'control' and c.get('state') != 'rejected']
        photo['controlId'] = matches[0] if len(matches) == 1 else None


def reconcile_photos(photos, previous, controls):
    used = set()
    for photo in photos:
        matches = [(float(np.linalg.norm(np.array(p['pixelBox'])-photo['pixelBox'])), p)
                   for p in previous if p['id'] not in used]
        if matches:
            distance, old = min(matches, key=lambda item: item[0])
            if distance < 20:
                photo['id'] = old['id']; used.add(old['id'])
                if old.get('labelSource') == 'manual':
                    photo.update(label=old['label'], labelSource='manual')
                continue
        photo['id'] = ''
    reserved = {p['id'] for p in previous} | used
    for photo in photos:
        if not photo['id']:
            index = 1
            while f'photo-{index}' in reserved:
                index += 1
            photo['id'] = f'photo-{index}'; reserved.add(photo['id'])
    for old in previous:
        if old['id'] not in used and old.get('labelSource') == 'manual':
            photos.append(dict(old))
    link_photos(photos, controls)
    return photos


def photo_jpeg(image, photo):
    x0, y0, x1, y1 = photo['pixelBox']
    crop = image[max(0, y0):min(image.shape[0], y1), max(0, x0):min(image.shape[1], x1)]
    if not crop.size:
        raise ValueError('参考照片超出原图')
    ratio = min(1., 960/max(crop.shape[:2]))
    if ratio < 1:
        crop = cv2.resize(crop, None, fx=ratio, fy=ratio, interpolation=cv2.INTER_AREA)
    ok, encoded = cv2.imencode('.jpg', crop, [cv2.IMWRITE_JPEG_QUALITY, 92])
    if not ok:
        raise ValueError('无法读取参考照片')
    return encoded.tobytes()


def save_photos(image, photos, directory):
    # JPEG cards are much smaller than another full-resolution sheet. The
    # API can serve a grid without decoding the large image for every tile.
    with zipfile.ZipFile(Path(directory)/'sheet-photos.zip', 'w') as archive:
        for photo in photos:
            archive.writestr(f'{photo["id"]}.jpg', photo_jpeg(image, photo))


def archived_photo(path, photo_id):
    try:
        with zipfile.ZipFile(path) as archive:
            return archive.read(f'{photo_id}.jpg')
    except (OSError, KeyError, zipfile.BadZipFile):
        raise ValueError('参考照片不可用，请更新识别') from None
