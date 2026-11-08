"""Align printed path networks when isolated buildings and water are scarce.

A park's outline proposes scale/rotation; independently held-out road ways
test the alignment. No control label, photograph, or place-specific pixel is
used as a geographic anchor.
"""
import math

import cv2
import numpy as np
from scipy.optimize import least_squares
from scipy.spatial import cKDTree

from .geometry import Registration, affine_points
from .reference import polygon_properties
from .registration import principal_angle


def map_footprint(image):
    h, s, v = cv2.split(cv2.cvtColor(image, cv2.COLOR_BGR2HSV))
    colour = ((h >= 10) & (h <= 90) & (s >= 45) & (v > 100)).astype(np.uint8)*255
    unit = max(1., max(image.shape[:2])/1400)
    colour = cv2.morphologyEx(colour, cv2.MORPH_OPEN, np.ones((max(3, round(5*unit)),)*2, np.uint8))
    colour = cv2.morphologyEx(colour, cv2.MORPH_CLOSE, np.ones((max(5, round(15*unit)),)*2, np.uint8))
    contours, _ = cv2.findContours(colour, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    contour = max(contours, key=cv2.contourArea)
    if cv2.contourArea(contour) < image.shape[0]*image.shape[1]*.10:
        return None
    return cv2.convexHull(contour).reshape(-1, 2).astype(float)


def registration_footprint(image):
    """Locate map ink when a forest sheet has little continuous colour.

    Plain colour footprints remain unchanged when they describe the map.
    Edge banners without extended map structures cannot replace the map;
    on sparse sheets, connected linework supplies an exploratory footprint.
    It only bounds registration searches: measured anchors and held-out
    checks still determine the calibrated domain. Recognition and terrain
    masks continue to use their existing conservative colour footprint.
    """
    height, width = image.shape[:2]
    h, s, v = cv2.split(cv2.cvtColor(image, cv2.COLOR_BGR2HSV))
    unit = max(1., max(height, width)/1400)
    kernel = np.ones((max(3, round(5*unit)),)*2, np.uint8)
    closing = np.ones((max(5, round(15*unit)),)*2, np.uint8)
    colour = ((h >= 10) & (h <= 90) & (s >= 45) & (v > 100)).astype(np.uint8)
    colour = cv2.morphologyEx(cv2.morphologyEx(colour, cv2.MORPH_OPEN, kernel), cv2.MORPH_CLOSE, closing)
    contours, _ = cv2.findContours(colour, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    # Keep genuine narrow maps with extended two-dimensional linework.
    structures = (((s < 100) & (v < 165)) | ((h > 78) & (h < 111) & (s > 45) & (v > 100))).astype(np.uint8)
    bands, candidates = [], []
    for contour in sorted(contours, key=cv2.contourArea, reverse=True):
        x, y, w, hh = cv2.boundingRect(contour)
        horizontal = w >= .75*width and w > 5*hh and hh < .25*height and (y < .06*height or y+hh > .94*height)
        vertical = hh >= .75*height and hh > 5*w and w < .25*width and (x < .06*width or x+w > .94*width)
        band = horizontal or vertical
        if band:
            margin = max(2, round(3*unit))
            crop = structures[y+margin:y+hh-margin, x+margin:x+w-margin]
            if crop.size:
                _, _, stats, _ = cv2.connectedComponentsWithStats(crop, 8)
                for row in stats[1:]:
                    major, minor = (row[2], row[3]) if horizontal else (row[3], row[2])
                    if major > .35*(w if horizontal else hh) and minor > .12*(hh if horizontal else w) and row[4] < .65*w*hh:
                        band = False; break
        if band:
            bands.append((x, y, w, hh, cv2.contourArea(contour)))
        elif cv2.contourArea(contour) >= height*width*.1:
            candidates.append(contour)
    if candidates and (not bands or cv2.contourArea(candidates[0]) >= max(b[-1] for b in bands)):
        return cv2.convexHull(candidates[0]).reshape(-1, 2).astype(float)
    # Terrain colours and neutral linework locate the map. Saturated
    # dark-blue branding and purple page graphics are not map boundaries.
    ink = (((h >= 5) & (h <= 115) & (s >= 45) & (v > 100)) | ((s < 100) & (v < 165))).astype(np.uint8)
    for x, y, w, hh, _ in bands:
        pad = max(3, round(5*unit))
        top, bottom, left, right = max(0, y-pad), min(height, y+hh+pad), max(0, x-pad), min(width, x+w+pad)
        ink[top:bottom, left:right] = structures[top:bottom, left:right]
    margin = max(3, round(max(height, width)*.012))
    ink[:margin] = ink[-margin:] = 0
    ink[:, :margin] = ink[:, -margin:] = 0
    size = max(9, round(max(height, width)*.015)) | 1
    dense = (cv2.blur(ink.astype(np.float32), (size, size)) >= .035).astype(np.uint8)
    outlines, _ = cv2.findContours(dense, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if outlines:
        outline = max(outlines, key=cv2.contourArea)
        if cv2.contourArea(outline) >= max(height*width*.1, cv2.contourArea(candidates[0]) if candidates else 0):
            return cv2.convexHull(outline).reshape(-1, 2).astype(float)
    return cv2.convexHull(candidates[0]).reshape(-1, 2).astype(float) if candidates else None



def thin(mask):
    """Zhang-Suen thinning, with the two deletion rules in small lookup tables."""
    codes = np.arange(256, dtype=np.uint16)
    p = [(codes >> i) & 1 for i in range(8)]
    neighbours = sum(p)
    transitions = sum((p[i] == 0) & (p[(i+1) % 8] == 1) for i in range(8))
    common = (neighbours >= 2) & (neighbours <= 6) & (transitions == 1)
    tables = [common & (p[0]*p[2]*p[4] == 0) & (p[2]*p[4]*p[6] == 0),
              common & (p[0]*p[2]*p[6] == 0) & (p[0]*p[4]*p[6] == 0)]
    kernel = np.array([[128, 1, 2], [64, 0, 4], [32, 16, 8]], np.uint16)
    result = (mask > 0).astype(np.uint8)
    for _ in range(120):
        changed = False
        for table in tables:
            neighbourhood = cv2.filter2D(result, cv2.CV_16U, kernel, borderType=cv2.BORDER_CONSTANT)
            remove = (result > 0) & table[neighbourhood]
            changed |= bool(remove.any())
            result[remove] = 0
        if not changed:
            break
    return result


def printed_paths(image, max_side=1400, *, diagnostics=None, crop_margins=True, exposure_image=None):
    if exposure_image is not None and exposure_image.shape != image.shape:
        raise ValueError('道路配准的亮度参照与工作图尺寸不一致')
    ratio = min(1., max_side/max(image.shape[:2]))
    h, s, v = cv2.split(cv2.cvtColor(image, cv2.COLOR_BGR2HSV))
    shape = tuple(round(size*ratio) for size in image.shape[1::-1])
    footprint = registration_footprint(cv2.resize(image, shape, interpolation=cv2.INTER_AREA))
    if footprint is None:
        if crop_margins:
            # A wide blank border is not evidence that the printed map is a
            # small legend. Retry on the observed page content, retaining its
            # exact offset in the original working-image coordinate system.
            yy, xx = np.nonzero(np.any(image < 245, axis=2))
            if len(xx):
                left, top = max(0, int(xx.min())-3), max(0, int(yy.min())-3)
                right, bottom = min(image.shape[1], int(xx.max())+4), min(image.shape[0], int(yy.max())+4)
                if (right-left)*(bottom-top) < image.shape[0]*image.shape[1]*.8:
                    pixels, outline, scale = printed_paths(image[top:bottom, left:right], max_side,
                        diagnostics=diagnostics, crop_margins=False,
                        exposure_image=None if exposure_image is None else exposure_image[top:bottom, left:right])
                    offset = np.array([left, top])*scale
                    if diagnostics is not None:
                        diagnostics['analysisPixelBox'] = [left, top, right, bottom]
                    return pixels+offset, outline+offset, scale
        raise ValueError('地图轮廓不足以提出道路配准范围')
    support = np.zeros(shape[::-1], np.uint8)
    cv2.fillPoly(support, [np.rint(footprint).astype(np.int32)], 1)
    inside = support > 0
    support = cv2.dilate(support, np.ones((15, 15), np.uint8))
    pale = (h >= 5) & (h <= 25) & (s >= 12) & (s <= 65)

    def recover_exposure(white_value, source):
        nonlocal pale
        floor = max(80., min(215., 215*white_value/255-.5))
        # Dark 8-bit channels carry enough rounding uncertainty to move
        # low-saturation hue/chroma by two codes even at mild exposure loss.
        tolerance = 2
        pale = ((h >= 5-tolerance) & (h <= 25+tolerance)
                & (s >= 12-tolerance) & (s <= 65+tolerance))
        if diagnostics is not None:
            diagnostics.update(neutralWhiteValue=round(white_value, 2), pathColourTolerance=tolerance,
                               pathValueFloor=round(floor, 2), exposureReference=source)
        return floor

    def centreline(value_floor):
        paving = (pale & (v > value_floor)).astype(np.uint8)*255
        paving = cv2.morphologyEx(paving, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
        if ratio < 1:
            paving = cv2.resize(paving, None, fx=ratio, fy=ratio, interpolation=cv2.INTER_AREA)
        paving = cv2.morphologyEx((paving > 60).astype(np.uint8), cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
        skeleton = thin(paving & support)
        _, labels, stats, _ = cv2.connectedComponentsWithStats(skeleton, 8)
        keep = np.array([False]+[row[cv2.CC_STAT_AREA] >= 12 for row in stats[1:]])
        yy, xx = np.nonzero(keep[labels])
        return np.column_stack((xx, yy)).astype(float)

    # Paper-white clearings inside the map give an exposure reference that
    # excludes blank outer margins. Read them from the unmasked original,
    # since erasing photographic panels creates artificial white pixels.
    reference = cv2.resize(image if exposure_image is None else exposure_image, shape, interpolation=cv2.INTER_AREA)
    reference = cv2.cvtColor(reference, cv2.COLOR_BGR2HSV)
    neutral_inside = inside & (reference[..., 1] < 12) & (reference[..., 2] > 100)
    floor = 215.
    if np.count_nonzero(neutral_inside) >= 128 and pale.any():
        white = float(np.percentile(reference[..., 2][neutral_inside], 95))
        light_paving = float(np.percentile(v[pale], 90))
        # A dark neutral roof cannot stand in for white paper when the
        # observed paving is brighter than it.
        if white < 250 and white >= light_paving-2:
            floor = recover_exposure(white, 'unmasked_map')
    pixels = centreline(floor)
    if len(pixels) < 200 and floor == 215 and pale.any():
        # Maps without white clearings can still have observable dim paper
        # around their edge. This retry is only used when paths are missing.
        neutral = (s < 12) & (v > 100) & (v < 250)
        if np.count_nonzero(neutral) >= 128:
            white_value = float(np.percentile(v[neutral], 95))
            value_floor = recover_exposure(white_value, 'neutral_page')
        else:
            value_floor = max(80., min(215., float(np.percentile(v[pale], 90))*.88))
        if value_floor < 215:
            pixels = centreline(value_floor)
            if diagnostics is not None:
                diagnostics['pathValueFloor'] = round(value_floor, 2)
    if len(pixels) < 200:
        # Some mapping software prints paving as a deeper tan (S about 80).
        # Yellow open land stays excluded by its higher saturation; this is
        # only tried when the usual beige paving is missing.
        pale = (h >= 5) & (h <= 25) & (s >= 12) & (s <= 100)
        pixels = centreline(floor)
        if diagnostics is not None and len(pixels) >= 200:
            diagnostics['pathSaturationMax'] = 100
    if len(pixels) < 200:
        raise ValueError('印刷道路不足以进行网络配准')
    return pixels, footprint, ratio


def sample_line(points, step=8.):
    points = np.asarray(points, float)
    result = []
    for a, b in zip(points[:-1], points[1:]):
        length = np.linalg.norm(b-a)
        count = max(1, math.ceil(length/step))
        result.extend(a+(b-a)*np.arange(count)[:, None]/count)
    return np.array([*result, points[-1]])


def matrix_of(parameters):
    angle, log_scale, x, y = parameters
    c, s = math.cos(angle)*math.exp(log_scale), math.sin(angle)*math.exp(log_scale)
    return np.array([[c, s, x], [s, -c, y]])


def parameters_of(matrix):
    return np.array([math.atan2(matrix[1, 0], matrix[0, 0]),
                     math.log(math.hypot(matrix[0, 0], matrix[1, 0])), *matrix[:, 2]])


def refine_matched_roads(matrix, pixels, tree, roads):
    """Keep absent/changed reference ways from pulling an established fit.

    The initial park/network search establishes the position and direction.
    Refine only with whole training ways whose majority follows printed
    paths, keeping their full lengths and equal per-way weight. Held-out
    ways never select the training set or participate in this refinement.
    """
    initial = parameters_of(matrix)
    scale = math.exp(initial[1])
    matched = []
    for key, world in roads:
        projected = (world-matrix[:, 2]) @ np.linalg.inv(matrix[:, :2]).T
        distances, _ = tree.query(projected)
        if np.mean(distances*scale <= 12) >= .6:
            matched.append((key, world))
    if len(matched) < 8 or len(matched) == len(roads):
        return matrix, None
    world = np.concatenate([p for _, p in matched])
    all_world = np.concatenate([p for _, p in roads])
    area = lambda points: cv2.contourArea(cv2.convexHull(points.astype(np.float32)))
    if area(world) < area(all_world)*.5:
        return matrix, None  # a small surviving corner cannot anchor the map
    weights = np.concatenate([np.full(len(p), math.sqrt(20/len(p))) for _, p in matched])

    def residual(parameters):
        candidate = matrix_of(parameters)
        projected = (world-candidate[:, 2]) @ np.linalg.inv(candidate[:, :2]).T
        _, near = tree.query(projected)
        return ((affine_points(candidate, pixels[near])-world)*weights[:, None]).ravel()

    before = residual(initial)
    fitted = least_squares(residual, initial, loss='soft_l1', f_scale=4., max_nfev=100,
                           bounds=(initial-[.1, .1, 40, 40], initial+[.1, .1, 40, 40]))
    initial_cost = float(np.sum(16*(np.sqrt(1+(before/4)**2)-1)))
    if fitted.cost >= initial_cost*(1-1e-5):
        return matrix, None
    matched_ids = {key for key, _ in matched}
    return matrix_of(fitted.x), {
        'fitWays': len(roads), 'retainedWays': len(matched),
        'excludedWayIds': [key for key, _ in roads if key not in matched_ids],
        'scope': '仅用拟合道路筛选并细化；未使用留出道路或目标位置。',
    }


def boundary_path_matches(matrix, pixels, tree, observed, roads, trusted_ids=()):
    """Observed boundary pixels with mutual matches to training polylines.

    Sampling reference ways every few metres can miss a printed curve's
    extreme point. Project exterior image pixels to the original training
    segments, and require a reverse match to the same raster neighbourhood.
    An adjacent pixel may tie within half a pixel of nearest distance; this
    avoids an arbitrary staircase-pixel choice shrinking the hull. Only new
    convex-boundary vertices are retained. This
    adds measured support, not fitted image corners or validation roads.
    A partly visible training way must have a continuous matched stretch;
    isolated crossings are not enough evidence for its boundary.
    """
    if len(observed) < 3:
        return []
    trusted = [(key, line) for key, line in roads if key in trusted_ids]
    partial = [(key, line) for key, line in roads if key not in trusted_ids]
    if trusted and partial:
        # An unsupported partial way must not steal a nearest-match tie
        # from a well-supported way and shrink existing boundary evidence.
        groups = [boundary_path_matches(matrix, pixels, tree, observed, trusted, trusted_ids),
                  boundary_path_matches(matrix, pixels, tree, observed, partial)]
        unique = {}
        for group in groups:
            for record in group:
                key = tuple(record[0])
                if key not in unique or record[3] < unique[key][3]:
                    unique[key] = record
        records = list(unique.values())
        if not records:
            return []
        combined = np.vstack((observed, [record[0] for record in records]))
        lattice = np.rint(combined-pixels[0]).astype(np.float32)
        boundary = cv2.convexHull(lattice, returnPoints=False).ravel()
        return [records[i-len(observed)] for i in boundary if i >= len(observed)]
    # These points all lie on the same analysis-pixel lattice. Work in that
    # integer frame for hull predicates: a common fractional crop offset
    # must not turn a straight boundary into tiny floating-point corners.
    local_pixels = np.rint(pixels-pixels[0]).astype(np.float32)
    local_observed = np.rint(np.asarray(observed)-pixels[0]).astype(np.float32)
    hull = cv2.convexHull(local_observed)
    outside = np.array([cv2.pointPolygonTest(hull, tuple(p), False) < 0 for p in local_pixels])
    if not outside.any():
        return []
    candidates = np.arange(len(pixels))
    world = affine_points(matrix, pixels[candidates])
    best = np.full(len(candidates), np.inf)
    nearest_world = np.zeros_like(world)
    reference_ids = np.empty(len(candidates), object)
    along_way = np.zeros(len(candidates))
    # Only pixels within 12 m of some segment can become eligible, and a
    # segment farther than that from a pixel can never be its nearest
    # one then; each segment is therefore measured against the pixels
    # within its reach only, in the same order as before.
    segments = []
    for key, line in roads:
        line = np.asarray(line, float)
        start = 0.
        for a, b in zip(line[:-1], line[1:]):
            delta = b-a; squared = float(delta @ delta)
            if squared <= 1e-12:
                continue
            length = math.sqrt(squared)
            segments.append((key, a, delta, squared, length, start))
            start += length
    # Balls every 12 m along a segment, of radius sqrt(6**2+12**2) m, cover
    # all points within 12 m of it.
    centres, owners = [], []
    for index, (_, a, delta, _, length, _) in enumerate(segments):
        count = max(1, math.ceil(length/12))
        centres.append(a+delta*((np.arange(count)+.5)/count)[:, None])
        owners.append(np.full(count, index))
    reach = [[] for _ in segments]
    if segments:
        balls = cKDTree(world).query_ball_point(np.vstack(centres), math.hypot(6, 12)+1e-6)
        for owner, ball in zip(np.concatenate(owners), balls):
            reach[owner].append(ball)
    for (key, a, delta, squared, length, start), parts in zip(segments, reach):
        near = np.unique(np.concatenate([np.asarray(part, int) for part in parts])) if parts else []
        if not len(near):
            continue
        points = world[near]
        fraction = np.clip((points-a) @ delta/squared, 0, 1)
        projected = a+fraction[:, None]*delta
        distances = np.sum((points-projected)**2, axis=1)
        take = distances < best[near]
        chosen = near[take]
        best[chosen], nearest_world[chosen], reference_ids[chosen] = distances[take], projected[take], key
        along_way[chosen] = start+fraction[take]*length
    eligible = np.flatnonzero(best <= 12**2)
    if not len(eligible):
        return []
    projected = (nearest_world[eligible]-matrix[:, 2]) @ np.linalg.inv(matrix[:, :2]).T
    distance, nearest = tree.query(projected)
    own_pixels = pixels[candidates[eligible]]
    own_distance = np.linalg.norm(projected-own_pixels, axis=1)
    neighbours = np.linalg.norm(pixels[nearest]-own_pixels, axis=1) <= math.sqrt(2)+1e-6
    matched = eligible[neighbours & (own_distance <= distance+.5)]
    if not len(matched):
        return []
    supported = np.zeros(len(candidates), bool)
    gap = max(4., 3*math.sqrt(abs(np.linalg.det(matrix[:, :2]))))
    for key in set(reference_ids[matched]):
        own = matched[reference_ids[matched] == key]
        if key in trusted_ids:
            supported[own] = True
            continue
        ordered = own[np.argsort(along_way[own])]
        for run in np.split(ordered, np.flatnonzero(np.diff(along_way[ordered]) > gap)+1):
            if len(run) >= 4 and along_way[run[-1]]-along_way[run[0]] >= 16:
                supported[run] = True
    matched = matched[outside[matched] & supported[matched]]
    if not len(matched):
        return []
    combined = np.vstack((local_observed, local_pixels[candidates[matched]]))
    boundary = cv2.convexHull(combined.astype(np.float32), returnPoints=False).ravel()
    keep = sorted(boundary[boundary >= len(observed)]-len(observed))
    return [(pixels[candidates[matched[i]]], nearest_world[matched[i]], reference_ids[matched[i]],
             math.sqrt(best[matched[i]])) for i in keep]


PARK_PATHS = ('footway', 'path', 'pedestrian', 'cycleway', 'steps', 'service')
# A sheet found by global search may be urban: any printed way counts, except
# ways a map never draws as paths on the ground.
NOT_PATHS = ('motorway', 'motorway_link', 'trunk', 'trunk_link', 'proposed', 'construction', 'platform',
             'elevator', 'corridor', 'bus_stop', 'raceway')
# On forest-style sheets paving can show only the surrounding streets;
# small footpaths and private service drives need not be drawn as paving.
STREET_TYPES = ('residential', 'unclassified', 'tertiary', 'secondary', 'primary', 'living_street', 'pedestrian')


def _ways_within(reference, hull, types=None, excluded=()):
    roads = []
    for feature in sorted(reference, key=lambda f: f['id']):
        highway = feature.get('tags', {}).get('highway')
        if feature['kind'] != 'road' or (types is not None and highway not in types) or highway in excluded:
            continue
        points = sample_line(feature['points'])
        inside = np.array([cv2.pointPolygonTest(hull, tuple(p), True) >= -15 for p in points])
        if inside.sum() >= 4:
            roads.append((feature['id'], points[inside]))
    return roads


def _holdout(roads, hull):
    """Hold out whole interior ways, chosen from reference geometry only.

    Holding out the only road along one map edge would shrink calibration
    for an arbitrary split. This partition is fixed before looking at any
    road matching residual or at a control's location.
    """
    interior = [key for key, points in roads if all(cv2.pointPolygonTest(hull, tuple(p), True) > 25 for p in points)]
    holdout = set(interior[1::3])
    return ([(key, points) for key, points in roads if key not in holdout],
            [(key, points) for key, points in roads if key in holdout])


def _fit_roads(initial, pixels, tree, fit):
    """Fit a similarity to whole training ways; return its score and matrix."""
    world = np.concatenate([p for _, p in fit])
    weights = np.concatenate([np.full(len(p), math.sqrt(20/len(p))) for _, p in fit])

    def residual(parameters):
        matrix = matrix_of(parameters)
        projected = (world-matrix[:, 2]) @ np.linalg.inv(matrix[:, :2]).T
        _, near = tree.query(projected)
        error = affine_points(matrix, pixels[near])-world
        return (error*weights[:, None]).ravel()

    fitted = least_squares(residual, initial, loss='soft_l1', f_scale=4., max_nfev=100,
                           bounds=(initial-[.25, .2, 90, 90], initial+[.25, .2, 90, 90])).x
    matrix = matrix_of(fitted)
    projected = (world-matrix[:, 2]) @ np.linalg.inv(matrix[:, :2]).T
    distance, _ = tree.query(projected)
    return float(np.mean(np.minimum(distance*math.exp(fitted[1]), 25))), matrix


def path_directions(pixels, tree):
    """Unit direction of the printed centre line at each skeleton pixel."""
    _, near = tree.query(pixels, k=min(9, len(pixels)))
    offsets = pixels[near]-pixels[near].mean(axis=1, keepdims=True)
    xx, xy, yy = (np.sum(offsets[..., 0]**2, axis=1), np.sum(offsets[..., 0]*offsets[..., 1], axis=1),
                  np.sum(offsets[..., 1]**2, axis=1))
    angle = .5*np.arctan2(2*xy, xx-yy)
    return np.column_stack((np.cos(angle), np.sin(angle)))


def oriented_support(matrix, pixels, tree, directions, roads, tolerance=8., degrees=25.):
    """Share of way samples lying on a printed path running the same way.

    In a park with paths every few tens of metres almost any pose puts a
    way within 12 m of some printed path; following its direction as well
    is what a wrong pose rarely achieves.
    """
    inverse = np.linalg.inv(matrix[:, :2])
    scale = math.sqrt(abs(np.linalg.det(matrix[:, :2])))
    hits = total = 0
    for _, world in roads:
        if len(world) < 2:
            continue
        projected = (world-matrix[:, 2]) @ inverse.T
        distance, near = tree.query(projected)
        heading = np.gradient(projected, axis=0)
        heading /= np.maximum(np.linalg.norm(heading, axis=1)[:, None], 1e-9)
        aligned = np.abs(np.sum(directions[near]*heading, axis=1)) >= math.cos(math.radians(degrees))
        hits += int(np.sum((distance*scale <= tolerance) & aligned))
        total += len(world)
    return hits/max(1, total)


def road_network_register(image, reference, *, exposure_image=None, seeds=None, paths=None, streets_only=False):
    """Align printed paths to the place's ways.

    Without `seeds`, a park outline proposes scale and rotation. `seeds` are
    coarse similarity poses (working pixels to local metres) from the
    global search; each is fitted to the ways within its own projected
    sheet outline instead. `paths` reuses an earlier `printed_paths` result
    of the same image as (pixels, footprint, ratio, diagnostics).
    `streets_only` is an additional fit of observed paving to major streets,
    not to every minor path in the reference. It needs more independently
    checked ways, including boundary ways, and may not omit failed checks.
    """
    if streets_only and not seeds:
        raise ValueError('铺装街道核查需要已搜索的位置')
    if paths is None:
        extraction = {}
        pixels, footprint, ratio = printed_paths(image, diagnostics=extraction, exposure_image=exposure_image)
    else:
        pixels, footprint, ratio, extraction = paths
        extraction = dict(extraction)
    tree = cKDTree(pixels)
    options = []
    directions = path_directions(pixels, tree) if seeds else None
    for seed in seeds or []:
        # Analysis pixels are working pixels times `ratio`.
        seed = np.column_stack((np.asarray(seed, float)[:, :2]/ratio, np.asarray(seed, float)[:, 2]))
        if np.linalg.det(seed[:, :2]) > 0:
            continue  # a sheet is never mirrored
        hull = affine_points(seed, footprint).astype(np.float32)
        roads = _ways_within(reference, hull, types=STREET_TYPES if streets_only else None, excluded=NOT_PATHS)
        if len(roads) < (9 if streets_only else 8):
            continue
        if streets_only:
            # The useful streets may surround the sheet. Partition whole
            # ways before looking at image residuals; held-out boundary
            # ways validate the pose but never expand calibrated support.
            validation = roads[1::3]
            held = {key for key, _ in validation}
            fit = [(key, points) for key, points in roads if key not in held]
        else:
            fit, validation = _holdout(roads, hull)
        if len(validation) < (3 if streets_only else 2):
            continue
        score, matrix = _fit_roads(parameters_of(seed), pixels, tree, fit)
        # Rank searched poses by direction-aware support of training ways.
        support = oriented_support(matrix, pixels, tree, directions, fit)
        options.append((1-support, score, matrix, fit, validation, None))
    if seeds is not None and not options:
        raise ValueError('全局搜索的候选位置缺少足够的道路可核对')
    source_shape = polygon_properties(footprint)
    source_outline = sample_line(np.vstack((footprint, footprint[0])), 12.)
    parks = [] if seeds is not None else [
        f for f in reference if f['kind'] == 'green' and f.get('tags', {}).get('leisure') in ('park', 'recreation_ground')
        and 10000 < f['area'] < 3_000_000 and max(np.ptp(f['points'], axis=0)) < 3500]
    for park in parks:
        hull = cv2.convexHull(np.asarray(park['points'], np.float32)).reshape(-1, 2)
        target_shape = polygon_properties(hull)
        scale = math.sqrt(target_shape['area']/source_shape['area'])
        if not 150 < np.ptp(footprint, axis=0).max()*scale < 4200:
            continue
        outline = sample_line(np.vstack((hull, hull[0])), 8.)
        outline_tree = cKDTree(outline)
        roads = _ways_within(reference, hull, types=PARK_PATHS)
        if len(roads) < 8:
            continue
        fit, validation = _holdout(roads, hull)
        if len(validation) < 2:
            continue
        a = principal_angle(footprint*np.array([1, -1]))
        b = principal_angle(hull)
        for turn in (0, math.pi/2, math.pi, 3*math.pi/2):
            angle = b-a+turn
            linear = matrix_of([angle, math.log(scale), 0, 0])[:, :2]
            shift = np.array(target_shape['center'])-linear @ source_shape['center']
            initial = np.array([angle, math.log(scale), *shift])

            def outline_residual(parameters):
                transformed = affine_points(matrix_of(parameters), source_outline)
                _, near = outline_tree.query(transformed)
                return (transformed-outline[near]).ravel()

            shaped = least_squares(outline_residual, initial, loss='soft_l1', f_scale=8., max_nfev=60,
                                   bounds=(initial-[.35, .3, 150, 150], initial+[.35, .3, 150, 150])).x
            score, matrix = _fit_roads(shaped, pixels, tree, fit)
            options.append((score, score, matrix, fit, validation, park['id']))
    if not options:
        raise ValueError('场地缺少可核对的公园轮廓与道路网络')
    options.sort(key=lambda candidate: candidate[0])
    rank, score, matrix, roads, validation, park_id = options[0]
    # Repeated rectangular paths can support two quite different positions.
    # Do not resolve that ambiguity using arbitrary candidate iteration order.
    for alternative_rank, _, alternative, *_ in options[1:]:
        if alternative_rank > (rank+max(1., rank*.2) if seeds is None else rank+.1):
            break
        disagreement = np.linalg.norm(affine_points(matrix, footprint)-affine_points(alternative, footprint), axis=1)
        if np.median(disagreement) > 20:
            raise ValueError('存在多个相似道路布局，请补同名点确定方向或位置')
    initial_matrix = matrix
    matrix, refinement = refine_matched_roads(matrix, pixels, tree, roads)
    scale = math.sqrt(abs(np.linalg.det(matrix[:, :2])))
    observations, checks, omitted_ways, validation_ways = [], [], [], []
    for purpose, groups in (('fit', roads), ('validation', validation)):
        for key, world in groups:
            projected = (world-matrix[:, 2]) @ np.linalg.inv(matrix[:, :2]).T
            distance, nearest = tree.query(projected)
            errors = distance*scale
            good = errors <= 12
            if purpose == 'validation':
                validation_ways.append({
                    'referenceId': key, 'matched': bool(good.mean() >= .6), 'sampleCount': len(errors[::2]),
                    'medianNearestPathM': round(float(np.median(errors[::2])), 2),
                    'p95NearestPathM': round(float(np.percentile(errors[::2], 95)), 2),
                    'maxNearestPathM': round(float(np.max(errors[::2])), 2),
                })
            if good.mean() < .6:
                if purpose == 'validation':
                    omitted_ways.append(key)
                continue
            destination = observations if purpose == 'fit' else checks
            take = good if purpose == 'fit' else np.ones(len(world), bool)
            for p, local, error in zip(pixels[nearest][take][::2]/ratio, world[take][::2], errors[take][::2]):
                destination.append({'pixel': p.tolist(), 'local': local.tolist(), 'purpose': purpose,
                                    'kind': 'road_centerline', 'referenceId': key, 'errorM': round(float(error), 2)})
    if refinement and len(observations) >= 3:
        # Refinement can slide a correspondence along a path, arbitrarily
        # shrinking its measured boundary. Keep earlier observed training
        # matches outside that hull when the new transform still agrees
        # within the SAME matching tolerance. No image corner, control, or
        # held-out road is added as boundary evidence.
        hull = cv2.convexHull(np.array([p['pixel'] for p in observations], np.float32))
        initial_scale = math.sqrt(abs(np.linalg.det(initial_matrix[:, :2])))
        retained = 0
        for key, world in roads:
            projected = (world-initial_matrix[:, 2]) @ np.linalg.inv(initial_matrix[:, :2]).T
            distances, nearest = tree.query(projected)
            good = distances*initial_scale <= 12
            if good.mean() < .6:
                continue
            for pixel, local in zip(pixels[nearest][good][::2]/ratio, world[good][::2]):
                error = float(np.linalg.norm(affine_points(matrix, [pixel*ratio])[0]-local))
                if error > 12 or cv2.pointPolygonTest(hull, tuple(pixel), False) >= 0:
                    continue
                observations.append({'pixel': pixel.tolist(), 'local': local.tolist(), 'purpose': 'fit',
                                     'kind': 'road_centerline', 'referenceId': key, 'errorM': round(error, 2),
                                     'source': 'initial_network_correspondence'})
                retained += 1
        refinement['retainedBoundaryPoints'] = retained
    way_errors = [p['errorM'] for p in checks]
    # A searched pose is judged against other poses by the common support
    # check; its own gate need not assume that most of the place's ways are
    # printed as paving (forest-style paths are not), only that enough of
    # them, and held-out ones, are.
    fitted_ways = len({p['referenceId'] for p in observations})
    checked_ways = len({p['referenceId'] for p in checks})
    # A searched pose on a small sheet may have one matched held-out way:
    # it is marked thin, and the common support check demands a wide margin.
    thin = not streets_only and seeds is not None and checked_ways == 1
    if streets_only:
        if (fitted_ways < 5 or checked_ways < 3 or omitted_ways or not way_errors
                or np.median(way_errors) > 8 or np.percentile(way_errors, 95) > 12):
            raise ValueError('铺装街道的完整留出核查未通过')
        observed = np.asarray([p['pixel'] for p in observations], np.float32)*ratio
        if cv2.contourArea(cv2.convexHull(observed)) < .25*cv2.contourArea(footprint.astype(np.float32)):
            raise ValueError('铺装街道的测量范围不足')
    elif (score > 9 if seeds is None else fitted_ways < 8) or len(observations) < 16 or (checked_ways < 2 and not thin) or not way_errors or np.percentile(way_errors, 95) > 18:
        raise ValueError('道路匹配的独立核查证据不足，需补充同名点')
    # Boundary ways are often only partly visible. They may add spatial
    # support after a stable fit, but cannot pull that fit or use held-out
    # ways; the helper requires continuous, mutual image/road matches.
    training_ids = {key for key, _ in roads}
    trusted_ids = {p['referenceId'] for p in observations}
    training = [(f['id'], f['points']) for f in sorted(reference, key=lambda feature: feature['id'])
                if f['kind'] == 'road' and f['id'] in training_ids]
    boundary_matches = boundary_path_matches(matrix, pixels, tree,
        np.array([p['pixel'] for p in observations])*ratio, training, trusted_ids)
    for pixel, local, key, error in boundary_matches:
        observations.append({'pixel': (pixel/ratio).tolist(), 'local': local.tolist(), 'purpose': 'fit',
            'kind': 'road_centerline', 'referenceId': key, 'errorM': round(error, 2), 'source': 'mutual_road_boundary'})
    unique = {tuple(p['pixel']): p for p in observations}
    observations = list(unique.values())
    matrix[:, :2] *= ratio
    source = np.array([p['pixel'] for p in observations])
    # A similarity has no local correction. Only the observed convex boundary
    # is needed to represent it; hundreds of collinear samples along straight
    # roads would create numerically degenerate, unnecessary mesh triangles.
    # Scaling the integer analysis pixels to the original image can create
    # false corners after float32 rounding. Select the hull in the original
    # analysis lattice, then keep the exact observed source coordinates.
    lattice = np.rint((source-source[0])*ratio).astype(np.float32)
    boundary = cv2.convexHull(lattice, returnPoints=False).ravel()
    reg = Registration(matrix, source[boundary], affine_points(matrix, source[boundary]), regularization=0)
    _, inside = reg.forward([p['pixel'] for p in checks])
    for check, valid in zip(checks, inside):
        check['insideHull'] = bool(valid)
    method = 'global_search_street_network' if streets_only else ('park_outline_road_network' if park_id else 'global_search_road_network')
    quality = {'state': 'needs_review', 'method': method,
               **({'parkReferenceId': park_id} if park_id else {}),
               'fitCount': len(observations), 'fitObjectCount': len({p['referenceId'] for p in observations}),
               'validationCount': len(checks), 'validationObjectCount': len({p['referenceId'] for p in checks}),
               'validationOmittedWayIds': omitted_ways, 'validationTotalWays': len(validation),
               'validationWays': validation_ways,
               'validationInsideHull': int(inside.sum()), 'medianErrorM': round(float(np.median(way_errors)), 2),
               'p95ErrorM': round(float(np.percentile(way_errors, 95)), 2), 'maxErrorM': round(float(max(way_errors)), 2),
               'foldedTriangles': 0, 'landmarks': observations+checks,
               'validationScope': f'{"公园轮廓" if park_id else "全局搜索"}提出位置，再按道路形状对齐；内部留出 {len(validation)} 条道路，其中 {len(validation)-len(omitted_ways)} 条能对应。'
                                  '误差统计仅针对能对应的路段与原图道路中心线，仍需现场核对。'}
    if streets_only:
        quality['referenceHighways'] = list(STREET_TYPES)
        quality['validationScope'] = (f'全局搜索提出位置，原图铺装与主要街道单独对齐；整条留出 {len(validation)} 条街道，全部核查，'
                                      '不把未印成铺装的小路和私人支路强行拉入拟合。边界只采用实测训练路段，仍需现场核对。')
    if refinement:
        quality['roadRefinement'] = refinement
    if extraction:
        quality['pathExtraction'] = extraction
    if seeds:
        quality['orientedPathSupport'] = round(1-rank, 3)
    if thin:
        quality['thinValidation'] = True
    if boundary_matches:
        quality['boundaryPathSamples'] = len(boundary_matches)
        quality['boundaryPixelTolerance'] = .5
    return reg, quality
