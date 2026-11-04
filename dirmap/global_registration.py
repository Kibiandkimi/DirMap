"""Find a sheet within its place without a place-specific seed.

The water, roof and park-outline seeds each need one distinctive object of
the right kind, and on a new place they can lock onto a look-alike. Here
scale, rotation and position are searched jointly instead. The sheet's
observed roofs, water and printed path centre lines are drawn at each
candidate scale and rotation and correlated with the same layers of the
place's OSM reference for every translation at once (FFT). The coarse
poses seed three fits: printed ways, whole objects, and block-by-block
layer correlation, each with its own held-out check. Every candidate,
including results of the seeded methods, is then judged by how well the
whole sheet agrees with the place at its final pose; a different place
supported almost as well is a refusal, not a guess.

Priors are generic: a map is normally drawn north up and a control circle
is 5-6 mm at print scales from 1:3000 to 1:15000, which bounds the ground
size of a circle. Neither is required.
"""
import math

import cv2
import numpy as np
from scipy import fft

from .geometry import affine_points

CHANNELS = ('building', 'water', 'path')
# Printed widths vary, so centre lines of every way compare, except ways a
# sheet never prints as paths on the ground.
UNPRINTED_WAYS = ('motorway', 'motorway_link', 'proposed', 'construction', 'platform', 'elevator')
NORTH_UP_DEG = np.arange(-12, 12.1, 3.)
SCALE_STEP = 1.04


class NoEvidence(ValueError):
    """The sheet shows no layer that the place's reference also has."""


def _rotation(angle):
    c, s = math.cos(angle), math.sin(angle)
    return np.array([[c, -s], [s, c]])


def reference_rasters(reference, cell, bounds=None):
    """Building, water and path rasters of the place (rows run southwards).

    OSM returns whole ways, so a river or avenue can reach far beyond the
    downloaded place; robust bounds keep the raster to the place itself.
    """
    if bounds is None:
        points = np.concatenate([np.asarray(f['points'], float) for f in reference])
        bounds = np.percentile(points, 1, axis=0)-150, np.percentile(points, 99, axis=0)+150
    lower, upper = bounds
    shape = tuple(np.ceil((np.asarray(upper)-lower)/cell).astype(int)[::-1]+1)
    origin = np.array([lower[0], upper[1]])
    to_cells = lambda p: np.rint((np.asarray(p, float)-origin)*[1/cell, -1/cell]*16).astype(np.int32)
    rasters = {kind: np.zeros(shape, np.float32) for kind in CHANNELS}
    for feature in reference:
        kind = feature['kind']
        if kind in ('building', 'water'):
            cv2.fillPoly(rasters[kind], [to_cells(feature['points'])], 1., cv2.LINE_8, 4)
        elif kind == 'waterway':
            cv2.polylines(rasters['water'], [to_cells(feature['points'])], False, 1., 1, cv2.LINE_8, 4)
        elif kind == 'road' and feature.get('tags', {}).get('highway') not in UNPRINTED_WAYS:
            cv2.polylines(rasters['path'], [to_cells(feature['points'])], False, 1., 1, cv2.LINE_8, 4)
    result = {}
    for kind, raster in rasters.items():
        raster = cv2.GaussianBlur(raster, (0, 0), 1.)
        if np.count_nonzero(raster > .2) >= 8:
            result[kind] = raster
    return result, origin


def sheet_layers(features, paths, footprint):
    """Observed sheet layers in working pixels."""
    layers = {'building': [np.asarray(f['points'], float) for f in features if f['kind'] == 'building'],
              'water': [np.asarray(f['points'], float) for f in features if f['kind'] == 'water'],
              'path': np.asarray(paths, float).reshape(-1, 2)}
    return layers, np.asarray(footprint, float)


