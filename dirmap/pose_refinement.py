"""Dense final refinement of an accepted registration.

Every accepted fit rests on a few dozen landmarks: roof corners, samples
of some ways or a handful of layer blocks. Landmarks that share one bias
(all blocks of one path layer, ways checked only across their direction)
let a fit pass its held-out checks with a scale or rotation error that
grows towards the sheet edges. The whole sheet's linework carries far
more evidence than those landmarks. Starting from the accepted pose, the
sheet's thin dark ink, paving centre lines, roof outlines and water edges
are aligned with the place's way centre lines, building outlines and water
edges: each sheet line sample is paired with the nearest reference line
running the same way, and the other way round, and their distance across
the line is minimised robustly by a similarity. (A further affine stage
was tried; its extra freedom fitted unmapped ink and worsened P95 errors.)

The correction is applied on top of the accepted registration, so measured
local mesh corrections remain. It is adopted only when it improves the
whole sheet's agreement clearly and is confirmed by what partial fits left
out: a fit to one half of the sheet must place the sheet as the full fit
does, and a fit without one kind of linework must not make that kind agree
worse, while some held-out kind agrees better. A correction that moves
the sheet far and improves agreement, but fails these checks, means the
accepted pose itself is unreliable.
"""
import math

import cv2
import numpy as np
from scipy.spatial import cKDTree

from .geometry import Registration, affine_points

# Sheet linework -> the reference outlines it can show.
PAIRS = (('ink', ('road', 'building')), ('paving', ('road',)), ('roof', ('building',)), ('water', ('water',)))
TOLERANCES_M = (30., 20., 13., 8., 5.)
SCORE_CAP_M = 4.
# Line samples pair only with lines running within this angle.
PAIR_DEGREES = 30.
MAXIMUM_POINTS = 60000
# The working raster for the sheet's ink: about this many metres a pixel.
INK_CELL_M = .8
UNPRINTED_WAYS = ('motorway', 'motorway_link', 'proposed', 'construction', 'platform', 'elevator')
# Adoption: the whole-sheet gain (per cent of the capped across-line
# distance); the largest loss (m) allowed for a held-out kind of linework
# and the least gain that confirms the correction; the largest P95
# distance over the sheet between a half-sheet fit and the full one. A
# rejected correction that moves the sheet this far (P95, m) makes the
# accepted pose unreliable.
ADOPT_GAIN_PERCENT = 3.
LAYER_LOSS_M = .05
LAYER_CONFIRMATION_M = .02
HALF_AGREEMENT_M = 10.
UNRELIABLE_MOVE_M = 15.
IDENTITY = np.array([[1., 0., 0.], [0., 1., 0.]])


def metres_per_pixel(matrix):
    return math.sqrt(abs(np.linalg.det(np.asarray(matrix, float)[:, :2])))


def _polyline(points, step, closed=False):
    """Samples every `step` along a polyline, with their unit directions."""
    points = np.asarray(points, float).reshape(-1, 2)
    if closed and len(points) > 2:
        points = np.vstack((points, points[:1]))
    samples, directions = [], []
    for a, b in zip(points[:-1], points[1:]):
        length = float(np.linalg.norm(b-a))
        if length < 1e-6:
            continue
        count = max(1, int(length/step))
        samples.append(a+(b-a)*(np.arange(count)[:, None]/count))
        directions.append(np.repeat([(b-a)/length], count, axis=0))
    if not samples:
        return np.empty((0, 2)), np.empty((0, 2))
    return np.vstack(samples), np.vstack(directions)


def _thinned(points, cell, *extra):
    """One point per `cell`-sized square (deterministic), with matching rows of `extra`."""
    points = np.asarray(points, float).reshape(-1, 2)
    if len(points):
        _, index = np.unique(np.floor(points/cell).astype(np.int64), axis=0, return_index=True)
        index = np.sort(index)
        if len(index) > MAXIMUM_POINTS:
            index = np.sort(np.random.default_rng(0).choice(index, MAXIMUM_POINTS, replace=False))
    else:
        index = np.zeros(0, int)
    return (points[index], *(np.asarray(e)[index] for e in extra)) if extra else points[index]


