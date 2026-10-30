"""Local height surfaces inferred from recorded tracks, with bounded support.

These are relative, noisy track heights, not a surveyed bare-earth DEM.  A
surface never extends beyond the observations' supported triangulation.

Phone GNSS heights drift by several metres within one walk, and recordings
of the same path on different days disagree by up to ~10 m.  One robust
least-squares problem therefore estimates the smooth ground surface together
with a slowly varying height error for every recording, using the places
where recordings overlap or cross.
"""
from collections import defaultdict
import hashlib
import json

import numpy as np
from scipy.ndimage import distance_transform_edt, gaussian_filter
from scipy.sparse import coo_matrix, diags
from scipy.sparse.csgraph import connected_components
from scipy.sparse.linalg import LinearOperator, cg, splu
from scipy.spatial import Delaunay, QhullError, cKDTree

from .geometry import Frame, mercator

SCHEMA = 'shared-track-height-v2'
CELL_M = 10.
MAX_EDGE_M = 160.
MAX_SUPPORT_M = 70.
NEAR_SUPPORT_M = 25.
MAX_GRID_SIDE = 384
MAX_UNKNOWNS = 40_000
SAMPLE_STEP_M = 5.
WALKING_MPS = 1.2
KNOT_SEC = 300.
SESSION_GAP_SEC = 1800.
HEIGHT_SIGMA_M = 2.
CURVATURE_SIGMA = .01  # grade may change by about 0.1 over 10 m
GRADE_SIGMA = 3.  # only settles directions no recording constrains; no slope bias
DRIFT_BEND_SIGMA_M = 1/3
DRIFT_STEP_SIGMA_M = 3.
DATUM_SIGMA_M = 10.


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def spatial_groups(tracks):
    """Separate distant areas even when one recording jumps between them."""
    if not tracks:
        return []
    cells, point_cells = {}, []
    for track in tracks:
        tile = np.floor(mercator(track['points'][:, :2])/400).astype(np.int64)
        indexes = np.empty(len(tile), np.int32)
        for i, key in enumerate(map(tuple, tile)):
            indexes[i] = cells.setdefault(key, len(cells))
        point_cells.append(indexes)
    parent = list(range(len(cells)))
    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for (x, y), i in cells.items():
        for dx, dy in ((1, 0), (0, 1), (1, 1), (1, -1)):
            j = cells.get((x+dx, y+dy))
            if j is not None:
                parent[find(j)] = find(i)
    labels = np.array([find(i) for i in range(len(cells))])
    grouped = defaultdict(list)
    for track, indexes in zip(tracks, point_cells):
        parts = labels[indexes]
        for part in np.unique(parts):
            keep = parts == part
            if keep.sum() >= 3:
                times = track.get('times')
                grouped[int(part)].append({**track, 'points': track['points'][keep],
                                           'times': None if times is None else np.asarray(times)[keep]})
    return list(grouped.values())


def track_times(track, xy):
    """Recorded seconds, or walking-time along the path when a file has none."""
    times = track.get('times')
    if times is not None:
        times = np.asarray(times, float)
        if len(times) == len(xy) and np.isfinite(times).all() and np.ptp(times) > 0:
            return times-times.min()
    travelled = np.r_[0., np.cumsum(np.linalg.norm(np.diff(xy, axis=0), axis=1))]
    return travelled/WALKING_MPS