def _draw(layers, footprint, linear, cell, channels, raw=False):
    """Rasterise the sheet under one linear map (working px -> cells).

    Layers come back zero-mean within the footprint and of unit norm, with
    a weight; `raw` returns the blurred layers themselves.
    """
    corners = footprint @ linear.T
    offset = corners.min(axis=0)-4
    size = np.ceil(corners.max(axis=0)-offset+4).astype(int)
    shape = (size[1], size[0])
    to_cells = lambda p: np.rint((np.asarray(p) @ linear.T-offset)*16).astype(np.int32)
    support = np.zeros(shape, np.float32)
    cv2.fillPoly(support, [to_cells(footprint)], 1., cv2.LINE_8, 4)
    inside = support > 0
    drawn = {}
    for kind in channels:
        raster = np.zeros(shape, np.float32)
        if kind == 'path':
            cells = np.rint(layers['path'] @ linear.T-offset).astype(int)
            if len(cells):
                raster[cells[:, 1].clip(0, shape[0]-1), cells[:, 0].clip(0, shape[1]-1)] = 1.
        else:
            for polygon in layers[kind]:
                cv2.fillPoly(raster, [to_cells(polygon)], 1., cv2.LINE_8, 4)
        evidence = float(np.count_nonzero(raster[inside]))
        raster = cv2.GaussianBlur(raster, (0, 0), 1.)
        if raw:
            drawn[kind] = raster
            continue
        values = raster[inside]
        raster = (raster-values.mean())*inside
        norm = float(np.linalg.norm(raster))
        if norm > 1e-6:
            drawn[kind] = (raster/norm, math.sqrt(evidence))
    return drawn, support, offset, shape


def _peaks(score, count, separation):
    """Strongest well separated maxima of a correlation surface."""
    work = score.copy()
    result = []
    for _ in range(count):
        index = int(np.argmax(work))
        row, col = divmod(index, work.shape[1])
        value = float(work[row, col])
        if not np.isfinite(value):
            break
        result.append((value, row, col))
        work[max(0, row-separation):row+separation+1, max(0, col-separation):col+separation+1] = -np.inf
    return result


def _correlate(layers, footprint, rasters, origin, cell, poses, peaks=2):
    """Masked normalised cross-correlation of every pose and translation.

    Per layer, the sheet raster has zero mean within its footprint and unit
    norm; the place's raster is normalised by its own local variance under
    that footprint. Zero mean makes a dense quarter of the place no more
    attractive than a sparse one unless shapes coincide.
    """
    channels = list(rasters)
    reference_shape = next(iter(rasters.values())).shape
    largest = max(math.ceil(scale/cell*float(np.hypot(*np.ptp(footprint, axis=0))))+12 for scale, _ in poses)
    shape = tuple(fft.next_fast_len(size+largest, real=True) for size in reference_shape)
    spectra = {kind: (fft.rfft2(rasters[kind], shape), fft.rfft2(rasters[kind]**2, shape)) for kind in channels}
    floors = {kind: .1*float(rasters[kind].var()) for kind in channels}
    signed = lambda size: np.where(np.arange(size) > size/2, np.arange(size)-size, np.arange(size))
    shift_rows, shift_cols = signed(shape[0]), signed(shape[1])
    flip = np.diag([1., -1.])
    found = []
    for scale, angle in poses:
        linear = scale/cell*_rotation(math.radians(angle))
        drawn, support, offset, canvas = _draw(layers, footprint, linear, cell, channels)
        if not drawn:
            continue
        count = float(support.sum())
        window = np.conj(fft.rfft2(support, shape))
        total = np.zeros(shape, np.float32)
        for kind, (raster, weight) in drawn.items():
            plain, squared = spectra[kind]
            numerator = fft.irfft2(np.conj(fft.rfft2(raster, shape))*plain, shape)
            local_sum = fft.irfft2(window*plain, shape)
            variance = fft.irfft2(window*squared, shape)-local_sum**2/count
            # An almost empty quarter of the place is not evidence; floor
            # its variance at a tenth of the place's typical variance.
            total += weight*numerator/np.sqrt(np.maximum(variance, floors[kind]*count))
        # A few blue specks must not outvote hundreds of roofs: each layer
        # counts by the square root of the cells it marks on the sheet.
        total /= sum(weight for _, weight in drawn.values())
        # The teacher frames the place around the sheet: at least half of
        # the sheet's bounding box must lie within the place.
        overlap = lambda shifts, size, limit: (np.clip(shifts+size, 0, limit)-np.clip(shifts, 0, limit))/size
        inside = np.outer(overlap(shift_rows, canvas[0], reference_shape[0]), overlap(shift_cols, canvas[1], reference_shape[1]))
        total[inside < .5] = -np.inf
        for value, row, col in _peaks(total, peaks, 6):
            shift = np.array([shift_cols[col], shift_rows[row]], float)
            matrix = np.column_stack((cell*flip @ linear, origin+cell*flip @ (shift-offset)))
            found.append((value, scale, angle, matrix))
    return found


