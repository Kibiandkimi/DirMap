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
import contextlib
import copy
import math
import threading
from collections import OrderedDict

import cv2
import numpy as np
from scipy import fft
from scipy.spatial import cKDTree

from .geometry import affine_points

CHANNELS = ('building', 'water', 'path')
# Printed widths vary, so centre lines of every way compare, except ways a
# sheet never prints as paths on the ground.
UNPRINTED_WAYS = ('motorway', 'motorway_link', 'proposed', 'construction', 'platform', 'elevator')
NORTH_UP_DEG = np.arange(-12, 12.1, 3.)
# The search subsamples angles by two, then refines each by +/-3 degrees.
# Together with the north-up pass this covers every orientation, including
# oblique scans, without slowing the normal successful north-up path.
TURNED_DEG = np.arange(18, 348, 3.)
SCALE_STEP = 1.04


class NoEvidence(ValueError):
    """The sheet shows no layer that the place's reference also has."""


class BlockFitError(ValueError):
    """Block measurements exist, but did not yet establish a checked fit."""

    def __init__(self, message, observed_blocks):
        super().__init__(message)
        self.observed_blocks = observed_blocks


def _rotation(angle):
    c, s = math.cos(angle), math.sin(angle)
    return np.array([[c, -s], [s, c]])


class _Memo:
    """Pure computations on one place's reference within one registration.

    The search compares every reading of the sheet (palettes, text-free,
    without black roofs, north up and turned) with the same place, and
    re-scores the same poses under each. Their rasters, spectra and
    scores, and fits from a seed under the same path evidence, are reused
    unchanged: outcomes are exactly those of recomputing them. Large
    arrays are kept within a byte budget, least recently used dropped.
    """
    BUDGET = 64 << 20

    def __init__(self, reference):
        self.reference = reference
        self.arrays = OrderedDict()  # key -> (value, bytes)
        self.size = 0
        self.values = {}
        self.table = None

    def get(self, key):
        if key in self.arrays:
            self.arrays.move_to_end(key)
            return self.arrays[key][0]
        return None

    def put(self, key, value, size):
        if size > self.BUDGET//2:
            return
        self.arrays[key] = (value, size)
        self.size += size
        while self.size > self.BUDGET:
            _, (_, dropped) = self.arrays.popitem(last=False)
            self.size -= dropped


_LOCAL = threading.local()


@contextlib.contextmanager
def memoised(reference):
    """Reuse pure place computations for `reference` within this block."""
    previous = getattr(_LOCAL, 'memo', None)
    if previous is not None and previous.reference is reference:
        yield previous
        return
    _LOCAL.memo = _Memo(reference)
    try:
        yield _LOCAL.memo
    finally:
        _LOCAL.memo = previous


def _memo(reference):
    memo = getattr(_LOCAL, 'memo', None)
    return memo if memo is not None and memo.reference is reference else None


def _reference_table(reference):
    """Drawable reference features: (layer, polygon?, points), their boxes, robust bounds, channels."""
    memo = _memo(reference)
    if memo is not None and memo.table is not None:
        return memo.table
    drawn, boxes = [], []
    for feature in reference:
        kind = feature['kind']
        if kind in ('building', 'water'):
            item = (kind, True)
        elif kind == 'waterway':
            item = ('water', False)
        elif kind == 'road' and feature.get('tags', {}).get('highway') not in UNPRINTED_WAYS:
            item = ('path', False)
        else:
            continue
        points = np.asarray(feature['points'], float)
        if not len(points):
            continue
        drawn.append((*item, points))
        boxes.append(np.concatenate((points.min(axis=0), points.max(axis=0))))
    points = np.concatenate([np.asarray(f['points'], float) for f in reference])
    bounds = np.percentile(points, 1, axis=0)-150, np.percentile(points, 99, axis=0)+150
    # A layer present in the place stays in every local comparison, even
    # if this window has none of it. Dropping an empty local layer would
    # reward a wrong pose for moving printed evidence away from its kind.
    available = {f['kind'] for f in reference}
    channels = ({'building'} if 'building' in available else set())
    if available & {'water', 'waterway'}:
        channels.add('water')
    if any(f['kind'] == 'road' and f.get('tags', {}).get('highway') not in UNPRINTED_WAYS for f in reference):
        channels.add('path')
    table = (drawn, np.array(boxes).reshape(-1, 4), bounds, channels)
    if memo is not None:
        memo.table = table
    return table


def reference_rasters(reference, cell, bounds=None):
    """Building, water and path rasters of the place (rows run southwards).

    OSM returns whole ways, so a river or avenue can reach far beyond the
    downloaded place; robust bounds keep the raster to the place itself.
    Returned arrays may be shared and must not be modified.
    """
    memo = _memo(reference)
    key = ('rasters', float(cell), None if bounds is None else np.asarray(bounds, float).tobytes())
    cached = memo.get(key) if memo is not None else None
    if cached is not None:
        return dict(cached[0]), cached[1].copy()
    drawn, boxes, robust, channels = _reference_table(reference)
    lower, upper = robust if bounds is None else bounds
    shape = tuple(np.ceil((np.asarray(upper)-lower)/cell).astype(int)[::-1]+1)
    origin = np.array([lower[0], upper[1]])
    to_cells = lambda p: np.rint((np.asarray(p, float)-origin)*[1/cell, -1/cell]*16).astype(np.int32)
    rasters = {kind: np.zeros(shape, np.float32) for kind in CHANNELS}
    # A feature wholly beyond the raster draws nothing; skipping it leaves
    # every raster value unchanged.
    columns = np.column_stack(((boxes[:, 0]-origin[0])/cell, (boxes[:, 2]-origin[0])/cell))
    rows = np.column_stack(((origin[1]-boxes[:, 3])/cell, (origin[1]-boxes[:, 1])/cell))
    visible = ((columns[:, 1] >= -2) & (columns[:, 0] <= shape[1]+1)
               & (rows[:, 1] >= -2) & (rows[:, 0] <= shape[0]+1))
    for index in np.flatnonzero(visible):
        kind, polygon, points = drawn[index]
        if polygon:
            cv2.fillPoly(rasters[kind], [to_cells(points)], 1., cv2.LINE_8, 4)
        else:
            cv2.polylines(rasters[kind], [to_cells(points)], False, 1., 1, cv2.LINE_8, 4)
    result = {}
    for kind, raster in rasters.items():
        raster = cv2.GaussianBlur(raster, (0, 0), 1.)
        if kind in channels:
            result[kind] = raster
    if memo is not None:
        memo.put(key, (result, origin), sum(raster.nbytes for raster in result.values()))
    return dict(result), origin.copy()


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
        if not evidence:
            # Ink just outside the map can blur into it. That is not an
            # observed layer, and its zero weight cannot normalise a score.
            continue
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


