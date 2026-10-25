"""Recognize straight ISSprOM stair flights from repeated enclosed treads.

The repeated light cells establish the flight geometry. Only thin, nearly
full-width tread strokes are removable; side rails, columns, thick crossing
walls and all protected area classes keep their original blocking effect.
"""
import cv2
import numpy as np
from scipy.spatial import cKDTree


def _terminal_tread(gray, neutral_ink, protected, cell, axis, normal, expected, stroke_limit):
    """Keep a wall on part of a landing while recovering its thin stair exit.

    A clipped flight can enter at one side of a wide landing and leave at
    the other. Averaging thickness across that landing either loses the
    exit or opens the adjacent retaining wall. Measure each cross-section.
    """
    projection = cell['points'] @ axis
    lo_u, hi_u = float(projection.min()+.4), float(projection.max()-.4)
    us = np.linspace(lo_u, hi_u, max(3, round(hi_u-lo_u)+1))
    pad = max(4, int(np.ceil(stroke_limit*3)))
    ts = np.arange(expected-pad, expected+pad+.25, .5)
    points = us[None, :, None]*axis+ts[:, None, None]*normal
    sampled = cv2.remap(gray, points[:, :, 0].astype(np.float32), points[:, :, 1].astype(np.float32),
                        cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=255).astype(float)
    neutral = cv2.remap(neutral_ink, points[:, :, 0].astype(np.float32), points[:, :, 1].astype(np.float32),
                        cv2.INTER_NEAREST, borderMode=cv2.BORDER_CONSTANT)
    contrast = np.clip((cell['tone']-sampled)/cell['tone'], 0, 1)
    nearby = np.flatnonzero(abs(ts-expected) <= 1)
    lows, highs = np.full(len(us), np.inf), np.full(len(us), -np.inf)
    for column in range(len(us)):
        peak = int(nearby[np.argmax(contrast[nearby, column])])
        if contrast[peak, column] < .15 or not neutral[peak, column]:
            continue
        low = high = peak
        while low > 0 and contrast[low-1, column] > .12:
            low -= 1
        while high < len(ts)-1 and contrast[high+1, column] > .12:
            high += 1
        if low == 0 or high == len(ts)-1 or np.sum(contrast[low:high+1, column])*.5 > stroke_limit:
            continue
        lows[column], highs[column] = ts[low]-.25, ts[high]+.25
    usable = np.isfinite(lows)
    runs = np.diff(np.r_[False, usable, False].astype(int))
    widths = np.flatnonzero(runs == -1)-np.flatnonzero(runs == 1)
    if np.mean(usable) < .35 or not len(widths) or max(widths) < 3:
        return None
    corners = np.array([lo_u*axis+ts[0]*normal, hi_u*axis+ts[0]*normal,
                        lo_u*axis+ts[-1]*normal, hi_u*axis+ts[-1]*normal])
    x0, y0 = np.maximum(0, np.floor(corners.min(axis=0)-1)).astype(int)
    x1, y1 = np.minimum(gray.shape[::-1], np.ceil(corners.max(axis=0)+2)).astype(int)
    yy, xx = np.mgrid[y0:y1, x0:x1]
    pixels = np.stack((xx, yy), axis=-1)
    u, v = pixels @ axis, pixels @ normal
    columns = np.clip(np.rint((u-lo_u)/(hi_u-lo_u)*(len(us)-1)).astype(int), 0, len(us)-1)
    mask = ((u >= lo_u) & (u <= hi_u) & (v >= lows[columns]) & (v <= highs[columns])
            & (protected[y0:y1, x0:x1] == 0))
    return (x0, y0, x1, y1), mask