def pose_support(layers, footprint, reference, matrix, cell=4., weighted=False):
    """Masked NCC of each sheet layer with the place at one exact pose."""
    corners = affine_points(matrix, footprint)
    rasters, origin = reference_rasters(reference, cell, (corners.min(axis=0)-60, corners.max(axis=0)+60))
    rasters = {kind: raster for kind, raster in rasters.items() if len(layers[kind]) >= (60 if kind == 'path' else 3)}
    flip = np.diag([1., -1.])
    drawn, support, offset, canvas = _draw(layers, footprint, flip @ np.asarray(matrix, float)[:, :2]/cell, cell, list(rasters))
    # Sheet canvas cell (x, y) lies at reference cell (x, y)+shift.
    col, row = np.rint(flip @ (np.asarray(matrix, float)[:, 2]-origin)/cell+offset).astype(int)
    result, weights = {}, {}
    count = float(support.sum())
    for kind, (raster, weight) in drawn.items():
        weights[kind] = weight
        window = np.zeros(canvas, np.float32)
        top, left = max(0, row), max(0, col)
        bottom, right = min(rasters[kind].shape[0], row+canvas[0]), min(rasters[kind].shape[1], col+canvas[1])
        if bottom <= top or right <= left:
            continue
        window[top-row:bottom-row, left-col:right-col] = rasters[kind][top:bottom, left:right]
        inside = window*support
        variance = float(np.sum(inside**2)-np.sum(inside)**2/count)
        floor = .1*count*float(rasters[kind].var())
        result[kind] = round(float(np.sum(raster*window))/math.sqrt(max(variance, floor, 1e-12)), 4)
    return (result, weights) if weighted else result


def _distinct(found, footprint, keep, separation=40.):
    """Best poses whose sheet outlines differ by a tenth of the sheet."""
    found = sorted(found, key=lambda item: -item[0])
    diagonal = float(np.hypot(*np.ptp(footprint, axis=0)))
    distinct = []
    for item in found:
        limit = max(separation, .1*diagonal*item[1])
        if any(np.median(np.linalg.norm(affine_points(item[3], footprint)-affine_points(other[3], footprint), axis=1)) < limit
               for other in distinct):
            continue
        distinct.append(item)
        if len(distinct) >= keep:
            break
    return distinct


def _cell(ground, cells):
    """Cell size giving a sheet `ground` metres across about `cells` cells.

    Templates of similar cell counts have comparable correlation noise; a
    sheet shrunk to a few coarse cells would otherwise correlate well with
    almost anything. Sizes come from a fixed ladder so rasters are shared.
    """
    return float(2**(round(2*math.log2(max(2., ground/cells)))/2))


def search(layers, footprint, reference, scales, angles, keep=6, coarse=16):
    """Coarse similarity poses (working px -> local metres), best first.

    A sweep over all scales, rotations and translations (about 64 cells
    across the sheet) proposes poses; each is re-scored at twice that
    resolution with finer scale and rotation steps within a window.
    """
    usable = lambda kind: len(layers[kind]) >= (60 if kind == 'path' else 3)
    diagonal = float(np.hypot(*np.ptp(footprint, axis=0)))
    bands = {}
    for scale in scales[::2]:
        bands.setdefault(_cell(scale*diagonal, 64), []).append(scale)
    sweep, channels = [], set()
    for cell, members in sorted(bands.items()):
        rasters, origin = reference_rasters(reference, cell)
        rasters = {kind: raster for kind, raster in rasters.items() if usable(kind)}
        if rasters:
            channels |= set(rasters)
            sweep += _correlate(layers, footprint, rasters, origin, cell, [(s, a) for s in members for a in angles[::2]])
    if not sweep:
        raise NoEvidence('参照与原图没有共同的建筑、水体或道路可供搜索')
    refined = []
    for value, scale, angle, matrix in _distinct(sweep, footprint, coarse):
        cell = _cell(scale*diagonal, 128)
        corners = affine_points(matrix, footprint)
        margin = max(120., 15*cell)
        local, local_origin = reference_rasters(reference, cell, (corners.min(axis=0)-margin, corners.max(axis=0)+margin))
        local = {kind: raster for kind, raster in local.items() if usable(kind)}
        if not local:
            continue
        poses = [(scale*SCALE_STEP**k, angle+d) for k in (-2, -1, 0, 1, 2) for d in (-3, -1.5, 0, 1.5, 3)]
        refined += _correlate(layers, footprint, local, local_origin, cell, poses, peaks=1)
    return [{'score': round(value, 4), 'metresPerPixel': round(float(scale), 5), 'rotationDeg': round(float(angle), 2),
             'matrix': matrix, 'channels': sorted(channels)}
            for value, scale, angle, matrix in _distinct(refined, footprint, keep)]