def _spectra(raster, shape):
    """Spectra of a place raster and of its square, reused within a registration."""
    memo = getattr(_LOCAL, 'memo', None)
    key = ('spectra', id(raster), shape)
    cached = memo.get(key) if memo is not None else None
    if cached is not None and cached[0] is raster:
        return cached[1]
    spectra = fft.rfft2(raster, shape), fft.rfft2(raster**2, shape)
    if memo is not None:
        memo.put(key, (raster, spectra), sum(part.nbytes for part in spectra))
    return spectra


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
    spectra = {kind: _spectra(rasters[kind], shape) for kind in channels}
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
            total += weight*numerator/np.sqrt(np.maximum(variance, max(floors[kind]*count, 1e-12)))
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


def _distinct(found, footprint, keep, separation=40., scale_window=None):
    """Best poses whose sheet outlines differ by a tenth of the sheet.

    With `scale_window`, poses whose scales differ by more than that factor
    are distinct even where their outlines lie close: refining one within
    the window can never reach the other's scale.
    """
    found = sorted(found, key=lambda item: -item[0])
    diagonal = float(np.hypot(*np.ptp(footprint, axis=0)))
    distinct = []
    within = (lambda a, b: True) if scale_window is None else (
        lambda a, b: abs(math.log(a/b)) <= math.log(scale_window)+1e-9)
    for item in found:
        limit = max(separation, .1*diagonal*item[1])
        if any(within(item[1], other[1])
               and np.median(np.linalg.norm(affine_points(item[3], footprint)-affine_points(other[3], footprint), axis=1)) < limit
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


def search(layers, footprint, reference, scales, angles, keep=6, coarse=16, coarse_cells=64):
    """Coarse similarity poses (working px -> local metres), best first.

    A sweep over all scales, rotations and translations (about 64 cells
    across the sheet) proposes poses; each is re-scored at twice that
    resolution with finer scale and rotation steps within a window.
    """
    usable = lambda kind: len(layers[kind]) >= (60 if kind == 'path' else 3)
    diagonal = float(np.hypot(*np.ptp(footprint, axis=0)))
    bands = {}
    for scale in scales[::2]:
        bands.setdefault(_cell(scale*diagonal, coarse_cells), []).append(scale)
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
    # A near-true sweep peak can lie within a tenth of the sheet of a
    # stronger peak at a quite different scale; merged into it, it would
    # never be refined at its own scale.
    for value, scale, angle, matrix in _distinct(sweep, footprint, coarse, scale_window=SCALE_STEP**2):
        cell = _cell(scale*diagonal, 128)
        corners = affine_points(matrix, footprint)
        margin = max(120., 15*cell)
        local, local_origin = reference_rasters(reference, cell, (corners.min(axis=0)-margin, corners.max(axis=0)+margin))
        local = {kind: raster for kind, raster in local.items() if usable(kind)}
        if not local:
            continue
        poses = [(scale*SCALE_STEP**k, angle+d) for k in (-2, -1, 0, 1, 2) for d in (-3, -1.5, 0, 1.5, 3)]
        refined += _correlate(layers, footprint, local, local_origin, cell, poses, peaks=1)
    # A local search window can omit reference ink beyond its edge. Its
    # peak is a proposal score, not the common whole-sheet comparison:
    # using it as a rival's final score can veto a correct, measured fit.
    # Proposals are ranked as the sweep's templates compare them, about 128
    # cells across the sheet at every scale: on a fixed ground cell a pose
    # shrinking the sheet to a small window keeps few cells, whose noise can
    # outrank the true place. Each carries the common whole-sheet support
    # (`score`) by which every candidate is then judged.
    candidates = [{'score': round(support(layers, footprint, reference, matrix)[0], 4),
                   'proposal': round(support(layers, footprint, reference, matrix, ground_cell=False)[0], 4),
                   'metresPerPixel': round(float(scale), 5), 'rotationDeg': round(float(angle), 2),
                   'matrix': matrix, 'channels': sorted(channels)}
                  for _, scale, angle, matrix in _distinct(refined, footprint, max(keep, coarse))]
    return sorted(candidates, key=lambda candidate: -candidate['proposal'])[:keep]


def scale_range(footprint, controls, reference, hint=None):
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
    if hint is not None:
        measured = hint['metresPerPixel']
        if max(low, measured*.8) < min(high, measured*1.25):
            low, high = max(low, measured*.8), min(high, measured*1.25)
    count = max(1, math.ceil(math.log(high/low)/math.log(SCALE_STEP)))
    return low*SCALE_STEP**np.arange(count+1)


def sheet_evidence(image, cartography, result):
    """The sheet's layers and outline, plus its printed paths if any."""
    from .network_registration import registration_footprint, printed_paths
    diagnostics = {}
    try:
        pixels, outline, ratio = printed_paths(cartography, diagnostics=diagnostics, exposure_image=image)
        paths, footprint, extraction = pixels/ratio, outline/ratio, (pixels, outline, ratio, diagnostics)
    except ValueError:
        factor = min(1., 1400/max(image.shape[:2]))
        outline = registration_footprint(cv2.resize(cartography, None, fx=factor, fy=factor, interpolation=cv2.INTER_AREA))
        h, w = image.shape[:2]
        paths, extraction = np.empty((0, 2)), None
        footprint = outline/factor if outline is not None else np.array([[0, 0], [w, 0], [w, h], [0, h]], float)
    layers, footprint = sheet_layers(result['features'], paths, footprint)
    return layers, footprint, extraction


def expanded_paths(image, footprint, *, linear_ink=False, darker_than_fill=False):
    """Fallback for deeper paving and forest-style black path strokes.

    These are candidates for matching, not a traversability mask. Dark ink
    also contains fences and lettering, so the usual held-out fits and
    whole-sheet ambiguity checks still decide whether it supplies a pose.
    """
    from .network_registration import thin
    ratio = min(1., 1400/max(image.shape[:2]))
    small = cv2.resize(image, None, fx=ratio, fy=ratio, interpolation=cv2.INTER_AREA)
    h, s, v = cv2.split(cv2.cvtColor(small, cv2.COLOR_BGR2HSV))
    paving = ((h >= 3) & (h <= 27) & (s >= 12) & (s <= 150) & (v > 135)).astype(np.uint8)
    if darker_than_fill:
        # Some sheets print paving in a tan of almost the same hue and
        # saturation as their yellow open land, only darker (V about 226
        # against 252). Paths crossing open land then join the fields'
        # broad areas, and removing those erases the paths with them.
        # Keep instead the tan that is darker than the fill on both sides
        # within a printed road's width: a morphological closing restores
        # the surrounding fill over strokes narrower than the kernel, while
        # broad areas, including their edges, close to themselves. Open
        # land as bright as the paper is never darker than its surroundings.
        fill = cv2.morphologyEx(v, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (21, 21)))
        paving &= (fill.astype(np.int16)-v >= 12).astype(np.uint8)
        # Hairlines such as contour edges are not printed roads.
        paving = cv2.morphologyEx(paving, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    else:
        paving = cv2.morphologyEx(paving, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
        # Pale open land can share paving's hue. Its broad filled areas are
        # not observed ways, and thinning them invents lines through fields.
        broad = cv2.morphologyEx(paving, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (19, 19)))
        paving[cv2.dilate(broad, np.ones((5, 5), np.uint8)) > 0] = 0
    if linear_ink:
        ink = ((v < 140) & (s < 100)).astype(np.uint8)
        wide = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7)))
        ink[cv2.dilate(wide, np.ones((3, 3), np.uint8)) > 0] = 0
        _, labels, stats, _ = cv2.connectedComponentsWithStats(ink, 8)
        keep = [False]+[max(row[2], row[3]) >= 5 and row[4] >= 5 for row in stats[1:]]
        ink = np.asarray(keep, np.uint8)[labels]
        paving |= cv2.morphologyEx(ink, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    mask = np.zeros(small.shape[:2], np.uint8)
    cv2.fillPoly(mask, [np.rint(footprint*ratio).astype(np.int32)], 1)
    skeleton = thin(paving & mask)
    _, labels, stats, _ = cv2.connectedComponentsWithStats(skeleton, 8)
    keep = [False]+[row[4] >= 30 and max(row[2], row[3]) >= 18 for row in stats[1:]]
    yy, xx = np.nonzero(np.asarray(keep, np.uint8)[labels])
    if len(xx) < 200:
        raise NoEvidence('备用道路提取没有足够的连续线段')
    return np.column_stack((xx, yy)).astype(float), footprint*ratio, ratio, {
        'pathEvidence': 'linear_ink' if linear_ink else 'darker_paving' if darker_than_fill else 'expanded_paving'}


def different_paths(first, second, ratio):
    """A palette retry must add/remove evidence, not just shift the same raster ink."""
    if not len(second):
        return bool(len(first))
    if not len(first):
        return True
    first, second = np.asarray(first)*ratio, np.asarray(second)*ratio
    new = cKDTree(second).query(first, distance_upper_bound=1.5)[0]
    lost = cKDTree(first).query(second, distance_upper_bound=1.5)[0]
    return float(np.isinf(new).mean()) > .1 or float(np.isinf(lost).mean()) > .25


# Whole-sheet support compares poses at a fixed ground cell, not at a
# fixed number of cells across the sheet: a pose at twice the scale would
# otherwise be judged on cells twice as coarse, where any built-up quarter
# blurs into agreement with any other (a sheet's true place scored 0.33
# against 0.49 at twice its scale; at 4 m 0.42 against 0.12). Small sheets
# keep their 128 cells; very large ones are capped at 768 cells.
SUPPORT_CELL_M = 4.


def support_cell(ground, cells=128):
    """Cell size (m) at which `support` compares a sheet `ground` metres across."""
    return min(_cell(ground, cells), max(SUPPORT_CELL_M*128/cells, _cell(ground, 6*cells)))


def support(layers, footprint, reference, matrix, *, cells=128, ground_cell=True):
    """Evidence-weighted agreement of all sheet layers at one final pose.

    The same yardstick for every candidate, whichever seed and fit produced
    it (see `support_cell`; `cells` = 256 halves the cell). Without
    `ground_cell`, about `cells` cells across the sheet at every scale, as
    the search ranks the poses it proposes.
    """
    matrix = np.asarray(matrix, float)
    memo = _memo(reference)
    key = ('support', id(layers), np.asarray(footprint, float).tobytes(), matrix.tobytes(), cells, ground_cell)
    if memo is not None and key in memo.values and memo.values[key][0] is layers:
        value, values = memo.values[key][1]
        return value, dict(values)
    ground = math.sqrt(abs(np.linalg.det(matrix[:, :2])))*float(np.hypot(*np.ptp(footprint, axis=0)))
    cell = support_cell(ground, cells) if ground_cell else _cell(ground, cells)
    values, weights = pose_support(layers, footprint, reference, matrix, cell, weighted=True)
    result = (0., {}) if not values else (sum(values[k]*weights[k] for k in values)/sum(weights[k] for k in values), values)
    if memo is not None:
        memo.values[key] = (layers, result)
    return result[0], dict(result[1])


def poses_agree(first, second, footprint, coarse=False):
    """Same placement of the sheet; `coarse` allows a search pose's error."""
    ground = math.sqrt(abs(np.linalg.det(np.asarray(first)[:, :2])))*float(np.hypot(*np.ptp(footprint, axis=0)))
    distance = np.median(np.linalg.norm(affine_points(first, footprint)-affine_points(second, footprint), axis=1))
    return distance <= (max(40., .05*ground) if coarse else max(15., .02*ground))


def outdone(matrix, value, searched, footprint):
    """A searched pose of the same place that the sheet supports clearly better.

    A search pose may lie tens of metres from a fit of its place. A fit that
    wide of it, and clearly less supported, has misaligned that place:
    nearby look-alike roofs can still pass its own held-out check.
    """
    return any(candidate['score'] > value+AMBIGUITY_MARGIN
               and poses_agree(matrix, candidate['matrix'], footprint, coarse=True)
               and not poses_agree(matrix, candidate['matrix'], footprint) for candidate in searched)


def covered(candidate, fits, footprint):
    """Whether a fit, given as (matrix, support), already accounts for a searched pose."""
    return any(poses_agree(matrix, candidate['matrix'], footprint, coarse=True)
               and not outdone(matrix, value, [candidate], footprint) for matrix, value in fits)


def _content(memo, value):
    """A digest of a path extraction's content, computed once per object."""
    import hashlib
    import pickle
    key = ('content', id(value))
    if key not in memo.values or memo.values[key][0] is not value:
        pixels, outline, ratio, diagnostics = value
        digest = hashlib.sha256()
        for part in (pixels, outline):
            part = np.ascontiguousarray(part)
            digest.update(repr((part.dtype.str, part.shape)).encode())
            digest.update(part.tobytes())
        digest.update(pickle.dumps((float(ratio), sorted(diagnostics.items()))))
        memo.values[key] = (value, digest.hexdigest())
    return memo.values[key][1]


def road_fit(cartography, reference, image, seed, paths, streets_only=False):
    """`road_network_register` from one searched pose under given path evidence.

    With `paths` given the fit depends only on them, the seed and the
    place, so a seed met again under the same evidence (another reading
    of the sheet with unchanged paths) reuses the earlier outcome.
    """
    from .network_registration import road_network_register
    memo = _memo(reference)
    if memo is None:
        return road_network_register(cartography, reference, exposure_image=image, seeds=[seed], paths=paths,
                                     streets_only=streets_only)
    key = ('road', np.asarray(seed, float).tobytes(), _content(memo, paths), bool(streets_only))
    if key not in memo.values:
        try:
            memo.values[key] = (None, ('fit', road_network_register(cartography, reference, exposure_image=image, seeds=[seed],
                                                                    paths=paths, streets_only=streets_only)))
        except ValueError as exc:
            memo.values[key] = (None, ('error', type(exc), exc.args))
    outcome = memo.values[key][1]
    if outcome[0] == 'error':
        raise outcome[1](*outcome[2])
    reg, quality = outcome[1]
    return copy.copy(reg), copy.deepcopy(quality)


def street_paving(cartography, footprint):
    """`expanded_paths` of the plain palette, reused for one image and outline."""
    memo = getattr(_LOCAL, 'memo', None)
    if memo is None:
        return expanded_paths(cartography, footprint)
    key = ('paving', id(cartography), np.asarray(footprint, float).tobytes())
    if key not in memo.values or memo.values[key][0] is not cartography:
        try:
            memo.values[key] = (cartography, ('paths', expanded_paths(cartography, footprint)))
        except NoEvidence as exc:
            memo.values[key] = (cartography, ('error', exc.args))
    outcome = memo.values[key][1]
    if outcome[0] == 'error':
        raise NoEvidence(*outcome[1])
    return outcome[1]


def refine(image, cartography, result, reference, seeds, extraction, layers, footprint):
    """Fit layer blocks, printed ways and objects from each searched pose.

    Each fit keeps its own held-out check; what fails it is reported."""
    from .network_registration import STREET_TYPES
    from .registration import auto_register
    attempts, failures = [], []
    street_paths = None
    if seeds and sum(f['kind'] == 'road' and f.get('tags', {}).get('highway') in STREET_TYPES for f in reference) >= 9:
        if extraction is not None and extraction[3].get('pathEvidence') != 'linear_ink':
            street_paths = extraction
        else:
            try:
                street_paths = street_paving(cartography, footprint)
            except NoEvidence:
                pass
    for seed in seeds:
        try:
            attempts.append(layer_correlation_register(layers, footprint, reference, seed))
        except BlockFitError as exc:
            failures.append(str(exc))
            # Coarse pixels can merge nearby paths and displace their
            # blocks. Where several measurements already exist, retry at
            # finer resolution now, before unrelated poses crowd out this
            # seed. The same held-out/spread limits still apply.
            if exc.observed_blocks >= 3:
                for blocks, fine_cells in FINE_BLOCK_PASSES:
                    try:
                        reg, quality = layer_correlation_register(layers, footprint, reference, seed,
                                                                  blocks=blocks, fine_cells=fine_cells)
                        attempts.append((reg, {**quality, 'fineBlockRefinement': True}))
                        break
                    except ValueError as finer:
                        failures.append(str(finer))
        except ValueError as exc:
            failures.append(str(exc))
        if extraction is not None:
            try:
                attempts.append(road_fit(cartography, reference, image, seed, extraction))
            except ValueError as exc:
                failures.append(str(exc))
        if street_paths is not None:
            try:
                attempts.append(road_fit(cartography, reference, image, seed, street_paths, streets_only=True))
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


def layer_correlation_register(layers, footprint, reference, seed, blocks=5, cells=192, fine_cells=None):
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
    cell = _cell(ground, cells)
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
    if fine_cells:
        cell = _cell(ground, fine_cells)
        corners = affine_points(pose, footprint)
        margin = max(60., 8*cell)
        rasters, origin = reference_rasters(reference, cell, (corners.min(axis=0)-margin, corners.max(axis=0)+margin))
        rasters = {kind: raster for kind, raster in rasters.items() if usable(kind)}
        scale, angle = math.sqrt(abs(np.linalg.det(pose[:, :2]))), _angle(pose)
        poses = [(scale*1.003**k, angle+d) for k in range(-2, 3) for d in np.arange(-.5, .6, .25)]
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
        raise BlockFitError(f'仅 {len(pairs)} 个图块与参照清晰吻合，不足以核查配准', len(pairs))
    # Outlying blocks are found against the dense pose fitted to the whole
    # sheet; the blocks then refine that similarity only if they span enough
    # of the sheet, since a cluster in one corner would tilt it elsewhere.
    keep = [float(np.linalg.norm(affine_points(pose, [p['pixel']])[0]-p['local'])) <= 10 for p in pairs]
    fitting = [p for p, accepted in zip(pairs, keep) if accepted]
    matrix, hull_fraction = pose, None
    if len(fitting) >= 6:
        spread = cv2.contourArea(cv2.convexHull(np.array([p['pixel'] for p in fitting], np.float32)))
        sheet = cv2.contourArea(footprint.astype(np.float32))
        hull_fraction = spread/max(sheet, 1e-9)
        if spread < .1*sheet:
            raise BlockFitError('吻合的图块集中在一条带上，不足以核查配准', len(pairs))
        if spread >= .35*sheet:
            matrix = _similarity([p['pixel'] for p in fitting], [p['local'] for p in fitting])
    if len(fitting) < 6 or len(fitting) < .6*len(pairs):
        raise BlockFitError('图块位移不一致，不能建立可核查的配准', len(pairs))
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
               'blockHullFraction': None if hull_fraction is None else round(float(hull_fraction), 3),
               'poseSource': 'search_refinement' if matrix is pose else 'block_similarity',
               'landmarks': fitting,
               'validationScope': '全局搜索提出位置，原图分块与参照的建筑、水体、道路图层逐块比对；每个图块都用其余图块拟合的相似变换预测并核对（留一法）。'
                                  '图块位置来自图层相关，不是逐个地物的同名点，仍需现场核对。'}
    if quality['medianErrorM'] > 6 or quality['p95ErrorM'] > 12:
        raise BlockFitError(f'图块留出核查误差过大（P95 {quality["p95ErrorM"]} m）', len(pairs))
    return reg, quality