def _skeleton_points(mask, ratio):
    from .network_registration import thin
    yy, xx = np.nonzero(thin(mask))
    return (np.column_stack((xx, yy))+.5)/ratio


def sheet_linework(image, footprint, layers, matrix):
    """Thin linework of the sheet in working pixels, by kind.

    `image` is the cartographic working image (BGR), `layers` the sheet's
    recognised layers (roof and water polygons, paving centre-line pixels)
    and `matrix` the accepted pose, used only for the ground size of a pixel.
    Ink is read at about INK_CELL_M a pixel, so a large sheet costs no more
    than a sheet of its ground size needs.
    """
    mpp = metres_per_pixel(matrix)
    ratio = min(1., mpp/INK_CELL_M)
    small = cv2.resize(image, None, fx=ratio, fy=ratio, interpolation=cv2.INTER_AREA) if ratio < 1 else image
    inside = np.zeros(small.shape[:2], np.uint8)
    cv2.fillPoly(inside, [np.rint(np.asarray(footprint, float)*ratio).astype(np.int32)], 1)
    hi, lo = small.max(axis=2), small.min(axis=2)
    dark = ((hi < 70) & (hi.astype(np.int16)-lo < 35)).astype(np.uint8)
    del hi, lo
    # Black roof fills and lettering blocks are wider than a printed line
    # (well under 5 m on the ground); keep only strokes an opening removes.
    size = int(np.clip(round(5/(mpp/ratio)), 3, 61))
    solid = cv2.morphologyEx(dark, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size)))
    dark[cv2.dilate(solid, np.ones((3, 3), np.uint8)) > 0] = 0
    del solid
    # The water colour rule of recognition; its edges are water outlines.
    b, g, r = (channel.astype(np.int16) for channel in cv2.split(small))
    hue = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)[..., 0]
    blue = ((b-r > 24) & (g-r > 15) & (g > 95) & (hue > 78) & (hue < 111)).astype(np.uint8)
    del b, g, r, hue, small
    blue &= ~cv2.erode(blue, np.ones((3, 3), np.uint8))
    result = {'ink': _skeleton_points(dark & inside, ratio), 'water': _skeleton_points(blue & inside, ratio)}
    del dark, blue
    step = max(1., INK_CELL_M/mpp)
    result['water'] = np.vstack([result['water']]+[_polyline(p, step, closed=True)[0] for p in layers.get('water', [])])
    roofs = [_polyline(p, step, closed=True)[0] for p in layers.get('building', [])]
    result['roof'] = np.vstack(roofs) if roofs else np.empty((0, 2))
    result['paving'] = np.asarray(layers.get('path', []), float).reshape(-1, 2)
    for kind, points in result.items():
        cells = np.floor(points*ratio).astype(int)
        ok = (cells[:, 0] >= 0) & (cells[:, 1] >= 0) & (cells[:, 0] < inside.shape[1]) & (cells[:, 1] < inside.shape[0])
        ok[ok] = inside[cells[ok, 1], cells[ok, 0]] > 0
        result[kind] = _thinned(points[ok], step)
    return result


def reference_linework(reference, step=1.):
    """Way centre lines, building outlines and water edges: (points, directions) in local metres."""
    parts = {'road': [], 'building': [], 'water': []}
    for feature in reference:
        kind, points = feature['kind'], feature['points']
        if kind == 'road' and feature.get('tags', {}).get('highway') not in UNPRINTED_WAYS:
            parts['road'].append(_polyline(points, step))
        elif kind in ('building', 'water'):
            parts[kind].append(_polyline(points, step, closed=True))
        elif kind == 'waterway':
            parts['water'].append(_polyline(points, step))
    return {kind: (np.vstack([p for p, _ in values]), np.vstack([d for _, d in values])) if values
            else (np.empty((0, 2)), np.empty((0, 2))) for kind, values in parts.items()}