def scale_range(footprint, controls, reference):
    """Plausible metres per working pixel from circle size and place size."""
    extent = float(np.hypot(*np.ptp(footprint, axis=0)))
    points = np.concatenate([np.asarray(f['points'], float) for f in reference])
    place = float(np.hypot(*(np.percentile(points, 99, axis=0)-np.percentile(points, 1, axis=0))))
    # A sheet shows at least 200 m and at most a little more than its place.
    low, high = 200/extent, max(250., place*1.1)/extent
    radii = [c['radiusPx'] for c in controls if c.get('role', 'control') == 'control' and c.get('radiusPx')]
    if len(radii) >= 2:
        # 5-6 mm circles at 1:3000-1:15000 enlarged or reduced by printing
        # are 15-90 m across on the ground.
        radius = float(np.median(radii))
        if max(low, 7.5/radius) < min(high, 45/radius):
            low, high = max(low, 7.5/radius), min(high, 45/radius)
    count = max(1, math.ceil(math.log(high/low)/math.log(SCALE_STEP)))
    return low*SCALE_STEP**np.arange(count+1)


def sheet_evidence(image, cartography, result):
    """The sheet's layers and outline, plus its printed paths if any."""
    from .network_registration import map_footprint, printed_paths
    diagnostics = {}
    try:
        pixels, outline, ratio = printed_paths(cartography, diagnostics=diagnostics, exposure_image=image)
        paths, footprint, extraction = pixels/ratio, outline/ratio, (pixels, outline, ratio, diagnostics)
    except ValueError:
        factor = min(1., 1400/max(image.shape[:2]))
        outline = map_footprint(cv2.resize(cartography, None, fx=factor, fy=factor, interpolation=cv2.INTER_AREA))
        h, w = image.shape[:2]
        paths, extraction = np.empty((0, 2)), None
        footprint = outline/factor if outline is not None else np.array([[0, 0], [w, 0], [w, h], [0, h]], float)
    layers, footprint = sheet_layers(result['features'], paths, footprint)
    return layers, footprint, extraction


def support(layers, footprint, reference, matrix):
    """Evidence-weighted agreement of all sheet layers at one final pose.

    The same yardstick for every candidate, whichever seed and fit produced
    it, at about 128 cells across the sheet.
    """
    matrix = np.asarray(matrix, float)
    ground = math.sqrt(abs(np.linalg.det(matrix[:, :2])))*float(np.hypot(*np.ptp(footprint, axis=0)))
    values, weights = pose_support(layers, footprint, reference, matrix, _cell(ground, 128), weighted=True)
    if not values:
        return 0., {}
    return sum(values[k]*weights[k] for k in values)/sum(weights[k] for k in values), values


def poses_agree(first, second, footprint, coarse=False):
    """Same placement of the sheet; `coarse` allows a search pose's error."""
    ground = math.sqrt(abs(np.linalg.det(np.asarray(first)[:, :2])))*float(np.hypot(*np.ptp(footprint, axis=0)))
    distance = np.median(np.linalg.norm(affine_points(first, footprint)-affine_points(second, footprint), axis=1))
    return distance <= (max(40., .05*ground) if coarse else max(15., .02*ground))


