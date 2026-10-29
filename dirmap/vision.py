"""Colour/shape extraction with separate course ink and forbidden masks.

The output is a candidate interpretation. Grey buildings, blue water, dark
green and detected purple hatching are hard obstacles; ambiguous markings
remain visible for review. Source pixels are never overwritten.
"""

from pathlib import Path

import cv2
import numpy as np

from .reference import polygon_properties
from .symbols import classify_course_symbols, course_ink_mask
from .overprint import annotation_mask, restore, continue_roof_boundaries
from .stairs import detect_stairs
from .hatching import lower_level_hatching
from .protocol import TERRAIN_SCHEMA
from .images import read_map_image


def read_image(path, target_size=None):
    return read_map_image(path, target_size)[0]


def colour_masks(image):
    b, g, r = (channel.astype(np.int16) for channel in cv2.split(image))
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    h, s, v = cv2.split(hsv)
    purple = ((r-g > 22) & (b-g > 18) & (r > 85) & (b > 75)).astype(np.uint8)*255
    water = (((b-r > 24) & (g-r > 15) & (g > 95) & (h > 78) & (h < 111)) & (purple == 0)).astype(np.uint8)*255
    # The supplied roof fill straddles V=179 after JPEG compression (and
    # after removing purple overprint). A cutoff at that value breaks a
    # solid roof into traversable speckles. Keep the medium grey palette;
    # light grey canopies/passages (about V=215) remain separate.
    building = ((s < 48) & (v > 58) & (v < 196)).astype(np.uint8)*255
    green = ((g-r > 9) & (g-b > 8) & (h >= 32) & (h <= 90)).astype(np.uint8)*255
    dark_green = ((green > 0) & (v < 159) & (s > 80)).astype(np.uint8)*255
    black = ((v < 108) & (s < 115)).astype(np.uint8)*255
    # Olive green is the ISSprOM settlement/out-of-bounds fill, unlike yellow
    # open land or light green vegetation. Bright red custom course ink is
    # unknown, never treated as a new road.
    olive = ((h >= 22) & (h <= 39) & (s > 62) & (v < 216) & (v > 90)).astype(np.uint8)*255
    ink_threshold = max(45., float(np.percentile(s[purple > 0], 90))*.55) if np.any(purple) else 100.
    course_ink = purple & ((s >= ink_threshold).astype(np.uint8)*255)
    return dict(purple=purple, water=water, building=building, green=green,
                dark_green=dark_green, black=black, olive=olive,
                course_ink=course_ink,
                course_text=cv2.bitwise_and(purple, (s >= 100).astype(np.uint8)*255))


def north_guides(mask, native):
    """Find a repeated parallel guide family even on a photo montage.

    A map may occupy only part of the upload. Requiring each guide to span
    most of the whole image missed clean HD strokes and split the campus
    into strips. Short strokes need regularly spaced parallel neighbours;
    a lone stream or an arbitrary water edge is not enough evidence.
    """
    height, width = mask.shape
    guides = np.zeros_like(mask)
    lines = cv2.HoughLinesP(mask, 1, np.pi/720, threshold=max(60, int(height*.12)),
                            minLineLength=max(150, int(height*.25)), maxLineGap=max(8, int(height*.06)))
    candidates = []
    for line in lines[:, 0] if lines is not None else []:
        x1, y1, x2, y2 = map(int, line)
        if abs(x2-x1) >= abs(y2-y1)*.12:
            continue
        slope = (x2-x1)/(y2-y1)
        middle_x = x1+(height/2-y1)*slope
        candidate = (middle_x, slope, min(y1, y2), max(y1, y2), (x1, y1, x2, y2))
        # Hough often emits both edges of the same anti-aliased stroke.
        same = next((i for i, c in enumerate(candidates) if abs(c[0]-middle_x) < native*2), None)
        if same is None:
            candidates.append(candidate)
        elif abs(y2-y1) > candidates[same][3]-candidates[same][2]:
            candidates[same] = candidate
    candidates.sort()
    accepted = {i for i, c in enumerate(candidates) if c[3]-c[2] >= height*.85}
    for i in range(len(candidates)-2):
        a, b, c = candidates[i:i+3]
        first, second = b[0]-a[0], c[0]-b[0]
        overlap = min(v[3] for v in (a, b, c))-max(v[2] for v in (a, b, c))
        ratio = second/first if first else 0
        # One guide can disappear inside buildings, a lake or overprint.
        # A tightly matched double spacing still gives three observations;
        # do not accept arbitrary pairs of parallel streams as a family.
        regular = .88 <= ratio <= 1.12 or abs(ratio-2) <= .06 or abs(ratio-.5) <= .015
        if (min(first, second) >= max(native*8, width*.02) and regular
                and max(v[1] for v in (a, b, c))-min(v[1] for v in (a, b, c)) < .01
                and overlap > height*.12):
            accepted.update((i, i+1, i+2))
    for i in accepted:
        x1, y1, x2, y2 = candidates[i][4]
        cv2.line(guides, (x1, y1), (x2, y2), 255, native)
    return guides