# Finer blocks first retry coarse measurements under the same held-out
# limits. Remaining unconfirmed poses also get a last chance before a
# refusal offers positions to the teacher.
FINE_BLOCK_PASSES = ((6, 384), (7, 384))
AMBIGUITY_MARGIN = .05
METHOD_ORDER = ('global_seed', 'global_search_street', 'global_search_layer', 'global_search_road')
MINIMUM_SUPPORT = .2
OFFERED_PLACES = 3
# A searched pose stands on the agreement of the sheet with the place. One
# layer alone (say, only path ink) can align a self-consistent pattern at a
# slightly wrong scale: blocks of that one layer share the bias, so their
# held-out check cannot see it. Such a fit is offered, not applied.
MINIMUM_POSE_LAYERS = 2
# Leave-one-landmark-out spread of predicted positions across the sheet.
# A fit resting on a few landmarks in one part of the sheet extrapolates
# the rest; when dropping one landmark moves the far sheet by more than
# this, the held-out error says little about the controls there.
EXTRAPOLATION_LIMIT_M = 12.
MINIMUM_SPREAD_GROUPS = 4
# A map sheet is drawn conformally: equal scale in every direction. Print,
# scan and a roughly flat photo keep that within about one per cent (every
# recorded correct fit, campus photos included, is within 1.25%); an affine
# fit stretched more than this has extrapolated a local cluster of
# landmarks (held out in the same cluster) across the sheet, or bent an
# under-determined scale to fit look-alike ink in one direction.
MAXIMUM_ANISOTROPY = .025