def refine(image, cartography, result, reference, seeds, extraction, layers, footprint):
    """Fit layer blocks, printed ways and objects from each searched pose.

    Each fit keeps its own held-out check; what fails it is reported."""
    from .network_registration import road_network_register
    from .registration import auto_register
    attempts, failures = [], []
    for seed in seeds:
        try:
            attempts.append(layer_correlation_register(layers, footprint, reference, seed))
        except ValueError as exc:
            failures.append(str(exc))
        if extraction is not None:
            try:
                attempts.append(road_network_register(cartography, reference, exposure_image=image,
                                                      seeds=[seed], paths=extraction))
            except ValueError as exc:
                failures.append(str(exc))
        try:
            reg, quality = auto_register(result['features'], reference, result['width'], result['height'], initial=seed)
        except ValueError as exc:
            failures.append(str(exc))
            continue
        # A searched pose has no seed object vouching for it; its object
        # fit must stand on held-out objects. A small sheet may hold out
        # only one or two: such a fit competes, but `choose` lets it win only
        # by a wide support margin.
        if quality['medianErrorM'] <= 8 and quality['p95ErrorM'] <= 18:
            if quality['validationObjectCount'] < 3:
                quality = {**quality, 'thinValidation': True}
            attempts.append((reg, quality))
        else:
            failures.append(f'候选位置的留出地物误差过大（P95 {quality["p95ErrorM"]} m）')
    return attempts, failures


def _angle(matrix):
    """Rotation of a pose in the search's convention (0 = north up)."""
    linear = np.diag([1., -1.]) @ np.asarray(matrix, float)[:, :2]
    return math.degrees(math.atan2(linear[1, 0], linear[0, 0]))


def _similarity(source, target):
    """Least-squares similarity with the sheet's y axis flipped (Umeyama)."""
    source = np.asarray(source, float)*[1., -1.]
    target = np.asarray(target, float)
    mean_s, mean_t = source.mean(axis=0), target.mean(axis=0)
    a, b = source-mean_s, target-mean_t
    u, sigma, vt = np.linalg.svd(b.T @ a)
    d = np.diag([1., np.sign(np.linalg.det(u @ vt))])
    rotation = u @ d @ vt
    scale = float(np.sum(sigma*np.diag(d))/np.sum(a**2))
    linear = scale*rotation @ np.diag([1., -1.])
    return np.column_stack((linear, mean_t-linear @ (mean_s*[1., -1.])))