def _line_directions(points, neighbours=8, straightness=.3):
    """Direction of the local line at each point, and whether it is line-like."""
    if len(points) < neighbours:
        return np.zeros((len(points), 2)), np.zeros(len(points), bool)
    _, near = cKDTree(points).query(points, k=neighbours)
    offsets = points[near]-points[near].mean(axis=1, keepdims=True)
    xx, xy, yy = (np.sum(offsets[..., 0]**2, axis=1), np.sum(offsets[..., 0]*offsets[..., 1], axis=1),
                  np.sum(offsets[..., 1]**2, axis=1))
    angle = .5*np.arctan2(2*xy, xx-yy)
    spread = np.sqrt((xx-yy)**2+4*xy**2)
    small, large = (xx+yy-spread)/2, (xx+yy+spread)/2
    return np.column_stack((np.cos(angle), np.sin(angle))), small <= straightness*np.maximum(large, 1e-9)


def _inverse(matrix):
    linear = np.linalg.inv(matrix[:, :2])
    return np.column_stack((linear, -linear @ matrix[:, 2]))


def _compose(correction, affine):
    return np.column_stack((correction[:, :2] @ affine[:, :2], correction[:, :2] @ affine[:, 2]+correction[:, 2]))


def corrected(reg, correction):
    """`reg` followed by a local similarity `correction` (mesh included)."""
    correction = np.asarray(correction, float)
    return Registration(_compose(correction, reg.affine), reg.source,
                        affine_points(correction, reg.target) if len(reg.target) else reg.target, regularization=0)


def unrefined(reg, report):
    """The accepted registration a refinement `report` started from."""
    return corrected(reg, _inverse(np.asarray(report['correction'], float)))


def _solve(source, target, normal, weight, centre):
    """Similarity minimising weighted across-line distances, with a weak point-to-point term."""
    q, o = source-centre, target-centre
    nx, ny = normal[:, 0], normal[:, 1]
    zero, one = np.zeros(len(q)), np.ones(len(q))
    line = np.column_stack((nx*q[:, 0]+ny*q[:, 1], ny*q[:, 0]-nx*q[:, 1], nx, ny))
    px = np.column_stack((q[:, 0], -q[:, 1], one, zero))
    py = np.column_stack((q[:, 1], q[:, 0], zero, one))
    root = np.sqrt(weight)
    weak = root*.2
    design = np.vstack((line*root[:, None], px*weak[:, None], py*weak[:, None]))
    rhs = np.concatenate((np.sum(normal*o, axis=1)*root, o[:, 0]*weak, o[:, 1]*weak))
    a, b, x, y = np.linalg.lstsq(design, rhs, rcond=None)[0]
    linear = np.array([[a, -b], [b, a]])
    return np.column_stack((linear, np.array([x, y])+centre-linear @ centre))