def anisotropy(matrix):
    """Relative difference of the largest and smallest scale of an affine."""
    singular = np.linalg.svd(np.asarray(matrix, float)[:, :2], compute_uv=False)
    return float(singular[0]/singular[1]-1)


def sheet_points(footprint, count=16):
    """The footprint's corners and a grid of points inside it."""
    footprint = np.asarray(footprint, float).reshape(-1, 2)
    low, high = footprint.min(axis=0), footprint.max(axis=0)
    xs, ys = np.meshgrid(np.linspace(low[0], high[0], count), np.linspace(low[1], high[1], count))
    grid = np.column_stack((xs.ravel(), ys.ravel()))
    outline = footprint.astype(np.float32).reshape(-1, 1, 2)
    inside = [cv2.pointPolygonTest(outline, (float(x), float(y)), True) >= -1. for x, y in grid]
    return np.vstack((footprint, grid[inside]))


def extrapolation_spread(quality, footprint):
    """How far leaving out one fitted landmark moves the sheet, in metres.

    Landmarks are grouped by the reference object they measure (a roof's
    corners, a way's samples, a layer block). A similarity fitted to all
    groups is compared, over the whole sheet, with each fit that leaves one
    group out; the 95th percentile over the sheet of the largest movement
    is returned. None when fewer than four groups allow no such estimate.
    """
    groups = {}
    for index, landmark in enumerate(quality.get('landmarks') or []):
        if landmark.get('purpose', 'fit') != 'fit' or 'pixel' not in landmark or 'local' not in landmark:
            continue
        groups.setdefault(landmark.get('referenceId', index), []).append(landmark)
    if len(groups) < MINIMUM_SPREAD_GROUPS:
        return None
    keys = [key for key, members in groups.items() for _ in members]
    pixels = np.array([p['pixel'] for members in groups.values() for p in members], float)
    local = np.array([p['local'] for members in groups.values() for p in members], float)
    points = sheet_points(footprint)
    base = affine_points(_similarity(pixels, local), points)
    worst = np.zeros(len(points))
    for key in groups:
        keep = np.array([k != key for k in keys])
        moved = np.linalg.norm(affine_points(_similarity(pixels[keep], local[keep]), points)-base, axis=1)
        worst = np.maximum(worst, moved)
    return round(float(np.percentile(worst, 95)), 2)