def layer_correlation_register(layers, footprint, reference, seed, blocks=5):
    """Fit a searched pose by correlating the sheet block by block.

    Refines the pose with fine scale, rotation and translation steps, then
    lets each block of the sheet find its own shift against the place's
    layers. Blocks are independent measurements: a robust fit uses most
    of them, and each is checked against a fit to all the others.
    Works with whatever layers the sheet shows (water, roofs or paths), so
    a park without matchable roofs or paved ways can still be checked.
    """
    from .geometry import Registration
    seed = np.asarray(seed, float)
    usable = lambda kind: len(layers[kind]) >= (60 if kind == 'path' else 3)
    scale = math.sqrt(abs(np.linalg.det(seed[:, :2])))
    ground = scale*float(np.hypot(*np.ptp(footprint, axis=0)))
    cell = _cell(ground, 192)
    corners = affine_points(seed, footprint)
    margin = max(80., 12*cell)
    rasters, origin = reference_rasters(reference, cell, (corners.min(axis=0)-margin, corners.max(axis=0)+margin))
    rasters = {kind: raster for kind, raster in rasters.items() if usable(kind)}
    if not rasters:
        raise ValueError('候选位置附近没有可比对的参照图层')
    angle = _angle(seed)
    poses = [(scale*1.01**k, angle+d) for k in range(-3, 4) for d in np.arange(-1.5, 1.6, .5)]
    found = _correlate(layers, footprint, rasters, origin, cell, poses, peaks=1)
    if not found:
        raise ValueError('候选位置无法细化')
    pose = max(found, key=lambda item: item[0])[3]
    flip = np.diag([1., -1.])
    linear = flip @ pose[:, :2]/cell
    drawn, support, offset, canvas = _draw(layers, footprint, linear, cell, list(rasters), raw=True)
    shift = flip @ (pose[:, 2]-origin)/cell+offset
    reach = max(3, round(25/cell))
    inverse = np.linalg.inv(linear)
    pairs = []
    for row in range(blocks):
        for col in range(blocks):
            top, bottom = round(row*canvas[0]/blocks), round((row+1)*canvas[0]/blocks)
            left, right = round(col*canvas[1]/blocks), round((col+1)*canvas[1]/blocks)
            if support[top:bottom, left:right].mean() < .5:
                continue
            y0, x0 = top+round(shift[1])-reach, left+round(shift[0])-reach
            y1, x1 = bottom+round(shift[1])+reach, right+round(shift[0])+reach
            total, weights = None, 0.
            for kind, raster in drawn.items():
                template = raster[top:bottom, left:right]
                ink = float(np.count_nonzero(template > .2))
                reference_window = rasters[kind][max(0, y0):y1, max(0, x0):x1]
                if ink < 8 or template.std() < 1e-3 or reference_window.shape != (y1-y0, x1-x0) or reference_window.std() < 1e-3:
                    continue
                score = cv2.matchTemplate(reference_window, template, cv2.TM_CCOEFF_NORMED)
                total = math.sqrt(ink)*score if total is None else total+math.sqrt(ink)*score
                weights += math.sqrt(ink)
            if total is None:
                continue
            total /= weights
            peak_y, peak_x = np.unravel_index(int(np.argmax(total)), total.shape)
            best = float(total[peak_y, peak_x])
            others = total.copy()
            others[max(0, peak_y-2):peak_y+3, max(0, peak_x-2):peak_x+3] = -np.inf
            # A clear, interior peak only: an edge peak may lie beyond reach,
            # and a flat ridge (a long straight road) fixes one axis only.
            if (best < .3 or best-float(others.max()) < .03 or not 0 < peak_y < total.shape[0]-1
                    or not 0 < peak_x < total.shape[1]-1):
                continue
            offsets = []
            for values in (total[peak_y-1:peak_y+2, peak_x], total[peak_y, peak_x-1:peak_x+2]):
                curvature = values[0]-2*values[1]+values[2]
                offsets.append(.5*(values[0]-values[2])/curvature if curvature < 0 else 0.)
            moved = np.array([peak_x+offsets[1], peak_y+offsets[0]], float)-reach
            centre = np.array([(left+right)/2, (top+bottom)/2])
            pixel = inverse @ (centre+offset)
            local = origin+cell*flip @ (centre+shift+moved)
            pairs.append({'pixel': pixel.tolist(), 'local': local.tolist(), 'kind': 'layer_block',
                          'referenceId': f'block-{row}-{col}', 'correlation': round(best, 3)})
    if len(pairs) < 6:
        raise ValueError(f'仅 {len(pairs)} 个图块与参照清晰吻合，不足以核查配准')
    # Outlying blocks are found against the dense pose fitted to the whole
    # sheet; the blocks then refine that similarity only if they span enough
    # of the sheet, since a cluster in one corner would tilt it elsewhere.
    keep = [float(np.linalg.norm(affine_points(pose, [p['pixel']])[0]-p['local'])) <= 10 for p in pairs]
    fitting = [p for p, accepted in zip(pairs, keep) if accepted]
    matrix = pose
    if len(fitting) >= 6:
        spread = cv2.contourArea(cv2.convexHull(np.array([p['pixel'] for p in fitting], np.float32)))
        sheet = cv2.contourArea(footprint.astype(np.float32))
        if spread < .1*sheet:
            raise ValueError('吻合的图块集中在一条带上，不足以核查配准')
        if spread >= .35*sheet:
            matrix = _similarity([p['pixel'] for p in fitting], [p['local'] for p in fitting])
    if len(fitting) < 6 or len(fitting) < .6*len(pairs):
        raise ValueError('图块位移不一致，不能建立可核查的配准')
    # Every block is checked against a similarity fitted to all the others:
    # its own measurement never predicts itself.
    errors = []
    for index, block in enumerate(fitting):
        others = [p for i, p in enumerate(fitting) if i != index]
        similarity = _similarity([p['pixel'] for p in others], [p['local'] for p in others])
        errors.append(float(np.linalg.norm(affine_points(similarity, [block['pixel']])[0]-block['local'])))
    errors = np.array(errors)
    try:
        reg = Registration(matrix, [p['pixel'] for p in fitting], [p['local'] for p in fitting])
    except ValueError:
        # Block shifts too uneven for a non-folding mesh: keep the global
        # fit over the same measured support. Block centres lie on a grid;
        # only the strictly convex hull vertices are kept, since collinear
        # boundary points would read as false concavities.
        pixels = np.array([p['pixel'] for p in fitting])
        corners = pixels[cv2.convexHull(pixels.astype(np.float32), returnPoints=False).ravel()]
        reg = Registration(matrix, corners, affine_points(matrix, corners), regularization=0)
    for p, error in zip(fitting, errors):
        p.update(purpose='fit', errorM=round(float(np.linalg.norm(affine_points(matrix, [p['pixel']])[0]-p['local'])), 2),
                 leaveOneOutErrorM=round(float(error), 2))
    quality = {'state': 'needs_review', 'method': 'global_search_layer_correlation',
               'fitCount': len(fitting), 'fitObjectCount': len(fitting), 'validationCount': len(fitting),
               'validationObjectCount': len(fitting), 'validationInsideHull': len(fitting), 'leaveOneOut': True,
               'rejectedObjectIds': sorted(p['referenceId'] for p, accepted in zip(pairs, keep) if not accepted),
               'medianErrorM': round(float(np.median(errors)), 2), 'p95ErrorM': round(float(np.percentile(errors, 95)), 2),
               'maxErrorM': round(float(errors.max()), 2), 'foldedTriangles': reg.folded, 'cellM': cell,
               'landmarks': fitting,
               'validationScope': '全局搜索提出位置，原图分块与参照的建筑、水体、道路图层逐块比对；每个图块都用其余图块拟合的相似变换预测并核对（留一法）。'
                                  '图块位置来自图层相关，不是逐个地物的同名点，仍需现场核对。'}
    if quality['medianErrorM'] > 6 or quality['p95ErrorM'] > 12:
        raise ValueError(f'图块留出核查误差过大（P95 {quality["p95ErrorM"]} m）')
    return reg, quality