class _Problem:
    """Sheet line samples (in the accepted local frame) and reference lines, per kind."""

    def __init__(self, reg, footprint, sheet, osm):
        footprint = np.asarray(footprint, float)
        low, high = footprint.min(axis=0), footprint.max(axis=0)
        # Sheet margins hold legends and titles; keep to the inner sheet.
        margin = .06*(high-low)
        local_outline = reg.forward(footprint, strict=False)[0]
        self.corners = local_outline
        self.centre = local_outline.mean(axis=0)
        self.mask_origin = local_outline.min(axis=0)-2
        span = np.ceil(local_outline.max(axis=0)-self.mask_origin+2).astype(int)+1
        mask = np.zeros((span[1], span[0]), np.uint8)
        cv2.fillPoly(mask, [np.rint(local_outline-self.mask_origin).astype(np.int32)], 1)
        erosion = max(3, int(round(float(np.linalg.norm(margin))*metres_per_pixel(reg.affine)*.5)))
        self.mask = cv2.erode(mask, np.ones((erosion, erosion), np.uint8))
        self.cos = math.cos(math.radians(PAIR_DEGREES))
        self.pairs = []
        for kind, targets in PAIRS:
            pixels = np.asarray(sheet.get(kind, np.empty((0, 2))), float).reshape(-1, 2)
            pixels = pixels[np.all((pixels > low+margin) & (pixels < high-margin), axis=1)]
            chosen = [osm[t] for t in targets if t in osm and len(osm[t][0])]
            if len(pixels) < 200 or not chosen:
                continue
            reference = np.vstack([p for p, _ in chosen])
            heading = np.vstack([d for _, d in chosen])
            visible = self.inside(reference)
            reference, heading = _thinned(reference[visible], 1., heading[visible])
            local = reg.forward(pixels, strict=False)[0]
            local_heading, straight = _line_directions(local)
            pixels, local, local_heading = pixels[straight], local[straight], local_heading[straight]
            if len(pixels) < 200 or len(reference) < 200:
                continue
            self.pairs.append(self._pair(kind, pixels, local, local_heading, reference, heading,
                                         affine_points(_inverse(reg.affine), reference)))

    @staticmethod
    def _pair(kind, pixels, local, heading, reference, reference_heading, reference_pixels):
        return {'kind': kind, 'pixels': pixels, 'local': local, 'heading': heading, 'tree': cKDTree(local),
                'reference': reference, 'referenceHeading': reference_heading, 'referencePixels': reference_pixels,
                'referenceNormal': np.column_stack((-reference_heading[:, 1], reference_heading[:, 0])),
                'referenceTree': cKDTree(reference)}

    def restricted(self, keep_pixel=None, kinds=None):
        """The same problem with only some kinds, or only a part of the sheet (pixel predicate)."""
        other = object.__new__(_Problem)
        other.__dict__.update({k: v for k, v in self.__dict__.items() if k != 'pairs'})
        other.pairs = []
        for pair in self.pairs:
            if kinds is not None and pair['kind'] not in kinds:
                continue
            sheet = keep_pixel(pair['pixels']) if keep_pixel else np.ones(len(pair['pixels']), bool)
            reference = keep_pixel(pair['referencePixels']) if keep_pixel else np.ones(len(pair['reference']), bool)
            if sheet.sum() < 100 or reference.sum() < 100:
                continue
            other.pairs.append(self._pair(pair['kind'], pair['pixels'][sheet], pair['local'][sheet], pair['heading'][sheet],
                                          pair['reference'][reference], pair['referenceHeading'][reference],
                                          pair['referencePixels'][reference]))
        return other

    def inside(self, local):
        cells = np.floor(np.asarray(local)-self.mask_origin).astype(int)
        ok = (cells[:, 0] >= 0) & (cells[:, 1] >= 0) & (cells[:, 0] < self.mask.shape[1]) & (cells[:, 1] < self.mask.shape[0])
        result = np.zeros(len(cells), bool)
        result[ok] = self.mask[cells[ok, 1], cells[ok, 0]] > 0
        return result

    def _nearest(self, tree, size, queries, query_heading, headings, limit):
        """Index of the nearest point running the same way within `limit`, or -1."""
        distance, index = tree.query(queries, k=6, distance_upper_bound=limit)
        valid = index < size
        safe = np.where(valid, index, 0)
        aligned = np.abs(np.einsum('nkj,nj->nk', headings[safe], query_heading)) >= self.cos
        distance = np.where(valid & aligned, distance, np.inf)
        best = np.argmin(distance, axis=1)
        rows = np.arange(len(queries))
        return np.where(np.isfinite(distance[rows, best]), safe[rows, best], -1)

    def correspondences(self, correction, tolerance):
        """Sheet->reference and reference->sheet pairs under `correction`.

        Each group is (sheet local points, reference points, reference
        normals, sheet pixels, number of samples that looked for a pair).
        """
        inverse = _inverse(correction)
        linear = correction[:, :2]/metres_per_pixel(correction)
        groups = []
        for pair in self.pairs:
            moved = affine_points(correction, pair['local'])
            j = self._nearest(pair['referenceTree'], len(pair['reference']), moved, pair['heading'] @ linear.T,
                              pair['referenceHeading'], tolerance)
            hit = j >= 0
            forward = (pair['local'][hit], pair['reference'][j[hit]], pair['referenceNormal'][j[hit]], pair['pixels'][hit])
            visible = self.inside(affine_points(inverse, pair['reference']))
            reference = pair['reference'][visible]
            j = self._nearest(pair['tree'], len(pair['local']), affine_points(inverse, reference),
                              pair['referenceHeading'][visible] @ np.linalg.inv(linear).T, pair['heading'],
                              tolerance/metres_per_pixel(correction))
            hit = j >= 0
            backward = (pair['local'][j[hit]], reference[hit], pair['referenceNormal'][visible][hit], pair['pixels'][j[hit]])
            groups += [(*side, total) for side, total in ((forward, len(moved)), (backward, len(reference))) if len(side[0])]
        return groups

    def matches(self, correction, tolerance):
        """Weighted pairs: (source, target, normal, weight, pixels)."""
        rows = []
        for source, target, normal, pixels, _ in self.correspondences(correction, tolerance):
            across = np.abs(np.sum((affine_points(correction, source)-target)*normal, axis=1))
            # Each kind and direction counts by the square root of its
            # matches: dense ink must not silence a few water edges, and a
            # few must not outvote it either.
            weight = 1/(1+(across/(tolerance/4))**2)/math.sqrt(len(source))
            rows.append((source, target, normal, weight, pixels))
        if not rows:
            return None
        return tuple(np.concatenate(column) for column in zip(*rows))

    def score(self, correction, cap=SCORE_CAP_M):
        """Mean capped across-line distance (m) over all samples, two-way; None without pairs."""
        values, weights = [], []
        for source, target, normal, _, total in self.correspondences(correction, cap):
            across = np.minimum(np.abs(np.sum((affine_points(correction, source)-target)*normal, axis=1)), cap)
            values.append((across.sum()+cap*(total-len(across)))/total)
            weights.append(math.sqrt(total))
        return float(np.average(values, weights=weights)) if values else None