def layer_weights(layers, footprint, matrix, kinds, cells=128):
    """Each compared layer's share of the evidence weight at one pose."""
    kinds = [kind for kind in kinds if kind in layers]
    if not kinds:
        return {}
    matrix = np.asarray(matrix, float)
    ground = math.sqrt(abs(np.linalg.det(matrix[:, :2])))*float(np.hypot(*np.ptp(footprint, axis=0)))
    cell = support_cell(ground, cells)
    drawn, *_ = _draw(layers, footprint, np.diag([1., -1.]) @ matrix[:, :2]/cell, cell, kinds)
    total = sum(weight for _, weight in drawn.values())
    return {kind: round(weight/total, 3) for kind, (_, weight) in drawn.items()} if total else {}


def acceptance_refusal(quality, footprint, layers_support=None, matrix=None):
    """Why a fit that the place supports still cannot be applied, or None.

    `layers_support` holds the per-layer agreement at the fit's pose; it is
    None where no whole-sheet comparison was made (a seeded fallback).
    `matrix` is the fit's global affine.
    """
    if (layers_support is not None and quality.get('method', '').startswith('global_search')
            and len(layers_support) < MINIMUM_POSE_LAYERS):
        return f'位置仅由 {len(layers_support)} 类图层支撑（{"、".join(sorted(layers_support)) or "无"}），比例或旋转偏差无法独立核查'
    if matrix is not None and anisotropy(matrix) > MAXIMUM_ANISOTROPY:
        return f'拟合在不同方向的比例相差 {100*anisotropy(matrix):.0f}%，地图不会如此变形，外推不可靠'
    spread = extrapolation_spread(quality, footprint)
    if spread is not None and spread > EXTRAPOLATION_LIMIT_M:
        return f'同名点集中，去掉单个同名点后图幅位置变化 {spread:.1f} m，外推不可靠'
    return None


class UnreliablePose(ValueError):
    """The whole sheet's linework moves an accepted fit far, unconfirmed.

    `place` is (support, registration, quality) of the accepted fit, still
    offered for a teacher's choice.
    """

    def __init__(self, message, place):
        super().__init__(message)
        self.place = place


# A refined pose may lose at most this much whole-sheet support, and its
# held-out objects may not drift beyond the object-fit gate.
REFINED_SUPPORT_LOSS = AMBIGUITY_MARGIN/2
REFINED_OBJECT_P95_M = 18.
REFINEMENT_LOG_KEYS = ('decision', 'reason', 'pairs', 'scoreBeforeM', 'scoreAfterM', 'gainPercent', 'moveP95M', 'moveMaxM',
                       'scalePercent', 'rotationDeg', 'halfAgreementM', 'layerGainsM', 'sensitivityM', 'correction')


def dense_refinement(image, cartography, result, reference, reg, quality):
    """Final whole-sheet linework check of an accepted fit (`pose_refinement`).

    Returns the registration and quality to apply: the accepted fit, or its
    refined pose when the dense correction is confirmed by its own held-out
    fits and still passes the acceptance checks (whole-sheet support,
    supporting layers, extrapolation, held-out objects). Raises
    UnreliablePose when the correction shows the accepted pose to be
    unreliable. Matching uses only the sheet and the place's reference.
    """
    from .pose_refinement import refine, reference_linework, sheet_linework
    layers, footprint, _ = sheet_evidence(image, cartography, result)
    sheet = sheet_linework(cartography, footprint, layers, reg.affine)
    refined, report = refine(reg, footprint, sheet, reference_linework(reference))
    del sheet
    log = {key: report[key] for key in REFINEMENT_LOG_KEYS if key in report}
    if report['decision'] == 'keep':
        return reg, {**quality, 'denseRefinement': log}
    value, _ = support(layers, footprint, reference, reg.affine)
    if report['decision'] == 'unreliable':
        raise UnreliablePose(f'整幅原图线划与参照对齐后图幅移动 {report["moveP95M"]:.0f} m（P95），且未通过分半、分图层核查，'
                             '原配准不可靠', (value, reg, {**quality, 'denseRefinement': log}))
    refined_value, refined_layers = support(layers, footprint, reference, refined.affine)
    log['supportBefore'], log['supportAfter'] = round(value, 4), round(refined_value, 4)
    checks = [p for p in quality.get('landmarks') or [] if p.get('purpose') == 'validation'
              and p.get('kind') != 'road_centerline' and 'pixel' in p and 'local' in p]
    if checks:
        # Road samples were paired at the accepted pose; held-out objects
        # (roof corners, outlines) are measured independently of it.
        errors = np.linalg.norm(refined.forward([p['pixel'] for p in checks], strict=False)[0]
                                - np.array([p['local'] for p in checks], float), axis=1)
        log['heldOutObjectP95M'] = round(float(np.percentile(errors, 95)), 2)
    if refined_value < max(MINIMUM_SUPPORT, value-REFINED_SUPPORT_LOSS):
        reason = 'support'
    elif log.get('heldOutObjectP95M', 0) > max(REFINED_OBJECT_P95_M, quality.get('heldOutP95ErrorM', quality.get('p95ErrorM', 0))):
        reason = 'held_out_objects'
    else:
        reason = acceptance_refusal(quality, footprint, refined_layers, refined.affine)
    if reason:
        log.update(decision='keep', reason=f'refined_pose_failed:{reason}')
        return reg, {**quality, 'denseRefinement': log}
    return refined, {**quality, 'denseRefinement': {**log, 'originalAffine': np.round(reg.affine, 8).tolist()}}