AMBIGUITY_MARGIN = .05
METHOD_ORDER = ('global_seed', 'global_search_layer', 'global_search_road')
MINIMUM_SUPPORT = .2


def choose(attempts, layers, footprint, reference, preferred=(), searched=(), controls=()):
    """Pick the candidate whose final pose the sheet supports best.

    Refuse when a different pose is supported almost as well, or when no
    pose is supported at all: a refusal asks the teacher for a few
    corresponding points, a wrong fit silently misplaces every control.
    A `preferred` attempt (an established method) that agrees with the
    winner is returned instead, keeping earlier results unchanged.
    """
    scored = []
    for reg, quality in attempts:
        value, layers_support = support(layers, footprint, reference, reg.affine)
        scored.append((value, reg, quality, layers_support))
    if not scored:
        raise ValueError('没有可核查的候选配准')
    scored.sort(key=lambda item: -item[0])
    best_value, best, best_quality, best_layers = scored[0]
    alternatives = [{'method': quality.get('method'), 'support': round(value, 4),
                     'agreesWithChosen': bool(poses_agree(best.affine, reg.affine, footprint)),
                     'affine': np.round(reg.affine, 6).tolist()} for value, reg, quality, _ in scored]
    if best_value < MINIMUM_SUPPORT:
        raise ValueError(f'候选配准与参照的吻合度过低（{best_value:.2f}），请补充同名点')
    if best_quality.get('thinValidation'):
        rivals = [value for value, reg, *_ in scored[1:] if not poses_agree(best.affine, reg.affine, footprint)]
        rivals += [c['score'] for c in searched if not poses_agree(best.affine, c['matrix'], footprint, coarse=True)]
        if best_value-max(rivals, default=0.) < 2*AMBIGUITY_MARGIN:
            raise ValueError(f'留出地物过少，且其他位置的吻合度相差不足（{best_value:.2f}），请补充同名点')
    for value, reg, quality, _ in scored[1:]:
        if value >= best_value-AMBIGUITY_MARGIN and not poses_agree(best.affine, reg.affine, footprint):
            raise ValueError(f'存在吻合度相近的不同位置（{best_value:.2f} 与 {value:.2f}），请补充同名点确定位置')
    # A searched pose that no fit could confirm still counts against the
    # winner: it may be the true place where the objects did not suffice.
    for candidate in searched:
        if candidate['score'] >= best_value-AMBIGUITY_MARGIN and not poses_agree(best.affine, candidate['matrix'], footprint, coarse=True):
            raise ValueError(f'另一处位置与原图同样吻合（{candidate["score"]:.2f}，已选 {best_value:.2f}），请补充同名点确定位置')
    # Support picks the place; it is too coarse to rank fits of the same
    # place. There, an established result wins, then the fit that calibrates
    # most course controls (a control outside the measured area is not
    # calibrated), then a fit of whole objects, block correlation of all
    # layers, and printed ways (whose centre lines may sit beside paving).
    same_place = [(reg, quality) for _, reg, quality, _ in scored if poses_agree(best.affine, reg.affine, footprint)]
    same_place += [(reg, quality) for reg, quality in preferred
                   if poses_agree(best.affine, reg.affine, footprint) and all(reg is not r for r, _ in same_place)]
    controls = np.asarray(controls, float).reshape(-1, 2)
    order = lambda item: (not any(item[0] is reg for reg, _ in preferred),
                          -int(item[0].forward(controls)[1].sum()) if len(controls) else 0,
                          next((i for i, prefix in enumerate(METHOD_ORDER) if item[1].get('method', '').startswith(prefix)),
                               len(METHOD_ORDER)))
    best, best_quality = min(same_place, key=order)
    best_quality = dict(best_quality)
    best_quality['poseSupport'] = {'value': round(best_value, 4), 'layers': best_layers,
                                   'margin': round(best_value-max([v for v, r, *_ in scored[1:]
                                                                   if not poses_agree(best.affine, r.affine, footprint)], default=0.), 4),
                                   'alternatives': alternatives}
    return best, best_quality