def detect_stairs(image, controls, protected):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    neutral_ink = ((hsv[:, :, 1] < 125)
                   & ~((hsv[:, :, 0] >= 35) & (hsv[:, :, 0] <= 110) & (hsv[:, :, 1] >= 30))).astype(np.uint8)
    shape = gray.shape
    flight_mask = np.zeros(shape, np.uint8)
    tread_mask = np.zeros(shape, np.uint8)
    radii = [c['radiusPx'] for c in controls]
    radius = float(np.median(radii)) if radii else 15.
    # A control circle has a 3 mm radius; stair strokes are 0.1 mm.
    # Allow print/scan antialiasing, while retaining the 0.4 mm wall class.
    stroke_limit = max(1.4, radius/30*1.6+.25)
    # A protected edge pixel must remain blocked, but it need not discard
    # the rest of an otherwise visible tread cell. Segment visible ground
    # after removing protected pixels, instead of rejecting a whole cell.
    light_ground = ((gray > 185) & (protected == 0)).astype(np.uint8)
    _, labels, stats, centers = cv2.connectedComponentsWithStats(light_ground, connectivity=4)
    cells = []
    for label, (x, y, width, height, area) in enumerate(stats):
        if label == 0 or not 3 <= area <= radius*radius*2 or max(width, height) > radius*3:
            continue
        local = labels[y:y+height, x:x+width] == label
        yy, xx = np.nonzero(local)
        # Treads may be white, paved, or canopy grey, but not blue water,
        # olive gardens, or green vegetation with repeated plant markings.
        color = hsv[y:y+height, x:x+width][local]
        ground = ((color[:, 1] < 45) | ((color[:, 0] < 35) & (color[:, 1] < 170))) & (color[:, 2] > 200)
        if np.mean(ground) < .85:
            continue
        points = np.column_stack((xx+x, yy+y)).astype(np.float32)
        rect = cv2.minAreaRect(points)
        box = cv2.boxPoints(rect)
        vectors = np.roll(box, -1, axis=0)-box
        along = vectors[np.argmax(np.linalg.norm(vectors, axis=1))]
        length = np.linalg.norm(along)
        if length < 1:
            continue
        axis = along/length
        normal = np.array([-axis[1], axis[0]])
        u, v = points @ axis, points @ normal
        span, depth = np.ptp(u)+1, np.ptp(v)+1
        if span < max(2.5, radius*.16) or not 1.3 <= span/depth <= 30 or depth > max(4, radius*.4)+1.5:
            continue
        if area/(span*depth) < .65:
            continue
        cells.append({'label': label, 'center': centers[label], 'axis': axis, 'span': span, 'depth': depth,
                      'points': points, 'tone': float(np.median(gray[y:y+height, x:x+width][local]))})
    if len(cells) < 2:
        return flight_mask, tread_mask, []
    tree = cKDTree([c['center'] for c in cells])
    parent = list(range(len(cells)))
    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index
    for i, cell in enumerate(cells):
        for j in tree.query_ball_point(cell['center'], cell['depth']*2+stroke_limit+2):
            if j <= i:
                continue
            other = cells[j]
            if abs(np.dot(cell['axis'], other['axis'])) < .985:
                continue
            delta = other['center']-cell['center']
            sideways = abs(np.dot(delta, cell['axis']))
            forward = abs(cell['axis'][0]*delta[1]-cell['axis'][1]*delta[0])
            gap = forward-(cell['depth']+other['depth'])/2
            # Rotated pixel cells can overlap slightly in their projected
            # bounding boxes even though a real dark rung separates them.
            similar = (abs(cell['span']-other['span']) <= max(1.5, .2*cell['span'])
                       and sideways <= max(1.1, cell['span']*.12))
            # A short flight may have one tread partly occupied by a planted
            # corner. The remaining light cells still share a narrow tread.
            # Compare their actual overlap instead of shifted centroids.
            one, two = cell['points'] @ cell['axis'], other['points'] @ cell['axis']
            overlap = min(one.max(), two.max())-max(one.min(), two.min())
            clipped = overlap >= max(radius*.16, min(cell['span'], other['span'])*.65)
            if (not similar and not clipped) or not -.75 <= gap <= stroke_limit+1.5:
                continue
            parent[find(j)] = find(i)
    groups = {}
    for index in range(len(cells)):
        groups.setdefault(find(index), []).append(cells[index])
    detections = []
    for group in groups.values():
        if len(group) < 2:
            continue
        axis = np.asarray(group[0]['axis'], float)
        normal = np.array([-axis[1], axis[0]])
        group.sort(key=lambda c: c['center'] @ normal)
        centres = np.array([c['center'] @ normal for c in group])
        separations = np.diff(centres)
        spacing = float(np.median(separations))
        if np.any(abs(separations-spacing) > max(1.1, spacing*.3)):
            continue
        # The two rails need not be parallel or perpendicular to the treads:
        # scanned flights can shear, and real flights can taper. Fit each
        # side to the observed cells instead of forcing a single rectangle.
        origin = float(np.mean(centres))
        left = np.array([np.min(c['points'] @ axis) for c in group])
        right = np.array([np.max(c['points'] @ axis) for c in group])
        design = np.column_stack((np.ones(len(group)), centres-origin))
        left_fit = np.linalg.lstsq(design, left, rcond=None)[0]
        right_fit = np.linalg.lstsq(design, right, rcond=None)[0]
        short_clipped = len(group) == 2 and np.ptp(right-left) > max(1.5, np.max(right-left)*.2)
        if short_clipped:
            # Only the common clear corridor is stair ground. The shrub or
            # roof occupying the rest of a cell remains protected below.
            left_fit = np.array([left.max(), 0.])
            right_fit = np.array([right.min(), 0.])
        tolerance = max(1.25, float(np.median(right-left))*.08)
        if not short_clipped and (max(abs(left_fit[1]), abs(right_fit[1])) > 1.2
                or max(np.max(abs(design @ left_fit-left)), np.max(abs(design @ right_fit-right))) > tolerance):
            continue
        # Extrema occur at a cell's near/far end on a slanted rail. Move
        # inward to its centre line and keep the rail itself out of the mask.
        depth = float(np.median([c['depth'] for c in group]))
        left_fit[0] += abs(left_fit[1])*depth/2+.4
        right_fit[0] -= abs(right_fit[1])*depth/2+.4
        left_edge = lambda t: left_fit[0]+left_fit[1]*(t-origin)
        right_edge = lambda t: right_fit[0]+right_fit[1]*(t-origin)
        t0, t1 = centres[0]-spacing/2, centres[-1]+spacing/2
        widths = right_edge(np.array([t0, t1]))-left_edge(np.array([t0, t1]))
        if np.min(widths) < 1:
            continue
        tone = float(np.median([c['tone'] for c in group]))
        pad = max(4, int(np.ceil(stroke_limit*3)))
        fractions = np.linspace(0, 1, max(3, round(float(np.max(widths)))+1))
        ts = np.arange(t0-pad, t1+pad+.25, .5)
        us = left_edge(ts)[:, None]*(1-fractions)+right_edge(ts)[:, None]*fractions
        coordinates = us[:, :, None]*axis+ts[:, None, None]*normal
        # Two independent outlined boxes have exterior light ground BETWEEN
        # their outlines. Adjacent stair cells share one dark tread line;
        # the cross-section can contain only those two light components.
        interior_columns = (fractions >= .18) & (fractions <= .82)
        xy = np.rint(coordinates[:, interior_columns]).astype(int)
        valid = ((xy[:, :, 0] >= 0) & (xy[:, :, 0] < shape[1])
                 & (xy[:, :, 1] >= 0) & (xy[:, :, 1] < shape[0]))
        sampled_labels = np.zeros(xy.shape[:2], labels.dtype)
        sampled_labels[valid] = labels[xy[:, :, 1][valid], xy[:, :, 0][valid]]
        shared = True
        for first, second, a, b in zip(group, group[1:], centres, centres[1:]):
            values = sampled_labels[(ts > a) & (ts < b)]
            foreign = ((values != 0) & (values != first['label']) & (values != second['label'])
                       & (stats[values, cv2.CC_STAT_AREA] >= 3))
            if np.any(np.mean(foreign, axis=1) > .2):
                shared = False
                break
        if not shared:
            continue
        samples = cv2.remap(gray, coordinates[:, :, 0].astype(np.float32), coordinates[:, :, 1].astype(np.float32),
                            cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=255)
        ink_samples = cv2.remap(neutral_ink, coordinates[:, :, 0].astype(np.float32), coordinates[:, :, 1].astype(np.float32),
                               cv2.INTER_NEAREST, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        contrast = np.clip((tone-samples.astype(float))/tone, 0, 1)
        opacity = np.mean(contrast, axis=1)
        # Bilinear samples of a one-pixel tread may be lighter than an
        # absolute grey cutoff. Its contrast to this flight's visible
        # ground is the relevant evidence, including on pale paving.
        coverage = np.mean(contrast > .15, axis=1)
        bands = []
        band_indexes = set()
        terminals = {}
        interior_count = 0
        expected_bands = [t0, *((centres[:-1]+centres[1:])/2).tolist(), t1]
        for band_index, expected in enumerate(expected_bands):
            if short_clipped and band_index in (0, len(expected_bands)-1):
                terminal = _terminal_tread(gray, neutral_ink, protected,
                                           group[0] if band_index == 0 else group[-1],
                                           axis, normal, expected, stroke_limit)
                if terminal is not None:
                    terminals[band_index] = terminal
            nearby = np.flatnonzero(abs(ts-expected) <= 1)
            if not len(nearby):
                continue
            peak = int(nearby[np.argmax(opacity[nearby])])
            if opacity[peak] < .15 or coverage[peak] < .65:
                continue
            dark_columns = contrast[peak] > .15
            if np.mean(ink_samples[peak][dark_columns]) < .85:
                continue  # vegetation hatching is not a set of black steps
            lo = hi = peak
            while lo > 0 and opacity[lo-1] > .12:
                lo -= 1
            while hi < len(ts)-1 and opacity[hi+1] > .12:
                hi += 1
            effective_width = np.sum(opacity[lo:hi+1])*.5
            if lo == 0 or hi == len(ts)-1 or effective_width > stroke_limit:
                continue
            bands.append((float(ts[lo]-.25), float(ts[hi]+.25)))
            band_indexes.add(band_index)
            interior_count += 0 < band_index < len(expected_bands)-1
        if (interior_count < min(2, len(group)-1)
                or (len(group) == 2 and len(band_indexes | terminals.keys()) != 3)):
            continue
        # Real flights have two continuous straight rails. Separate rounded
        # parking bays also make repeated light cells, but their curved ends
        # cannot supply these rails. A solid neighbouring roof is not a rail:
        # require an ink trough darker than both sides of the stroke.
        rail_ts = np.arange(centres[0], centres[-1]+.25, .5)
        rail_count = 0
        flank = max(2.5, stroke_limit*2+1)
        for edge, sign in ((left_edge, -1), (right_edge, 1)):
            supported = False
            for offset in np.arange(.5, flank+1, .5):
                centre_u = edge(rail_ts)+sign*offset
                probes = centre_u[:, None]+np.array([-flank, -.5, 0, .5, flank])
                xy = probes[:, :, None]*axis+rail_ts[:, None, None]*normal
                values = cv2.remap(gray, xy[:, :, 0].astype(np.float32), xy[:, :, 1].astype(np.float32),
                                   cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=255).astype(float)
                ink = cv2.remap(neutral_ink, xy[:, :, 0].astype(np.float32), xy[:, :, 1].astype(np.float32),
                               cv2.INTER_NEAREST, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
                middle = values[:, 1:4].min(axis=1)
                support = (middle+25 < np.minimum(values[:, 0], values[:, -1])) & (ink[:, 1:4].max(axis=1) > 0)
                # Taking the darkest of several neighbouring samples alone
                # can follow the curved caps of repeated separate objects.
                # The fitted rail centre must itself remain continuous.
                centre_support = ((values[:, 2]+25 < np.minimum(values[:, 0], values[:, -1]))
                                  & (ink[:, 2] > 0))
                if np.mean(support) >= .8 and np.mean(centre_support) >= .75:
                    supported = True
                    break
            rail_count += supported
        # With a clipped two-cell flight, the artificial edge of the common
        # strip is inside the wider cell. Its other, observed side must still
        # have a continuous rail. Its two landings need thin exits, which can
        # occupy different parts of the width beside a retained thick wall.
        if rail_count < (1 if short_clipped else 2):
            continue
        corners = np.array([left_edge(t0)*axis+t0*normal, right_edge(t0)*axis+t0*normal,
                            right_edge(t1)*axis+t1*normal, left_edge(t1)*axis+t1*normal])
        lower, upper = min(t0, min(a for a, _ in bands)), max(t1, max(b for _, b in bands))
        bounds = np.array([left_edge(lower)*axis+lower*normal, right_edge(lower)*axis+lower*normal,
                           right_edge(upper)*axis+upper*normal, left_edge(upper)*axis+upper*normal])
        x0, y0 = np.maximum(0, np.floor(bounds.min(axis=0)-1)).astype(int)
        x1, y1 = np.minimum([shape[1], shape[0]], np.ceil(bounds.max(axis=0)+2)).astype(int)
        yy, xx = np.mgrid[y0:y1, x0:x1]
        points = np.stack((xx, yy), axis=-1)
        u, v = points @ axis, points @ normal
        inside = (u >= left_edge(v)) & (u <= right_edge(v)) & (v >= lower) & (v <= upper)
        inside &= protected[y0:y1, x0:x1] == 0
        allowed = np.zeros(inside.shape, bool)
        for low, high in bands:
            allowed |= (v >= low) & (v <= high)
        flight_mask[y0:y1, x0:x1][inside] = 255
        tread_mask[y0:y1, x0:x1][inside & allowed] = 255
        for (bx0, by0, bx1, by1), mask in terminals.values():
            tread_mask[by0:by1, bx0:bx1][mask] = 255
            flight_mask[by0:by1, bx0:bx1][mask] = 255
        detections.append({'polygon': np.round(corners, 2).tolist(), 'visibleTreadCells': len(group),
                           'traversableTreadLines': len(band_indexes | terminals.keys()),
                           'method': 'clipped_short_flight' if short_clipped else 'shared_treads_and_observed_rails'})
    return flight_mask, tread_mask, detections