def _correct(problem, tolerances):
    """Similarity correction from the accepted pose, narrowing the pairing tolerance."""
    correction, found = IDENTITY, None
    for tolerance in tolerances:
        for _ in range(12):
            found = problem.matches(correction, tolerance)
            if found is None or len(found[0]) < 50:
                return None, None
            updated = _solve(*found[:4], problem.centre)
            shift = np.abs(affine_points(updated, problem.corners)-affine_points(correction, problem.corners)).max()
            correction = updated
            if shift < .05:
                break
    return correction, found


def _sheet_grid(footprint, count=12):
    footprint = np.asarray(footprint, float)
    low, high = footprint.min(axis=0), footprint.max(axis=0)
    xs, ys = np.meshgrid(np.linspace(low[0], high[0], count), np.linspace(low[1], high[1], count))
    grid = np.column_stack((xs.ravel(), ys.ravel()))
    outline = footprint.astype(np.float32).reshape(-1, 1, 2)
    keep = [cv2.pointPolygonTest(outline, (float(x), float(y)), False) >= 0 for x, y in grid]
    return np.vstack((footprint, grid[keep]))


def _held_out(problem, correction, footprint, points, tolerances):
    """Corrections fitted without one half of the sheet, or without one kind of linework.

    `halfAgreementM` is the P95 distance over the sheet between each
    half-sheet fit and the full correction; `layerGainsM` each held-out
    kind's agreement gain (m, positive is better than the accepted pose)
    under the fit to the other kinds.
    """
    footprint = np.asarray(footprint, float)
    low, high = footprint.min(axis=0), footprint.max(axis=0)
    axis = int(np.argmax(high-low))
    middle = (low[axis]+high[axis])/2
    base = affine_points(correction, points)
    report = {'halfAgreementM': [], 'layerGainsM': {}}
    for side in (lambda p: p[:, axis] < middle, lambda p: p[:, axis] >= middle):
        train = problem.restricted(keep_pixel=side)
        partial = _correct(train, tolerances)[0] if train.pairs else None
        report['halfAgreementM'].append(None if partial is None else round(float(np.percentile(
            np.linalg.norm(affine_points(partial, points)-base, axis=1), 95)), 2))
    kinds = [pair['kind'] for pair in problem.pairs]
    for kind in kinds if len(kinds) >= 2 else ():
        train = problem.restricted(kinds=[k for k in kinds if k != kind])
        test = problem.restricted(kinds=[kind])
        partial = _correct(train, tolerances)[0] if train.pairs else None
        if partial is None or not test.pairs:
            continue
        before, after = test.score(IDENTITY), test.score(partial)
        if before is not None and after is not None:
            report['layerGainsM'][kind] = round(before-after, 3)
    return report