class AmbiguousPlace(ValueError):
    """A refusal that still found fitted places a teacher can tell apart.

    `places` holds (support, registration, quality), best supported first,
    one per distinct position. None is applied automatically.
    """

    def __init__(self, message, places):
        super().__init__(message)
        self.places = places


def distinct_places(attempts, evidence, reference, limit=OFFERED_PLACES, checked=()):
    """The best-supported distinct positions among fits, for a teacher's choice.

    Each fit counts under the image evidence that supports it best, since a
    retry may have found it with path ink the plain palette lacks. Within a
    position the most precise fit with a full held-out check represents it.
    Positions of `checked` fits (seeded fits that passed their own held-out
    object check) are always offered, beyond `limit` and whatever their
    support: a coarse whole-sheet comparison must not hide them.
    """
    scored = []
    for reg, quality in attempts:
        value = max(support(layers, footprint, reference, reg.affine)[0] for layers, footprint in evidence)
        if value >= MINIMUM_SUPPORT or any(reg is other for other, _ in checked):
            scored.append((value, reg, quality))
    scored.sort(key=lambda item: -item[0])
    footprint = evidence[0][1]
    places = []
    for value, reg, quality in scored:
        place = next((members for members in places
                      if poses_agree(members[0][1].affine, reg.affine, footprint, coarse=True)), None)
        if place is None:
            places.append([(value, reg, quality)])
        else:
            place.append((value, reg, quality))
    chosen = []
    kept = [index < limit or any(reg is other for _, reg, _ in members for other, _ in checked)
            for index, members in enumerate(places)]
    for members in [members for members, keep in zip(places, kept) if keep]:
        # A much less supported fit has misaligned the place, however well
        # its own landmarks agree; it cannot represent the place.
        poses = [{'score': value, 'matrix': reg.affine} for value, reg, _ in members]
        members = [item for item in members if not outdone(item[1].affine, item[0], poses, footprint)]
        value, reg, quality = min(members, key=lambda item: (bool(item[2].get('thinValidation')), item[2].get('p95ErrorM', np.inf)))
        spread = extrapolation_spread(quality, footprint)
        chosen.append((value, reg, quality if spread is None else {**quality, 'extrapolationSpreadM': spread}))
    return sorted(chosen, key=lambda item: -item[0])


def choose(attempts, layers, footprint, reference, preferred=(), searched=(), controls=(), scorer=None):
    """Pick the candidate whose final pose the sheet supports best.

    Refuse when a different pose is supported almost as well, or when no
    pose is supported at all: a refusal asks the teacher for a few
    corresponding points, a wrong fit silently misplaces every control.
    A `preferred` attempt (an established method) that agrees with the
    winner is returned instead, keeping earlier results unchanged.
    """
    score = scorer or (lambda matrix: support(layers, footprint, reference, matrix))
    scored = []
    for reg, quality in attempts:
        value, layers_support = score(reg.affine)
        scored.append((value, reg, quality, layers_support))
    if not scored:
        raise ValueError('没有可核查的候选配准')
    scored.sort(key=lambda item: -item[0])
    best_value, best, best_quality, _ = scored[0]
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
    # place. There, an established result wins; otherwise independently
    # checked objects can expose a much less precise fit of the same place.
    # Paving centre lines can sit beside the mapped ways: prefer measured
    # coverage only among fits whose held-out error remains comparable.
    # Abstract block centres must not shrink a well-checked road network.
    same_place = [(reg, quality) for _, reg, quality, _ in scored if poses_agree(best.affine, reg.affine, footprint)]
    same_place += [(reg, quality) for reg, quality in preferred
                   if poses_agree(best.affine, reg.affine, footprint) and all(reg is not r for r, _ in same_place)]
    controls = np.asarray(controls, float).reshape(-1, 2)
    object_checks = [quality['p95ErrorM'] for _, quality in same_place
                     if quality.get('method', '').startswith('global_seed')
                     and quality.get('validationObjectCount', 0) >= 3 and not quality.get('thinValidation')]
    precision_limit = max(15., 2*min(object_checks)) if object_checks else np.inf
    order = lambda item: (item[1].get('p95ErrorM', 0) > precision_limit,
                          not any(item[0] is reg for reg, _ in preferred),
                          bool(item[1].get('thinValidation')),
                          -int(item[0].forward(controls)[1].sum()) if len(controls) else 0,
                          next((i for i, prefix in enumerate(METHOD_ORDER) if item[1].get('method', '').startswith(prefix)),
                               len(METHOD_ORDER)))
    unsafe = []
    for best, best_quality in sorted(same_place, key=order):
        chosen_value, chosen_layers = score(best.affine)
        rivals = [value for value, reg, *_ in scored if not poses_agree(best.affine, reg.affine, footprint)]
        rivals += [candidate['score'] for candidate in searched
                   if not poses_agree(best.affine, candidate['matrix'], footprint, coarse=True)]
        required_margin = AMBIGUITY_MARGIN*(2 if best_quality.get('thinValidation') else 1)
        chosen_margin = chosen_value-max(rivals, default=0.)
        if (chosen_value >= MINIMUM_SUPPORT and chosen_margin >= required_margin
                and not outdone(best.affine, chosen_value, searched, footprint)):
            # The place is settled; this fit must also be checkable on its
            # own. Another fit of the same place may still qualify.
            reason = acceptance_refusal(best_quality, footprint, chosen_layers, best.affine)
            if reason is None:
                break
            unsafe.append(reason)
    else:
        if unsafe:
            raise ValueError(f'{unsafe[0]}，请对照候选位置或补充同名点')
        raise ValueError('候选拟合未通过最终位置核查，请补充同名点')
    best_quality = dict(best_quality)
    points = sheet_points(footprint)
    chosen_points = affine_points(best.affine, points)
    alternatives = [{'method': quality.get('method'), 'support': round(value, 4),
                     'agreesWithChosen': bool(poses_agree(best.affine, reg.affine, footprint)),
                     'sheetP95DistanceM': round(float(np.percentile(
                         np.linalg.norm(affine_points(reg.affine, points)-chosen_points, axis=1), 95)), 2),
                     'p95ErrorM': quality.get('p95ErrorM'), 'extrapolationSpreadM': extrapolation_spread(quality, footprint),
                     'layerCount': len(layers_support), 'anisotropyPercent': round(100*anisotropy(reg.affine), 2),
                     'affine': np.round(reg.affine, 6).tolist()} for value, reg, quality, layers_support in scored]
    best_quality['poseSupport'] = {'value': round(chosen_value, 4), 'layers': chosen_layers,
                                   'layerWeights': layer_weights(layers, footprint, best.affine, chosen_layers),
                                   'margin': round(chosen_margin, 4), 'alternatives': alternatives}
    # The held-out error measures the fit where it was checked; leaving out
    # single landmarks estimates how far that fit may wander elsewhere on
    # the sheet. The reported P95 combines both.
    best_quality['anisotropyPercent'] = round(100*anisotropy(best.affine), 2)
    spread = extrapolation_spread(best_quality, footprint)
    if spread is not None:
        best_quality['extrapolationSpreadM'] = spread
        if best_quality.get('p95ErrorM') is not None:
            best_quality['heldOutP95ErrorM'] = best_quality['p95ErrorM']
            best_quality['p95ErrorM'] = round(math.hypot(best_quality['p95ErrorM'], spread), 2)
    return best, best_quality