def water_obstacles(mask):
    """Keep water bodies and narrow streams, removing evidenced north guides."""
    height, width = mask.shape
    native = max(5, int(round(np.sqrt(height*width)/420)) | 1)
    bodies = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((native, native), np.uint8))
    guides = north_guides(mask, native)
    # Wide blue bodies survive even where a magnetic-north line crosses them.
    return cv2.bitwise_or(bodies, cv2.bitwise_and(mask, cv2.bitwise_not(guides)))


def paved_boundaries(image, masks, controls):
    """Thin ink separating light paving from open green is an area boundary.

    On clean scans, black blended with green becomes a continuous dark-green
    fringe. Treating every fringe pixel as dense vegetation made the same
    path accessible only after JPEG damage broke the line. Require both
    visible surface colours, the print-scale thin stroke and an extended
    boundary. A thick wall, roof, water or forbidden fill is never removed.
    """
    hue, saturation, value = cv2.split(cv2.cvtColor(image, cv2.COLOR_BGR2HSV))
    paving = (hue <= 25) & (saturation >= 18) & (saturation <= 100) & (value >= 205)
    open_green = (masks['green'] > 0) & (value >= 195) & (saturation >= 100)
    if not paving.any() or not open_green.any():
        return np.zeros(value.shape, np.uint8)
    radius = float(np.median([c['radiusPx'] for c in controls])) if controls else 15.
    width_limit = max(1.4, radius/30*1.6+.25)
    to_paving = cv2.distanceTransform((~paving).astype(np.uint8), cv2.DIST_L2, cv2.DIST_MASK_PRECISE)
    to_green = cv2.distanceTransform((~open_green).astype(np.uint8), cv2.DIST_L2, cv2.DIST_MASK_PRECISE)
    edge = ((masks['black'] > 0) | (masks['dark_green'] > 0)) & (to_paving+to_green <= width_limit+2)
    edge &= (masks['water'] == 0) & (masks['olive'] == 0)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(edge.astype(np.uint8), 8)
    keep = np.zeros(count, bool)
    for i in range(1, count):
        # A tree, pillar or isolated dark fleck is not a long surface border.
        keep[i] = max(stats[i, cv2.CC_STAT_WIDTH], stats[i, cv2.CC_STAT_HEIGHT]) >= max(12., radius*.65)
    return keep[labels].astype(np.uint8)*255