def refine(reg, footprint, sheet, osm, *, tolerances=TOLERANCES_M):
    """Dense correction of an accepted registration, with its adoption decision.

    `sheet` comes from `sheet_linework`, `osm` from `reference_linework`.
    Returns (corrected registration or None, report); report['decision'] is
    'adopt', 'keep' (the accepted pose stands) or 'unreliable'.
    """
    problem = _Problem(reg, footprint, sheet, osm)
    report = {'decision': 'keep', 'pairs': {p['kind']: [len(p['pixels']), len(p['reference'])] for p in problem.pairs}}
    if not problem.pairs:
        report['reason'] = 'no_linework'
        return None, report
    correction, found = _correct(problem, tolerances)
    if correction is None:
        report['reason'] = 'too_few_matches'
        return None, report
    before, after = problem.score(IDENTITY), problem.score(correction)
    points = reg.forward(_sheet_grid(footprint), strict=False)[0]
    movement = np.linalg.norm(affine_points(correction, points)-points, axis=1)
    report.update({
        'scoreBeforeM': round(before, 3), 'scoreAfterM': round(after, 3),
        'gainPercent': round(100*(before-after)/before, 2) if before else 0.,
        'moveP95M': round(float(np.percentile(movement, 95)), 2), 'moveMaxM': round(float(movement.max()), 2),
        'scalePercent': round(100*(metres_per_pixel(correction)-1), 3),
        'rotationDeg': round(math.degrees(math.atan2(correction[1, 0], correction[0, 0])), 3),
        'matches': int(len(found[0])), 'correction': np.round(correction, 8).tolist()})
    refined = corrected(reg, correction)
    if report['gainPercent'] < ADOPT_GAIN_PERCENT:
        report['reason'] = 'small_gain'
        return refined, report
    report.update(_held_out(problem, correction, footprint, points, tolerances))
    layers, halves = report['layerGainsM'], report['halfAgreementM']
    failed = []
    if len(layers) < 2 or min(layers.values()) < -LAYER_LOSS_M or max(layers.values()) < LAYER_CONFIRMATION_M:
        failed.append('held_out_layer')
    if None in halves or max(halves) > HALF_AGREEMENT_M:
        failed.append('held_out_half')
    if not failed:
        report['decision'] = 'adopt'
    else:
        report['reason'] = '+'.join(failed)
        if report['moveP95M'] >= UNRELIABLE_MOVE_M:
            report['decision'] = 'unreliable'
    return refined, report