def detailed_choose(attempts, layers, footprint, reference, *, preferred=(), searched=(), controls=()):
    """Resolve coarse ambiguity with finer ink, keeping unconfirmed rivals.

    A fine grid cannot fairly compare a measured fit with a coarse search
    pose. Each unconfirmed place first searches a finer neighbourhood;
    both its original and refined poses remain competitors, even if no
    landmark fit accepted them. Only an already checked fit can be chosen.
    """
    cells, cache = 256, {}

    def score(matrix):
        key = np.asarray(matrix, float).tobytes()
        if key not in cache:
            cache[key] = support(layers, footprint, reference, matrix, cells=cells)
        return cache[key]

    checked = [{**candidate, 'score': score(candidate['matrix'])[0]} for candidate in searched]
    tried = []
    for candidate in sorted(searched, key=lambda candidate: -candidate['score']):
        matrix = candidate['matrix']
        if (any(poses_agree(reg.affine, matrix, footprint, coarse=True) for reg, _ in attempts)
                or any(poses_agree(other, matrix, footprint) for other in tried)):
            continue
        tried.append(matrix)
        scale = math.sqrt(abs(np.linalg.det(np.asarray(matrix)[:, :2])))
        cell = _cell(scale*float(np.hypot(*np.ptp(footprint, axis=0))), cells)
        corners = affine_points(matrix, footprint)
        margin = max(80., 12*cell)
        rasters, origin = reference_rasters(reference, cell, (corners.min(axis=0)-margin, corners.max(axis=0)+margin))
        rasters = {kind: raster for kind, raster in rasters.items() if len(layers[kind]) >= (60 if kind == 'path' else 3)}
        if not rasters:
            continue
        poses = [(scale*1.01**k, _angle(matrix)+delta) for k in range(-4, 5) for delta in np.arange(-3, 3.1, 1)]
        found = _correlate(layers, footprint, rasters, origin, cell, poses, peaks=1)
        for _, _, _, refined in _distinct(found, footprint, 2):
            checked.append({'score': score(refined)[0], 'matrix': refined})
    reg, quality = choose(attempts, layers, footprint, reference, preferred=preferred,
                          searched=checked, controls=controls, scorer=score)
    quality['poseSupport']['resolutionCells'] = cells
    return reg, quality


def register(image, cartography, result, reference, *, angles=NORTH_UP_DEG, attempts=4, established=(), prepared_hints=None):
    """Search and validate poses, expanding image evidence only on refusal."""
    if not reference:
        raise NoEvidence('场地没有可比对的地理参照')
    with memoised(reference):
        return _register(image, cartography, result, reference, angles=angles, attempts=attempts, established=established,
                         prepared_hints=prepared_hints)