def register(image, cartography, result, reference, *, angles=NORTH_UP_DEG, attempts=4, established=()):
    """Global search, fits from each pose, and the common support check.

    `established` holds registrations already produced by the seeded
    methods; they compete under the same check rather than being trusted.
    """
    layers, footprint, extraction = sheet_evidence(image, cartography, result)
    candidates = search(layers, footprint, reference, scale_range(footprint, result['controls'], reference), angles)
    # A searched pose that an established result already covers, or that
    # scores well below it, cannot change the decision; skip its fits.
    floor = max([support(layers, footprint, reference, reg.affine)[0] for reg, _ in established], default=-np.inf)
    seeds = [candidate['matrix'] for candidate in candidates[:attempts]
             if candidate['score'] >= floor-2*AMBIGUITY_MARGIN
             and not any(poses_agree(reg.affine, candidate['matrix'], footprint, coarse=True) for reg, _ in established)]
    fitted, failures = refine(image, cartography, result, reference, seeds, extraction, layers, footprint)
    if fitted:
        # A fitted pose is a far better seed than a search pose: fit once
        # more from the best-supported one, so methods that need a close
        # start (printed ways, roof corners) can also measure that place.
        lead = max(fitted, key=lambda item: support(layers, footprint, reference, item[0].affine)[0])[0]
        polished, _ = refine(image, cartography, result, reference, [lead.affine], extraction, layers, footprint)
        fitted += [(reg, {**quality, 'polished': True}) for reg, quality in polished]
    try:
        reg, quality = choose(list(established)+fitted, layers, footprint, reference, preferred=established, searched=candidates,
                              controls=[c['pixel'] for c in result['controls'] if c.get('role', 'control') == 'control'])
    except ValueError as exc:
        raise ValueError(str(exc)+('；'+'；'.join(dict.fromkeys(failures))[:160] if failures and not fitted else '')) from None
    quality['globalSearch'] = {'candidates': [{key: candidate[key] for key in ('score', 'metresPerPixel', 'rotationDeg')}
                                              for candidate in candidates],
                               'channels': candidates[0]['channels'] if candidates else [], 'attempted': len(seeds)}
    return reg, quality