def grouped_median(values, groups):
    """Median per contiguous non-decreasing group label."""
    order = np.lexsort((values, groups))
    ordered, labels = values[order], groups[order]
    starts = np.flatnonzero(np.r_[True, np.diff(labels) != 0])
    counts = np.diff(np.r_[starts, len(labels)])
    return (ordered[starts+(counts-1)//2]+ordered[starts+counts//2])/2


def observations(track, frame, step=SAMPLE_STEP_M):
    """One observation per few metres walked; a pause counts as a few at most."""
    xy = frame.to_local(track['points'][:, :2])
    heights = track['points'][:, 2]
    times = track_times(track, xy)
    travelled = np.r_[0., np.cumsum(np.linalg.norm(np.diff(xy, axis=0), axis=1))]
    segment = np.floor(travelled/step).astype(np.int64)
    starts = np.flatnonzero(np.r_[True, np.diff(segment) != 0])
    counts = np.diff(np.r_[starts, len(segment)])[:, None]
    position = np.add.reduceat(xy, starts)/counts
    seconds = np.add.reduceat(times, starts)/counts[:, 0]
    middle = grouped_median(heights, np.repeat(np.arange(len(starts)), counts[:, 0]))
    # GPS jitter while standing still keeps adding path length. The same few
    # metres keep at most three observations' weight in one recording.
    _, inverse, repeats = np.unique(np.floor(position/step).astype(np.int64), axis=0, return_inverse=True, return_counts=True)
    weights = np.minimum(1., 3./repeats[inverse.ravel()])
    return {'xy': position, 'z': middle, 't': seconds, 'w': weights}


def overlap_links(clouds, radius=12.):
    """Recordings that share ground: a long shared path or a single crossing.

    Recordings meet in shared 12 m cells, found by sorting rather than by
    comparing every pair, so many walks along one road stay cheap. A link
    only says that the two can be compared; the joint fit weighs the evidence.
    """
    n = len(clouds)
    keys, owners, heights = [], [], []
    for i, cloud in enumerate(clouds):
        cell = np.floor(cloud['xy']/radius).astype(np.int64)
        flat = cell[:, 0]*4_000_037+cell[:, 1]
        unique, inverse = np.unique(flat, return_inverse=True)
        keys.append(unique); owners.append(np.full(len(unique), i))
        heights.append(np.bincount(inverse, weights=cloud['z'])/np.bincount(inverse))
    keys, owners, heights = np.concatenate(keys), np.concatenate(owners), np.concatenate(heights)
    order = np.lexsort((owners, keys))
    keys, owners, heights = keys[order], owners[order], heights[order]
    starts = np.flatnonzero(np.r_[True, np.diff(keys) != 0])
    counts = np.diff(np.r_[starts, len(keys)])
    first, second, difference = [], [], []
    for start, count in zip(starts[counts > 1], counts[counts > 1]):
        # A busy road only needs enough pairs to connect its recordings.
        members = slice(start, start+min(count, 24))
        a, b = np.triu_indices(min(count, 24), 1)
        first.append(owners[members][a]); second.append(owners[members][b])
        difference.append(heights[members][a]-heights[members][b])
    links = []
    if first:
        first, second, difference = np.concatenate(first), np.concatenate(second), np.concatenate(difference)
        pair = first*n+second
        order = np.argsort(pair, kind='stable')
        pair, difference = pair[order], difference[order]
        cuts = np.flatnonzero(np.diff(pair))+1
        for group, values in zip(np.split(pair, cuts), np.split(difference, cuts)):
            median = float(np.median(values))
            spread = float(1.4826*np.median(np.abs(values-median)))
            # A long shared stretch must agree up to a constant, e.g. not a
            # path on a footbridge beside the road below it.
            if abs(median) > 80 or (len(values) >= 8 and spread > 6):
                continue
            links.append((int(group[0]//n), int(group[0] % n)))
    rows = [v for a, b in links for v in (a, b)]
    cols = [v for a, b in links for v in (b, a)]
    adjacency = coo_matrix((np.ones(len(rows)), (rows, cols)), shape=(n, n)).tocsr()
    count, labels = connected_components(adjacency, directed=False)
    return links, count, labels


def drift_knots(times):
    """Piecewise-linear error per recording: a knot every few minutes."""
    order = np.argsort(times, kind='stable')
    ordered = times[order]
    sessions = np.split(np.arange(len(ordered)), np.flatnonzero(np.diff(ordered) > SESSION_GAP_SEC)+1)
    first, second, fraction = np.empty(len(times), np.int64), np.empty(len(times), np.int64), np.zeros(len(times))
    chains, count = [], 0
    for session in sessions:
        start, end = ordered[session[0]], ordered[session[-1]]
        knots = int(np.ceil((end-start)/KNOT_SEC))+1 if end > start else 1
        targets = order[session]
        if knots == 1:
            first[targets] = second[targets] = count
        else:
            position = (ordered[session]-start)/(end-start)*(knots-1)
            index = np.minimum(np.floor(position).astype(np.int64), knots-2)
            first[targets], second[targets], fraction[targets] = count+index, count+index+1, position-index
        chains.append((count, knots))
        count += knots
    return first, second, fraction, chains, count


def _regularization(index, spacing, chains, offset):
    """Thin-plate smoothness for the ground and slow change for each error."""
    rows, cols, values, used = [], [], [], [0]
    def add(columns, coefficients, weight):
        columns = [np.asarray(c).ravel() for c in columns]
        keep = np.all([c >= 0 for c in columns], axis=0)
        count = int(keep.sum())
        row = np.arange(used[0], used[0]+count)
        used[0] += count
        for column, coefficient in zip(columns, coefficients):
            rows.append(row); cols.append(column[keep]); values.append(np.full(count, coefficient*weight))
    bend = 1/(CURVATURE_SIGMA*spacing**2)
    add((index[:, :-2], index[:, 1:-1], index[:, 2:]), (1, -2, 1), bend)
    add((index[:-2], index[1:-1], index[2:]), (1, -2, 1), bend)
    add((index[:-1, :-1], index[:-1, 1:], index[1:, :-1], index[1:, 1:]), (1, -1, -1, 1), bend*np.sqrt(2))
    grade = 1/(GRADE_SIGMA*spacing)
    add((index[:, :-1], index[:, 1:]), (-1, 1), grade)
    add((index[:-1], index[1:]), (-1, 1), grade)
    for start, knots in chains:
        knot = offset+start+np.arange(knots)
        add((knot[:-2], knot[1:-1], knot[2:]), (1, -2, 1), 1/DRIFT_BEND_SIGMA_M)
        add((knot[:-1], knot[1:]), (-1, 1), 1/DRIFT_STEP_SIGMA_M)
        add((knot,), (1,), 1/(DATUM_SIGMA_M*np.sqrt(knots)))
    rows, cols, values = np.concatenate(rows), np.concatenate(cols), np.concatenate(values)
    return coo_matrix((values, (rows, cols)), shape=(used[0], offset+sum(k for _, k in chains))).tocsr()


def solve_component(clouds, origin, spacing, shape, unknown):
    """Ground heights on `unknown` grid nodes plus every recording's error."""
    unknown = unknown & (reach(np.concatenate([c['xy'] for c in clouds]), origin, spacing, shape) <= MAX_SUPPORT_M+spacing)
    nodes = int(unknown.sum())
    index = np.full(shape, -1, np.int64)
    index[unknown] = np.arange(nodes)
    xy = np.concatenate([c['xy'] for c in clouds])
    z = np.concatenate([c['z'] for c in clouds])
    weights = np.concatenate([c['w'] for c in clouds])
    owner = np.concatenate([np.full(len(c['z']), i) for i, c in enumerate(clouds)])
    first, second, fraction, chains = [], [], [], []
    count = 0
    for cloud in clouds:
        a, b, w, own, knots = drift_knots(cloud['t'])
        first.append(a+count); second.append(b+count); fraction.append(w)
        chains.extend((start+count, n) for start, n in own)
        count += knots
    first, second, fraction = np.concatenate(first), np.concatenate(second), np.concatenate(fraction)
    grid = (xy-origin)/spacing
    cell = np.floor(grid).astype(np.int64)
    dx, dy = (grid-cell).T
    x, y = cell.T
    corners = (index[y, x], index[y, x+1], index[y+1, x], index[y+1, x+1])
    if any((c < 0).any() for c in corners):
        raise ValueError('高程观测超出重建网格')
    rows = np.repeat(np.arange(len(z)), 6)
    cols = np.column_stack((*corners, nodes+first, nodes+second)).ravel()
    values = np.column_stack(((1-dx)*(1-dy), dx*(1-dy), (1-dx)*dy, dx*dy, 1-fraction, fraction)).ravel()
    design = coo_matrix((values, (rows, cols)), shape=(len(z), nodes+count)).tocsr()
    prior = _regularization(index, spacing, chains, nodes)
    penalty = (prior.T@prior).tocsc()
    robust = np.ones(len(z))
    factor = solution = None
    for _ in range(4):
        scale = weights*robust/HEIGHT_SIGMA_M**2
        normal = (design.T@diags(scale)@design).tocsc()+penalty
        rhs = design.T@(scale*z)
        if factor is None:
            # Symmetric positive definite: no pivoting keeps the fill small.
            factor = splu(normal, permc_spec='MMD_AT_PLUS_A', diag_pivot_thresh=0., options={'SymmetricMode': True})
            solution = factor.solve(rhs)
        else:
            # Reweighting only changes a few outliers' rows; the first
            # factorization preconditions the later solves.
            solution, status = cg(normal, rhs, x0=solution, rtol=1e-10, maxiter=200,
                                  M=LinearOperator(normal.shape, factor.solve, dtype=float))
            if status:
                solution = splu(normal, permc_spec='MMD_AT_PLUS_A', diag_pivot_thresh=0., options={'SymmetricMode': True}).solve(rhs)
        if not np.isfinite(solution).all():
            raise ValueError('高程重建数值不稳定，请检查轨迹高程')
        residual = design@solution-z
        sigma = max(.7, 1.4826*float(np.median(np.abs(residual))))
        limit = 1.5*sigma
        robust = np.where(np.abs(residual) <= limit, 1., limit/np.maximum(np.abs(residual), 1e-9))
    drift = (1-fraction)*solution[nodes+first]+fraction*solution[nodes+second]
    # The mean of all recordings, not one arbitrary walk, sets the datum.
    shift = float(np.average(drift, weights=weights))
    surface = np.full(shape, np.nan)
    surface[unknown] = solution[:nodes]+shift
    return {'surface': surface, 'drift': drift-shift, 'residual': residual, 'owner': owner, 'xy': xy, 'z': z,
            'sigma': sigma}


def supported_nodes(xy, query, shape):
    """Between observed paths, or close beside one; never a far extrapolation."""
    distance, _ = cKDTree(xy).query(query)
    supported = distance <= NEAR_SUPPORT_M
    unique = np.unique(np.round(xy/5), axis=0)*5
    if len(unique) >= 3 and np.linalg.matrix_rank(unique-unique.mean(axis=0)) == 2:
        try:
            triangles = Delaunay(unique)
        except QhullError:
            triangles = None
        if triangles is not None:
            simplex = triangles.find_simplex(query)
            corners = unique[triangles.simplices[np.maximum(simplex, 0)]]
            edge = np.linalg.norm(corners-np.roll(corners, -1, axis=1), axis=2).max(axis=1)
            supported |= (simplex >= 0) & (edge <= MAX_EDGE_M) & (distance <= MAX_SUPPORT_M)
    return supported.reshape(shape), distance.reshape(shape)


def reach(xy, origin, spacing, shape):
    """Distance from every grid node to the nearest observed cell corner."""
    occupied = np.zeros(shape, bool)
    cell = np.floor((xy-origin)/spacing).astype(np.int64)
    for dx, dy in ((0, 0), (1, 0), (0, 1), (1, 1)):
        occupied[cell[:, 1]+dy, cell[:, 0]+dx] = True
    return distance_transform_edt(~occupied)*spacing


def grid_geometry(clouds):
    xy = np.concatenate([c['xy'] for c in clouds])
    low, high = xy.min(axis=0), xy.max(axis=0)
    spacing = float(np.ceil(max(CELL_M, float(np.max(high-low))/(MAX_GRID_SIDE-6))))
    while True:
        origin = np.floor(low/spacing)*spacing-2*spacing
        columns, rows = (np.ceil((high-origin)/spacing).astype(int)+3).tolist()
        unknown = reach(xy, origin, spacing, (rows, columns)) <= MAX_SUPPORT_M+spacing
        # A bounded sparse factorization on the 892 MiB server.
        if unknown.sum() <= MAX_UNKNOWNS:
            return origin, spacing, (rows, columns), unknown
        spacing = float(np.ceil(spacing*1.25))


def reconstruct(tracks):
    points = np.concatenate([t['points'] for t in tracks])
    low, high = points[:, :2].min(axis=0), points[:, :2].max(axis=0)
    frame = Frame(*((low+high)/2))
    clouds = []
    for track in tracks:
        cloud = observations(track, frame)
        if len(cloud['z']):
            clouds.append({**cloud, 'id': track['id']})
    if not clouds:
        raise ValueError('有效高程观测不足，无法重建地形')
    links, count, labels = overlap_links([{'xy': c['xy'], 'z': c['z']} for c in clouds])
    origin, spacing, shape, unknown = grid_geometry(clouds)
    rows, columns = shape
    yy, xx = np.mgrid[:rows, :columns]
    query = np.column_stack((origin[0]+xx.ravel()*spacing, origin[1]+yy.ravel()*spacing))
    height = np.full(shape, np.nan)
    support = np.full(shape, np.inf)
    scatter = np.full(shape, np.nan)
    datum = np.full(shape, -1, np.int32)
    corrections, observed, residuals = {}, [], []
    for component in range(count):
        members = [clouds[i] for i in np.flatnonzero(labels == component)]
        fitted = solve_component(members, origin, spacing, shape, unknown)
        mask, distance = supported_nodes(fitted['xy'], query, shape)
        # Each unaligned recording group keeps its own datum; the nearer
        # observations own a node where two groups' support overlaps.
        take = mask & (distance < support) & np.isfinite(fitted['surface'])
        height[take], support[take], datum[take] = fitted['surface'][take], distance[take], component
        cell = np.clip(np.rint((fitted['xy']-origin)/spacing).astype(np.int64), 0, [columns-1, rows-1])
        total, votes = np.zeros(shape), np.zeros(shape)
        np.add.at(total, (cell[:, 1], cell[:, 0]), np.abs(fitted['residual']))
        np.add.at(votes, (cell[:, 1], cell[:, 0]), 1.)
        total, votes = gaussian_filter(total, 1.5), gaussian_filter(votes, 1.5)
        spread = np.where(votes > .2, 1.2533*total/np.maximum(votes, 1e-9), np.nan)
        scatter[take] = spread[take]
        for i, cloud in enumerate(members):
            own = fitted['owner'] == i
            drift = fitted['drift'][own]
            corrections[cloud['id']] = {'id': cloud['id'], 'correctionM': round(-float(np.average(drift, weights=cloud['w'])), 2),
                                        'driftRangeM': round(float(np.ptp(drift)), 2), 'component': int(component),
                                        'observationCells': int(own.sum())}
        observed.append(np.column_stack((fitted['xy'], fitted['z']-fitted['drift'])))
        residuals.append(fitted['residual'])
    # Unaligned recording datums must not meet as a made-up terrain step.
    for axis in (0, 1):
        before = [slice(None)]*2; after = [slice(None)]*2
        before[axis], after[axis] = slice(None, -1), slice(1, None)
        boundary = (datum[tuple(before)] != datum[tuple(after)]) & (datum[tuple(before)] >= 0) & (datum[tuple(after)] >= 0)
        height[tuple(before)][boundary] = np.nan
        height[tuple(after)][boundary] = np.nan
    valid = np.isfinite(height)
    support[~valid] = np.inf
    scatter[~valid] = np.nan
    observed = np.concatenate(observed)
    residual = np.concatenate(residuals)
    # Residuals where another recording passed within 12 m measure agreement.
    owners = np.concatenate([np.full(len(c['z']), i) for i, c in enumerate(clouds)])
    positions = np.concatenate([c['xy'] for c in clouds])
    distance, near = cKDTree(positions).query(positions, k=min(12, len(positions)), distance_upper_bound=12.)
    distance, near = distance.reshape(len(positions), -1), near.reshape(len(positions), -1)
    found = np.isfinite(distance)
    shared = np.any(found & (owners[np.where(found, near, 0)] != owners[:, None]), axis=1)
    values = height[valid]
    metadata = {
        'schema': SCHEMA, 'frame': frame.as_dict(), 'origin': origin.tolist(), 'spacingM': spacing,
        'columns': int(columns), 'rows': int(rows), 'bbox': [*low.tolist(), *high.tolist()],
        'trackIds': [t['id'] for t in tracks], 'pointCount': len(points), 'observationCells': len(observed),
        'coverageFraction': round(float(valid.mean()), 4), 'supportedCells': int(valid.sum()),
        'minHeightM': round(float(values.min()), 1) if len(values) else None,
        'maxHeightM': round(float(values.max()), 1) if len(values) else None,
        'alignment': {'alignedComponents': int(count), 'overlapPairs': len(links),
                      'overlapResidualM': round(float(np.median(np.abs(residual[shared]))), 2) if shared.any() else None,
                      'residualM': round(float(1.4826*np.median(np.abs(residual))), 2),
                      'tracks': [corrections[c['id']] for c in clouds]},
        'datum': 'relative-recorded-height',
        'method': 'robust joint least squares: ground surface + per-recording drift (5-min knots) / thin-plate smoothing',
        'maxTriangleEdgeM': MAX_EDGE_M, 'maxSupportDistanceM': MAX_SUPPORT_M, 'nearSupportDistanceM': NEAR_SUPPORT_M,
    }
    observed_lnglat = frame.to_lnglat(observed[:, :2])
    arrays = {'height': height.astype(np.float32), 'support': support.astype(np.float32), 'scatter': scatter.astype(np.float32),
              'observations': np.column_stack((observed_lnglat, observed[:, 2])).astype(np.float64)}
    return metadata, arrays


class HeightModel:
    def __init__(self, metadata, arrays):
        self.metadata = metadata
        self.frame = Frame.of(metadata)
        self.height = arrays['height']

    def sample(self, lnglat):
        points = np.asarray(lnglat, float).reshape(-1, 2)
        xy = (self.frame.to_local(points)-self.metadata['origin'])/self.metadata['spacingM']
        cells = np.floor(xy).astype(int)
        result = np.full(len(points), np.nan)
        valid = (cells[:, 0] >= 0) & (cells[:, 1] >= 0) & (cells[:, 0] < self.height.shape[1]-1) & (cells[:, 1] < self.height.shape[0]-1)
        ids = np.flatnonzero(valid)
        if not len(ids):
            return result
        x, y = cells[ids].T
        dx, dy = (xy[ids]-cells[ids]).T
        values = np.column_stack((self.height[y, x], self.height[y, x+1], self.height[y+1, x], self.height[y+1, x+1]))
        weights = np.column_stack(((1-dx)*(1-dy), dx*(1-dy), (1-dx)*dy, dx*dy))
        supported = np.all(np.isfinite(values) | (weights < 1e-9), axis=1)
        result[ids[supported]] = np.sum(np.nan_to_num(values[supported])*weights[supported], axis=1)
        return result


def slope_factors(start_height, end_height, horizontal_m):
    """Tobler's grade response, normalized to the team's flat-ground speed."""
    start, end, lengths = np.broadcast_arrays(start_height, end_height, horizontal_m)
    known = np.isfinite(start) & np.isfinite(end) & (lengths > .01)
    grade = np.zeros(start.shape)
    grade[known] = np.clip((end[known]-start[known])/lengths[known], -1., 1.)
    return np.where(known, np.exp(3.5*(np.abs(grade+.05)-.05)), 1.)