def _register(image, cartography, result, reference, *, angles, attempts, established, prepared_hints):
    from .registration_hints import prepare_scale_hints, printed_words, scale_bar, without_text
    if prepared_hints is None:
        cartography, result, prepared_hints = prepare_scale_hints(cartography, result)
    ruler, hint = prepared_hints['ruler'], prepared_hints['scaleBar']
    layers, footprint, extraction = sheet_evidence(image, cartography, result)
    controls = [c['pixel'] for c in result['controls'] if c.get('role', 'control') == 'control']
    hypotheses = [('paving', cartography, result, layers, footprint, extraction)]
    candidates, fitted, failures, attempted, unconfirmed = [], [], [], 0, []
    expanded, searched = False, False
    for angle_set in ((angles, TURNED_DEG) if angles is NORTH_UP_DEG else (angles,)):
        for style, carto, observed, layers, footprint, extraction in hypotheses:
            # Compare all earlier poses again under this same evidence.
            # A new palette must not erase a plausible competing position.
            for candidate in candidates:
                candidate['score'] = round(support(layers, footprint, reference, candidate['matrix'])[0], 4)
            scales = scale_range(footprint, observed['controls'], reference, hint)
            restricted = bool(hint is not None and scales[0] >= hint['metresPerPixel']*.8-1e-9
                              and scales[-1] <= hint['metresPerPixel']*1.25*SCALE_STEP+1e-9)
            compatible = lambda matrix: (not restricted or hint['metresPerPixel']*.75
                                         <= math.sqrt(abs(np.linalg.det(np.asarray(matrix)[:, :2])))
                                         <= hint['metresPerPixel']*1.35)
            # A measured ruler also rules out earlier seeds at an
            # incompatible scale; they must not win or veto a fitting pose.
            preferred = [(reg, quality) for reg, quality in established if compatible(reg.affine)]
            candidates = [candidate for candidate in candidates if compatible(candidate['matrix'])]
            try:
                # A measured scale greatly narrows the sweep. Spend that
                # saving on detail: coarse urban roof patterns otherwise
                # alias and can discard the true pose before refinement.
                found = search(layers, footprint, reference, scales, angle_set,
                               coarse_cells=96 if restricted else 64)
                searched = True
            except NoEvidence as exc:
                found = []
                failures.append(str(exc))
            candidates += found
            unconfirmed += [(candidate['score'], candidate['matrix'], layers, footprint, candidate['proposal'])
                            for candidate in found[:attempts]]
            established_support = [(reg.affine, support(layers, footprint, reference, reg.affine)[0]) for reg, _ in preferred]
            floor = max([value for _, value in established_support], default=-np.inf)
            seeds = [candidate['matrix'] for candidate in found[:attempts]
                     if candidate['score'] >= floor-2*AMBIGUITY_MARGIN
                     and not covered(candidate, established_support, footprint)]
            attempted += len(seeds)
            new, errors = refine(image, carto, observed, reference, seeds, extraction, layers, footprint)
            failures += errors
            if new:
                lead = max(new, key=lambda item: support(layers, footprint, reference, item[0].affine)[0])[0]
                polished, _ = refine(image, carto, observed, reference, [lead.affine], extraction, layers, footprint)
                new += [(reg, {**quality, 'polished': True}) for reg, quality in polished]
            fitted += new
            eligible = preferred+[(reg, quality) for reg, quality in fitted if compatible(reg.affine)]
            chosen = None
            try:
                reg, quality = choose(eligible, layers, footprint, reference, preferred=preferred,
                                      searched=candidates, controls=controls)
            except ValueError as exc:
                refusal = str(exc)
                # Fine, coherent block measurements can distinguish road
                # patterns that alias on the coarse grid. Do this only
                # when this reading produced such a checked fit.
                if any(q.get('leaveOneOut') and not q.get('thinValidation') for _, q in new):
                    try:
                        chosen = detailed_choose(eligible, layers, footprint, reference, preferred=preferred,
                                                 searched=candidates, controls=controls)
                    except ValueError as exc:
                        refusal = str(exc)
            else:
                chosen = reg, quality
            if chosen is not None and chosen[1].get('method') == 'global_search_layer_correlation':
                # Block centres can select a better position than the
                # highest-scoring seed. Refit paving at that selected pose,
                # not just at the earlier coarse poses or the NCC leader.
                from .network_registration import STREET_TYPES
                if sum(f['kind'] == 'road' and f.get('tags', {}).get('highway') in STREET_TYPES for f in reference) >= 9:
                    try:
                        paved = street_paving(carto, footprint)
                        street = road_fit(carto, reference, image, chosen[0].affine, paved, streets_only=True)
                    except ValueError:
                        pass
                    else:
                        attempted += 1
                        fitted.append(street)
                        eligible.append(street)
                        chooser = (detailed_choose if (chosen[1].get('poseSupport') or {}).get('resolutionCells')
                                   else choose)
                        try:
                            chosen = chooser(eligible, layers, footprint, reference, preferred=preferred,
                                             searched=candidates, controls=controls)
                        except ValueError as exc:
                            chosen = None
                            refusal = str(exc)
            if chosen is not None:
                reg, quality = chosen
                quality['globalSearch'] = {
                    'candidates': [{key: candidate[key] for key in ('score', 'metresPerPixel', 'rotationDeg')}
                                   for candidate in candidates],
                    'channels': found[0]['channels'] if found else [], 'attempted': attempted, 'pathEvidence': style,
                    'coarseCells': 96 if restricted else 64,
                    'scaleBar': {**hint, 'used': restricted} if hint else None}
                return reg, quality
            if not expanded:
                expanded = True
                words = printed_words(cartography)
                # Detailed text OCR may recover a ruler whose edges were
                # not detected. Otherwise the fast ruler measurement wins.
                hint = hint or scale_bar(cartography, words)
                clean, features = without_text(cartography, result['features'], words+ruler,
                                               excluded_boxes=[hint['pixelBox']] if hint else [])
                filtered = {**result, 'features': features}
                base, outline, normal = sheet_evidence(image, clean, filtered)
                if clean is not cartography:
                    hypotheses.append(('text_filtered_paving', clean, filtered, base, outline, normal))
                previous_paths = [base['path']]
                variants = [(hypotheses[-1][0], base['path'], normal)]
                for linear_ink in (False, True):
                    try:
                        extra = expanded_paths(clean, outline, linear_ink=linear_ink)
                    except NoEvidence:
                        continue
                    pixels, _, ratio, diagnostics = extra
                    paths = pixels/ratio
                    if any(not different_paths(paths, previous, ratio) for previous in previous_paths):
                        continue
                    previous_paths.append(paths)
                    augmented = {**base, 'path': paths}
                    hypotheses.append((diagnostics['pathEvidence'], clean, filtered, augmented, outline, extra))
                    variants.append((diagnostics['pathEvidence'], paths, extra))
                # Forest-style sheets print roofs in the same black as their
                # paths, fences and symbols. Wide path dashes, enlarged
                # symbols or legend swatches can survive the roof opening and
                # then outvote the true place, where none of them are roofs.
                # Let the sheet's water and ways place it without them; the
                # widest path reading is the only extra one tried.
                plain = [f for f in features if f.get('ink') != 'black']
                if len(plain) < len(features):
                    unroofed = {**filtered, 'features': plain}
                    roofs = [np.asarray(f['points'], float) for f in plain if f['kind'] == 'building']
                    for style, paths, extra in variants[:1]+variants[1:][-1:]:
                        hypotheses.append((style+'_without_black_roofs', clean, unroofed,
                                           {**base, 'building': roofs, 'path': paths}, outline, extra))
                # Paving printed in a tan close to the open land it
                # crosses is lost by every reading above. Tried last, so
                # sheets that any earlier reading places are unaffected.
                try:
                    extra = expanded_paths(clean, outline, darker_than_fill=True)
                except NoEvidence:
                    pass
                else:
                    paths = extra[0]/extra[2]
                    if all(different_paths(paths, previous, extra[2]) for previous in previous_paths):
                        hypotheses.append((extra[3]['pathEvidence'], clean, filtered,
                                           {**base, 'path': paths}, outline, extra))
    detail = '；'+'；'.join(dict.fromkeys(failures))[:160] if failures and not fitted else ''
    if not searched:
        raise NoEvidence('参照与原图没有共同的建筑、水体或道路可供搜索'+detail)
    offered, tried = list(fitted), []
    for score, matrix, layers, footprint, _ in sorted(unconfirmed, key=lambda item: -item[4]):
        if len(tried) > OFFERED_PLACES:
            break
        if (not compatible(matrix) or any(poses_agree(other, matrix, footprint, coarse=True) for other in tried)
                or covered({'score': score, 'matrix': matrix},
                           [(reg.affine, support(layers, footprint, reference, reg.affine)[0]) for reg, _ in fitted
                            if poses_agree(reg.affine, matrix, footprint, coarse=True)], footprint)):
            continue
        tried.append(matrix)
        for blocks, fine_cells in FINE_BLOCK_PASSES:
            try:
                offered.append(layer_correlation_register(layers, footprint, reference, matrix, blocks=blocks, fine_cells=fine_cells))
                break
            except ValueError:
                pass
    evidence = [(h[3], h[4]) for h in hypotheses]
    places = distinct_places(preferred+[(reg, quality) for reg, quality in offered if compatible(reg.affine)],
                             evidence, reference, checked=preferred)
    approximate = unconfirmed_place(unconfirmed, places, evidence, reference, compatible)
    if approximate is not None:
        places.append(approximate)
    if places:
        raise AmbiguousPlace(refusal+detail, places)
    raise ValueError(refusal+detail)


def unconfirmed_place(unconfirmed, places, evidence, reference, compatible=lambda matrix: True):
    """The best searched pose that no offered fit accounts for, as an approximate place.

    A sheet can match its place clearly while no landmark fit holds there
    (sparse roofs, paths drawn off the mapped ways). Its coarse search pose
    is still the most useful starting point for a teacher; it is offered
    marked as unchecked, after the fitted places, never applied by itself.
    """
    from .geometry import Registration
    # As the search ranks its proposals (`proposal`, 128 cells across).
    for score, matrix, layers, footprint, _ in sorted(unconfirmed, key=lambda item: -item[4]):
        if not compatible(matrix):
            continue
        # A fit within a search pose's coarse error accounts for it unless
        # the sheet supports the pose clearly better: that fit has then
        # misaligned the place, and the pose itself is worth a look.
        if covered({'score': score, 'matrix': matrix}, [(reg.affine, value) for value, reg, _ in places], footprint):
            return None
        value = max(support(other_layers, other_footprint, reference, matrix)[0] for other_layers, other_footprint in evidence)
        if value < MINIMUM_SUPPORT:
            return None
        quality = {'state': 'needs_review', 'method': 'global_search_unconfirmed', 'approximate': True,
                   'fitCount': 0, 'validationCount': 0, 'medianErrorM': None, 'p95ErrorM': None, 'landmarks': [],
                   'anisotropyPercent': round(100*anisotropy(matrix), 2),
                   'validationScope': '全局搜索的粗略位置：整幅原图与此处吻合，但没有地物拟合能核查它，位置可能偏差数十米。'
                                      '叠图核对后请录入同名点完成配准。'}
        return value, Registration(np.asarray(matrix, float)), quality
    return None