def region_features(mask, kind, image_area, close=3):
    cleaned = mask
    if kind == 'water':
        # Remove long north guides explicitly. Opening before closing would
        # also erase the thin blue strips BETWEEN purple forbidden hatching
        # and lose the actual lake as a geographic landmark.
        cleaned = water_obstacles(mask)
    cleaned = cv2.morphologyEx(cleaned, cv2.MORPH_CLOSE, np.ones((close, close), np.uint8))
    cleaned = cv2.morphologyEx(cleaned, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(cleaned, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    features = []
    for contour in contours:
        area = cv2.contourArea(contour)
        if not max(90, image_area*0.00004) < area < image_area*0.10:
            continue
        perimeter = cv2.arcLength(contour, True)
        polygon = cv2.approxPolyDP(contour, 0.006*perimeter, True).reshape(-1, 2)
        properties = polygon_properties(polygon)
        if properties and properties['aspect'] < 9:
            features.append({'kind': kind, **properties})
    return sorted(features, key=lambda f: -f['area'])


def black_roofs(black, roofs, controls, image_area):
    """Solid black roofs, as forest and park maps print them, for matching.

    A sprint map fills roofs grey; a forest-style map fills them black like
    its paths, fences and lettering. Opening by about 0.6 mm at print scale,
    a fifth of a control circle's radius, keeps only areas too wide for a
    line or a letter. Black outlining a grey roof adds no new landmark.
    """
    radii = [c['radiusPx'] for c in controls if c.get('role', 'control') == 'control']
    size = round(float(np.median(radii))*.2) if radii else round(np.sqrt(image_area)/420)
    size = max(3, size) | 1
    solid = cv2.morphologyEx(black, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size)))
    grey = cv2.morphologyEx(roofs, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    result = []
    for feature in region_features(solid, 'building', image_area):
        outline = np.round(feature['points']).astype(np.int32)
        x, y, w, h = cv2.boundingRect(outline)
        inside = np.zeros((h, w), np.uint8)
        cv2.fillPoly(inside, [outline-[x, y]], 1)
        if np.mean(grey[y:y+h, x:x+w][inside > 0] > 0) < .2:
            result.append(feature)
    return result


def refine_circle(ink, pixel, radius):
    """Fit the stroke centreline at working resolution, after coarse Hough.

    Maximizing ring coverage on the 1800 px search image has a flat optimum
    across the ink's thickness. Its subpixel bias becomes several working
    pixels and can put a control on the wrong side of a facade. Radial stroke
    midpoints give both JPEG variants the same geometric centre, independently
    of terrain or of whether a proposed route happens to be reachable.
    """
    seed = np.asarray(pixel, float)
    centre, original_radius = seed.copy(), radius
    angles = np.linspace(0, 2*np.pi, 360, endpoint=False)
    directions = np.column_stack((np.cos(angles), np.sin(angles)))
    for _ in range(4):
        reach = max(5., radius*.18)
        distances = radius+np.arange(-reach, reach+.25, .5)
        points = centre+directions[:, None, :]*distances[None, :, None]
        samples = cv2.remap(ink, points[:, :, 0].astype(np.float32), points[:, :, 1].astype(np.float32),
                            cv2.INTER_NEAREST, borderMode=cv2.BORDER_CONSTANT)
        midpoints, sectors = [], []
        for index, row in enumerate(samples):
            changes = np.diff(np.r_[False, row > 0, False].astype(int))
            runs = [(a, b) for a, b in zip(np.flatnonzero(changes == 1), np.flatnonzero(changes == -1))
                    if a > 0 and b < len(distances) and (b-a)*.5 <= max(4., radius*.18)]
            if not runs:
                continue
            a, b = min(runs, key=lambda r: abs(np.mean(distances[r[0]:r[1]])-radius))
            midpoints.append(centre+directions[index]*np.mean(distances[a:b]))
            sectors.append(index//90)
        if len(midpoints) < 240 or min(np.bincount(sectors, minlength=4)) < 45:
            return pixel, original_radius
        local = np.asarray(midpoints)-centre
        for _ in range(3):
            fit = np.linalg.lstsq(np.column_stack((2*local, np.ones(len(local)))),
                                  np.sum(local*local, axis=1), rcond=None)[0]
            offset = fit[:2]
            fitted_radius = np.sqrt(max(0., fit[2]+sum(offset**2)))
            residual = np.linalg.norm(local-offset, axis=1)-fitted_radius
            deviation = np.abs(residual-np.median(residual))
            local = local[deviation <= max(.8, 3*np.median(deviation))]
            if len(local) < 200:
                return pixel, original_radius
        centre += offset
        radius = float(fitted_radius)
        if np.linalg.norm(centre-seed) > max(3., original_radius*.18) or abs(radius-original_radius) > max(3., original_radius*.2):
            return pixel, original_radius
    return centre.tolist(), radius


def overprinted_course_ink(image):
    """Course purple printed with the overprint effect, over any ground.

    Overprinted ink multiplies the colour beneath: magenta over yellow open
    land prints red, over green dark violet. Dividing by the local ground,
    the median of a window much wider than the ink lines, recovers the ink
    colour. Only a strongly purple result counts; pale greys over green and
    black lines over anything do not.
    """
    ground = cv2.medianBlur(image, 15)
    ink = cv2.divide(image, ground, scale=255).astype(np.int16)
    b, g, r = ink[..., 0], ink[..., 1], ink[..., 2]
    darker = np.abs(image.astype(np.int16)-ground).sum(axis=2) > 60
    return ((r-g > 60) & (b-g > 40) & (r > 85) & (b > 75) & darker).astype(np.uint8)*255


def detect_controls(purple, image=None):
    """Course circles in the course ink mask.

    Given the map `image`, a second pass also reads ink printed with the
    overprint effect, and judges a ring only where dark lines do not hide it:
    overprinted magenta over a black path prints nearly black. That pass only
    adds circles; ones the plain ink shows stay exactly as found.
    """
    height, width = purple.shape
    scale = min(2., 1800/max(height, width))
    interpolation = cv2.INTER_CUBIC if scale > 1 else cv2.INTER_AREA
    mask = cv2.resize(purple, None, fx=scale, fy=scale, interpolation=interpolation)
    mask = (mask > 75).astype(np.uint8)*255
    candidates = ring_candidates(mask, scale)
    if image is not None:
        small = cv2.resize(image, mask.shape[::-1], interpolation=interpolation)
        mask |= overprinted_course_ink(small)
        occluded = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)[..., 2] < 110
        candidates += ring_candidates(mask, scale, occluded, known=candidates)
    return accept_rings(candidates, purple)


def ring_candidates(mask, scale, occluded=None, known=()):
    smoothed = cv2.GaussianBlur(mask, (3, 3), 0.8)
    distance = cv2.distanceTransform(255-mask, cv2.DIST_L2, 3)
    # A collage's white margin and photo panels do not set the printed
    # circle size. Search a range of observable stroke scales instead of
    # assuming every control occupies a fixed fraction of the whole sheet.
    min_radius = 8
    max_radius = max(min_radius+2, min(120, round(min(mask.shape)*.24)))
    circles = cv2.HoughCircles(smoothed, cv2.HOUGH_GRADIENT, dp=1,
                              minDist=8, param1=80, param2=13,
                              minRadius=min_radius, maxRadius=max_radius)
    if circles is None:
        return []
    candidates = []
    theta = np.linspace(0, 2*np.pi, 120, endpoint=False)
    # Where dark lines may hide the ring, they may also carry it.
    support = mask > 0 if occluded is None else (mask > 0) | occluded
    seen = np.array([c['pixel'] for c in known]).reshape(-1, 2)*scale
    for x, y, radius in circles[0]:
        if x-radius < 0 or y-radius < 0 or x+radius >= mask.shape[1] or y+radius >= mask.shape[0]:
            continue
        if len(seen) and np.min(np.hypot(*(seen-[x, y]).T)) < radius*.8:
            continue
        # Most Hough votes in text and hatching are not rings. Reject poor
        # seeds cheaply before the more precise centre/radius refinement.
        rough = []
        for offset in (-3., 0., 3.):
            xx = np.clip(np.rint(x+(radius+offset)*np.cos(theta)).astype(int), 0, mask.shape[1]-1)
            yy = np.clip(np.rint(y+(radius+offset)*np.sin(theta)).astype(int), 0, mask.shape[0]-1)
            rough.append(distance[yy, xx])
        if np.mean(np.min(rough, axis=0) < 3) < .65:
            continue
        # Refine the Hough radius and centre on the original ink. A one-pixel
        # error can suppress a genuine thin circle at this working scale.
        shifts = np.linspace(-min(4., radius*.15), min(4., radius*.15), 5)
        radius_shifts = np.linspace(-min(3., radius*.15), min(3., radius*.15), 7)
        # Every shifted ring at once, in (x, y, radius) order: the first best wins.
        delta_x, delta_y, delta_r = (grid.ravel() for grid in np.meshgrid(shifts, shifts, radius_shifts, indexing='ij'))
        ring_x = np.rint((x+delta_x)[:, None]+(radius+delta_r)[:, None]*np.cos(theta)).astype(int)
        ring_y = np.rint((y+delta_y)[:, None]+(radius+delta_r)[:, None]*np.sin(theta)).astype(int)
        inside = (ring_x.min(axis=1) >= 0) & (ring_y.min(axis=1) >= 0) & (ring_x.max(axis=1) < mask.shape[1]) & (ring_y.max(axis=1) < mask.shape[0])
        if not inside.any():
            continue
        values = np.where(inside, support[np.clip(ring_y, 0, mask.shape[0]-1), np.clip(ring_x, 0, mask.shape[1]-1)].mean(axis=1), -1.)
        best = int(np.argmax(values))
        x, y, radius = float(x+delta_x[best]), float(y+delta_y[best]), float(radius+delta_r[best])
        if radius < min_radius or x-radius < 2 or y-radius < 2 or x+radius >= mask.shape[1]-2 or y+radius >= mask.shape[0]-2:
            continue
        offsets = np.linspace(-min(2., radius*.08), min(2., radius*.08), 5)
        samples = []
        for delta in offsets:
            xx = np.clip(np.rint(x+(radius+delta)*np.cos(theta)).astype(int), 0, mask.shape[1]-1)
            yy = np.clip(np.rint(y+(radius+delta)*np.sin(theta)).astype(int), 0, mask.shape[0]-1)
            samples.append(distance[yy, xx])
        covered = np.min(samples, axis=0) < max(.8, min(1.2, radius*.045))
        hidden = np.zeros_like(covered)
        if occluded is not None:
            # A dark path printed across the ring hides it; judge the rest.
            ring_x = np.clip(np.rint(x+radius*np.cos(theta)).astype(int), 0, mask.shape[1]-1)
            ring_y = np.clip(np.rint(y+radius*np.sin(theta)).astype(int), 0, mask.shape[0]-1)
            hidden = occluded[ring_y, ring_x] & ~covered
            if hidden.mean() > .15:
                continue
        coverage = float(covered.sum()/max(1, (~hidden).sum()))
        quadrants = [c.sum()/max(1, (~h).sum()) for c, h in zip(np.array_split(covered, 4), np.array_split(hidden, 4))]
        inner_r = max(2, round(radius*.55))
        inner = mask[max(0, round(y)-inner_r):round(y)+inner_r,
                     max(0, round(x)-inner_r):round(x)+inner_r]
        density = np.mean(inner > 0) if inner.size else 1
        ring_samples = []
        for delta in (-1, 0, 1):
            xx = np.rint(x+(radius+delta)*np.cos(theta)).astype(int)
            yy = np.rint(y+(radius+delta)*np.sin(theta)).astype(int)
            ring_samples.append(mask[yy, xx] > 0)
        ring_density = float(np.mean(ring_samples))
        if coverage < .94 or min(quadrants) < .80 or density > .32 or ring_density-density < .25:
            continue
        candidates.append({'pixel': [float(x/scale), float(y/scale)],
                           'radiusPx': float(radius/scale), 'confidence': round(coverage, 3),
                           'ringDensity': round(ring_density, 3),
                           'state': 'candidate', 'labelSource': 'unread', 'label': ''})
    return candidates


def accept_rings(candidates, purple):
    # Nested rings at the finish are ONE checkpoint. Keep the stronger fit.
    accepted = []
    for item in sorted(candidates, key=lambda c: -c['confidence']):
        if any(np.linalg.norm(np.array(item['pixel'])-other['pixel']) < max(item['radiusPx'], other['radiusPx'])*.8
               for other in accepted):
            continue
        accepted.append(item)
    if len(accepted) >= 3:
        # A sheet's printed control circles share a scale; the holes in
        # nearby numerals are usually much smaller. Infer that scale from
        # repeated rings instead of tying it to image or paper dimensions.
        radii = np.asarray([item['radiusPx'] for item in accepted])
        support = np.abs(np.log(radii[:, None]/radii[None, :])) <= np.log(1.18)
        counts = support.sum(axis=1)
        best = max(range(len(radii)), key=lambda i: (counts[i], radii[i]))
        if counts[best] >= 3:
            typical = float(np.median(radii[support[best]]))
            accepted = [item for item in accepted if .68*typical <= item['radiusPx'] <= 1.45*typical]
    for item in accepted:
        item['pixel'], item['radiusPx'] = refine_circle(purple, item['pixel'], item['radiusPx'])
    accepted.sort(key=lambda c: (c['pixel'][1], c['pixel'][0]))
    for index, control in enumerate(accepted):
        control['id'] = f'c{index+1}'
        control['label'] = f'待核{index+1}'
        control['required'] = True
        control['serviceBaseSec'] = 5
        control['servicePerPersonSec'] = 1
    return accepted


def purple_restrictions(purple, controls, annotations=None):
    """Hatching has sustained local density; a circle or course line does not.

    Course-circle interiors are deliberately not erased: forbidden hatching
    inside a control circle must still prohibit passage.
    """
    ink = cv2.bitwise_and(purple, cv2.bitwise_not(annotations if annotations is not None else course_ink_mask(purple, controls)))
    h, w = ink.shape
    radius = max(7, round(max(h, w)*.006))
    density = cv2.boxFilter((ink > 0).astype(np.float32), -1, (2*radius+1, 2*radius+1))
    regions = ((density > .32) & (ink > 0)).astype(np.uint8)*255
    closed = cv2.morphologyEx(regions, cv2.MORPH_CLOSE, np.ones((radius, radius), np.uint8))
    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    result = np.zeros_like(ink)
    for contour in contours:
        area = cv2.contourArea(contour)
        if area > radius*radius*3:
            cv2.drawContours(result, [contour], -1, 255, cv2.FILLED)
    return result


def analyze(image, labeler=None):
    masks = colour_masks(image)
    height, width = image.shape[:2]
    features = region_features(masks['building'], 'building', height*width, close=5)
    features += region_features(masks['water'], 'water', height*width, close=7)
    h, s, v = cv2.split(cv2.cvtColor(image, cv2.COLOR_BGR2HSV))
    pitch_ink = ((h >= 12) & (h <= 34) & (s >= 65) & (s <= 180) & (v > 200)).astype(np.uint8)*255
    for feature in region_features(pitch_ink, 'pitch', height*width, close=5):
        rect = cv2.minAreaRect(np.asarray(feature['points'], np.float32))
        rectangularity = feature['area']/max(1, rect[1][0]*rect[1][1])
        if rectangularity > .72 and feature['area'] > height*width*.0004 and feature['aspect'] < 3.5:
            feature['rectangularity'] = float(rectangularity)
            features.append(feature)
    del h, s, v, pitch_ink  # full-size scratch; later steps peak higher
    controls = detect_controls(masks['course_ink'], image)
    classify_course_symbols(masks['course_ink'], controls)
    features += black_roofs(masks['black'], masks['building'], controls, height*width)
    ocr = labeler(masks['purple'], controls, masks['course_text']) if labeler else None
    annotations = annotation_mask(masks['purple'], controls, masks['course_text'])
    restrictions = purple_restrictions(masks['purple'], controls, annotations)
    recovered, hidden, continued_barriers = restore(image, masks['purple'], annotations, restrictions, masks['black'], controls)
    terrain_masks = colour_masks(recovered)
    for kind in ('purple', 'course_ink', 'course_text'):
        del terrain_masks[kind]  # course ink was separated above
    lower_stripes, lower_area, lower_features = lower_level_hatching(recovered, controls)
    # Only traversal changes. Registration keeps the full building outline,
    # which is what a reference footprint over a passable lower level shows.
    if lower_stripes.any():
        masks['building'][lower_stripes > 0] = 0
        terrain_masks['building'][lower_stripes > 0] = 0
    # Preserve observed obstacle pixels, while restoring the hidden portions
    # of water/roofs/walls rather than turning purple ink into free ground.
    for kind in ('water', 'building', 'dark_green', 'olive', 'black'):
        terrain_masks[kind] |= cv2.bitwise_and(masks[kind], cv2.bitwise_not(hidden))
    terrain_masks['black'] |= continued_barriers
    boundaries = paved_boundaries(image, masks, controls)
    boundaries[continued_barriers > 0] = 0
    for kind in ('dark_green', 'black'):
        terrain_masks[kind][boundaries > 0] = 0
    # Do not fill an entire external contour: it may contain a courtyard,
    # portico, garden, or an opening through a U-shaped building. Roof fill
    # and its black outline remain hard; real white openings remain visible.
    buildings = terrain_masks['building'].copy()
    # Anti-aliasing beside thin grey roofs leaves pale grey road pixels that
    # the loose grey threshold also accepts. A real roof is a filled region,
    # so drop building pixels whose 3x3 neighbourhood is not all grey; white
    # roads and courtyards then stay walkable instead of becoming false walls.
    support = cv2.erode(buildings, np.ones((3, 3), np.uint8))
    buildings = cv2.dilate(support, np.ones((3, 3), np.uint8))
    continued_roofs = continue_roof_boundaries(image, hidden, masks['building'], controls)
    buildings |= continued_roofs
    buildings[lower_stripes > 0] = 0
    # A black roof outline blended with yellow paving is dark brown in the
    # scan, not neutral black. Retain that one-pixel boundary where a solid
    # roof supports it, without filling bright gates or light-grey porticos.
    _, terrain_s, terrain_v = cv2.split(cv2.cvtColor(recovered, cv2.COLOR_BGR2HSV))
    roof_edge = ((terrain_v < 165) & (terrain_s < 150)
                 & (cv2.dilate(buildings, np.ones((3, 3), np.uint8)) > 0) & (lower_stripes == 0))
    buildings[roof_edge] = 255
    water_regions = water_obstacles(terrain_masks['water'])
    hard = cv2.bitwise_or(buildings, water_regions)
    for kind in ('dark_green', 'olive', 'black'):
        hard = cv2.bitwise_or(hard, terrain_masks[kind])
    hard = cv2.bitwise_or(hard, restrictions)
    unknown_ink = cv2.bitwise_and(masks['purple'], cv2.bitwise_not(cv2.bitwise_or(annotations, hidden)))
    hard = cv2.bitwise_or(hard, unknown_ink)
    protected = buildings | water_regions | terrain_masks['dark_green'] | terrain_masks['olive'] | restrictions | unknown_ink
    stairs, treads, stair_features = detect_stairs(recovered, controls, protected)
    hard[treads > 0] = 0
    # Tiny isolated compression flecks are not walls. A 2x2 black pillar on
    # the supplied low-resolution map is already a real obstacle, though.
    count, labels, stats, _ = cv2.connectedComponentsWithStats(hard, 8)
    keep = np.array([False]+[int(stats[i, cv2.CC_STAT_AREA]) >= 4 for i in range(1, count)])
    hard = keep[labels].astype(np.uint8)*255
    costs = np.ones((height, width), np.float32)
    costs[terrain_masks['green'] > 0] = 1.5
    costs[stairs > 0] = 1.7
    costs[hard > 0] = 0
    return {'width': width, 'height': height, 'controls': controls, 'features': features,
            'terrainFeatures': [{'kind': 'stairs', **feature} for feature in stair_features]+lower_features,
            'masks': masks, 'blocked': hard, 'costs': costs,
            'semantics': {'building': buildings > 0, 'water': water_regions > 0,
                          'point_ink': terrain_masks['black'] > 0, 'vegetation': terrain_masks['dark_green'] > 0,
                          'restricted': (restrictions > 0) | (terrain_masks['olive'] > 0),
                          'stairs': stairs > 0,
                          'lower_level': lower_area > 0,
                          'course_ink': hidden > 0, 'uncertain_ink': unknown_ink > 0},
            'statistics': {'buildingCandidates': sum(f['kind']=='building' for f in features),
                           'waterCandidates': sum(f['kind']=='water' for f in features),
                           'controlCandidates': sum(c.get('role') == 'control' for c in controls),
                           'courseSymbolCandidates': len(controls),
                           'forbiddenPixelFraction': round(float(np.mean(hard > 0)), 4),
                           'restoredCourseInkPixels': int(np.count_nonzero(hidden)),
                           'continuedBarrierPixels': int(np.count_nonzero(continued_barriers)),
                           'continuedRoofPixels': int(np.count_nonzero(continued_roofs)),
                           'staircaseCandidates': len(stair_features),
                           'stairTreadPixels': int(np.count_nonzero(treads)),
                           'pavedBoundaryPixels': int(np.count_nonzero(boundaries)),
                           'lowerLevelHatchingPixels': int(np.count_nonzero(lower_stripes)),
                           **({'ocr': ocr} if ocr else {})}, 'terrainSchema': TERRAIN_SCHEMA,
            'warnings': ['自动提取结果待核验：细墙、门洞、桥隧层级及紫色禁区可能存在歧义。',
                         '控制点编号需核对；圈心和到访位置分别保存。']}


def save_analysis(result, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(directory/'blocked.png'), result['blocked'])
    np.savez_compressed(directory/'terrain.npz', costs=result['costs'], **result.get('semantics', {}))
    overlay = np.zeros((result['height'], result['width'], 4), np.uint8)
    overlay[result['blocked'] > 0] = [67, 66, 221, 125]
    cv2.imwrite(str(directory/'terrain.png'), overlay)


def preview(path, output, max_side=1200):
    image = read_image(path)
    scale = min(1, max_side/max(image.shape[:2]))
    cv2.imwrite(str(output), cv2.resize(image, None, fx=scale, fy=scale), [cv2.IMWRITE_JPEG_QUALITY, 82])
