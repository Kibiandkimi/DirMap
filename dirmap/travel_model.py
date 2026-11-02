"""Learn relative travel costs, keeping activity and personal pace separate.

Times are observations, never inferred from distance. A robust log-time model
has a free intercept for each activity episode, a directional grade response,
and regularized local corridor effects. Only repeated corridors can change a
route. Fast unlabelled records are ambiguous (running and cycling overlap).
"""
from collections import Counter, defaultdict
import math

import numpy as np
from scipy.sparse import coo_matrix, diags, vstack
from scipy.sparse.linalg import lsqr
from scipy.spatial import cKDTree

from .geometry import Frame

SCHEMA = 'activity-pace-corridor-v1'
ACTIVITIES = ('auto', 'walk', 'run', 'bicycle', 'exclude')
MODES = ('walk', 'run', 'bicycle')
CELL_M = 50.
MATCH_M = 10.
MIN_TRACKS = 3
MAX_GAP_SEC = 15.
PRIOR = np.array([3.5, -3.5, 3.5])


def prior_for(mode):
    # The existing walking reference is also the explicitly labelled fallback
    # for foot courses without running data. It is not a bicycle power model.
    return np.zeros(3) if mode == 'bicycle' else PRIOR


def grade_features(grade):
    g = np.clip(np.nan_to_num(np.asarray(grade, float)), -1., 1.)
    return np.column_stack((np.maximum(g, 0), np.minimum(np.maximum(-g, 0), .05), np.maximum(-g-.05, 0)))


def grade_log_cost(grade, coefficients=PRIOR):
    return grade_features(grade) @ np.asarray(coefficients)


def _runs(mask):
    changes = np.flatnonzero(np.diff(np.r_[False, mask, False]))
    return zip(changes[::2], changes[1::2])


def weighted_quantile(values, weights, quantile=.5):
    order = np.argsort(values)
    cumulative = np.cumsum(np.asarray(weights)[order])
    index = np.searchsorted(cumulative+1e-12*cumulative[-1], quantile*cumulative[-1])
    return float(np.asarray(values)[order[min(index, len(order)-1)]])


def observations(track, frame, sampler=None):
    """Non-overlapping, nearly straight moving windows from real clock times.

    Five-second position averages suppress one-second GNSS jitter. Splits are
    made BEFORE smoothing: invalid clocks, geometry breaks, gaps and jumps
    cannot become a fast shortcut. Stationary samples never train path costs.
    """
    summary = {'id': track['id'], 'activity': track.get('activity', 'auto'), 'inferredActivity': 'unknown',
               'confidence': 0., 'timedPairs': 0, 'movingSec': 0., 'stoppedSec': 0.,
               'rejectedPairs': 0, 'observations': 0, 'episodes': 0, 'medianSpeedMps': None,
               'reason': 'missing_timestamps'}
    points, times = np.asarray(track['points']), track.get('times')
    if track.get('kind') == 'contours' or times is None or len(times) != len(points) or len(points) < 3:
        return [], summary
    times = np.asarray(times, float)
    xy = frame.to_local(points[:, :2])
    dt, distance = np.diff(times), np.linalg.norm(np.diff(xy, axis=0), axis=1)
    valid = np.isfinite(dt) & (dt > 0) & (dt <= MAX_GAP_SEC) & np.isfinite(distance)
    valid &= distance <= np.maximum(30., np.maximum(dt, 0)*15.)
    for index in track.get('breaks', []):
        if 0 < index < len(points):
            valid[int(index)-1] = False
    summary['timedPairs'] = int(valid.sum())
    summary['rejectedPairs'] = int(len(valid)-valid.sum())
    result, episode = [], 0
    for first, last in _runs(valid):
        t, positions = times[first:last+1], xy[first:last+1]
        # Bins are disjoint; their mean time remains paired with mean position.
        bins = np.floor((t-t[0])/5).astype(int)
        starts = np.flatnonzero(np.r_[True, np.diff(bins) != 0])
        counts = np.diff(np.r_[starts, len(t)])
        p = np.add.reduceat(positions, starts, axis=0)/counts[:, None]
        t = np.add.reduceat(t, starts)/counts
        if len(t) < 3:
            continue
        steps, seconds = np.linalg.norm(np.diff(p, axis=0), axis=1), np.diff(t)
        moving = (steps/seconds >= .4) & (steps/seconds <= 12.)
        summary['stoppedSec'] += float(seconds[steps/seconds < .4].sum())
        episode += 1
        previous_end = 0
        for begin, end in _runs(moving):
            if t[begin]-t[previous_end] > 90:
                episode += 1
            previous_end = end
            start = begin
            for stop in range(begin+1, end+1):
                elapsed = t[stop]-t[start]
                chord = np.linalg.norm(p[stop]-p[start])
                if (chord < 30 or elapsed < 8) and elapsed < 35 and stop != end:
                    continue
                along = steps[start:stop].sum()
                if chord >= 12 and elapsed >= 8 and chord >= .85*along:
                    result.append({'start': p[start].tolist(), 'end': p[stop].tolist(),
                                   'distance': float(chord), 'seconds': float(elapsed),
                                   'time': float(t[start]), 'episode': f'{track["id"]}:{episode}',
                                   'track': track['id'], 'grade': None})
                start = stop
    if not result:
        summary['reason'] = 'no_reliable_moving_windows' if valid.any() else 'missing_timestamps'
        return [], summary
    speed = np.array([r['distance']/r['seconds'] for r in result])
    median, p90 = np.percentile(speed, [50, 90])
    activity = track.get('activity', 'auto')
    # Low speed is only evidence compatible with walking, not a sensor label.
    inferred = 'walk' if .55 <= median <= 1.85 and p90 <= 2.25 else 'unknown'
    confidence = .65 if inferred == 'walk' else 0.
    if activity in MODES:
        inferred, confidence = activity, 1.
    elif activity == 'exclude':
        inferred, confidence = 'excluded', 0.
    if sampler is not None:
        endpoints = np.array([[r['start'], r['end']] for r in result])
        heights = sampler.sample(frame.to_lnglat(endpoints.reshape(-1, 2))).reshape(-1, 2)
        grades = (heights[:, 1]-heights[:, 0])/np.array([r['distance'] for r in result])
    else:
        grades = np.full(len(result), np.nan)
    # Persistent pace changes need another intercept. Grade-adjusted speed is
    # used so an ordinary hill itself does not create an activity boundary.
    normalized = np.log(speed)+grade_log_cost(grades, prior_for(inferred))
    block, previous = 0, None
    for index, r in enumerate(result):
        if previous != r['episode']:
            block += 1
        elif index >= 3 and index+3 <= len(result):
            before, after = np.median(normalized[index-3:index]), np.median(normalized[index:index+3])
            if abs(after-before) > math.log(1.8) and abs(normalized[index]-normalized[index-1]) > .3:
                block += 1
        previous = r['episode']
        r.update(episode=f'{track["id"]}:{block}', mode=inferred, confidence=confidence,
                 grade=float(grades[index]) if np.isfinite(grades[index]) and abs(grades[index]) <= .4 else None)
    summary.update(inferredActivity=inferred, confidence=confidence,
                   medianSpeedMps=round(float(median), 3), observations=len(result),
                   movingSec=round(sum(r['seconds'] for r in result), 1),
                   stoppedSec=round(summary['stoppedSec'], 1), episodes=block,
                   reason='contributor_label' if activity in MODES else 'excluded_by_contributor' if activity == 'exclude'
                   else 'walking_compatible' if inferred == 'walk' else 'running_or_cycling_ambiguous')
    return result, summary


def corridor_key(start, end):
    a, b = np.asarray(start), np.asarray(end)
    midpoint = (a+b)/2
    angle = math.atan2(*(b-a)[::-1]) % math.pi
    return (*np.floor(midpoint/CELL_M).astype(int).tolist(), int(np.floor((angle+math.pi/12)/(math.pi/6))) % 6)


def fit_profile(rows, mode):
    """IRLS with episode fixed effects and shrinkage toward known fallbacks.

    Each recording gets at most one unit of weight per corridor. A hundred
    repeated laps in one file are not a hundred independent observations.
    Pace intercepts are not penalized: a slow person's entire trip cannot
    be explained away as a collection of slow roads.
    """
    rows = [r for r in rows if r['mode'] == mode and r['confidence'] > 0]
    prior = prior_for(mode)
    profile = {'mode': mode, 'tracks': len({r['track'] for r in rows}), 'observations': len(rows),
               'coefficients': prior.tolist(), 'priorCoefficients': prior.tolist(), 'gradeLearned': False, 'gradeRange': [0., 0.],
               'corridors': [], 'residualLogMad': None, 'paceMps': {}, 'status': 'insufficient_data'}
    if len(rows) < 12 or profile['tracks'] < MIN_TRACKS:
        return profile
    keys = [corridor_key(r['start'], r['end']) for r in rows]
    by_key, episodes = {}, {}
    for key, row in zip(keys, rows):
        by_key.setdefault(key, len(by_key)); episodes.setdefault(row['episode'], len(episodes))
    n, p, c = len(rows), len(episodes), len(by_key)
    column = np.array([by_key[k] for k in keys])
    episode = np.array([episodes[r['episode']] for r in rows])
    grade = np.array([r['grade'] if r['grade'] is not None else np.nan for r in rows])
    known = np.isfinite(grade)
    # A slope response must be identified by multiple independent recordings.
    # Between-person grade differences do not identify a slope response: it
    # needs within-episode variation after the pace intercept is removed.
    slope_tracks = set()
    for identifier in np.unique(episode):
        which = np.flatnonzero((episode == identifier) & known)
        if len(which) >= 5 and np.std(grade[which]) >= .012 and np.ptp(grade[which]) >= .035:
            slope_tracks.add(rows[which[0]]['track'])
    learn_grade = bool(len(slope_tracks) >= MIN_TRACKS and known.sum() >= 25 and np.nanstd(grade) >= .012)
    features = grade_features(grade)/.05 if learn_grade else np.zeros((n, 3))
    response = np.log([r['seconds']/r['distance'] for r in rows])-grade_log_cost(grade, prior)
    design = coo_matrix((np.r_[np.ones(n), features.ravel(), np.ones(n)],
                        (np.r_[np.arange(n), np.repeat(np.arange(n), 3), np.arange(n)],
                         np.r_[episode, np.tile(np.arange(p, p+3), n), p+3+column])), shape=(n, p+3+c)).tocsr()
    counts = Counter((r['track'], k) for r, k in zip(rows, keys))
    weight = np.array([r['confidence']/counts[r['track'], k] for r, k in zip(rows, keys)])
    # Grade coefficients are expressed as log-time change at a 5% grade.
    penalty = np.r_[np.zeros(p), np.full(3, 4.), np.full(c, 2.)]
    regularizer = diags(np.sqrt(penalty), format='csr')
    robust = np.ones(n)
    for _ in range(7):
        w = np.sqrt(weight*robust)
        augmented = vstack((design.multiply(w[:, None]), regularizer), format='csr')
        beta = lsqr(augmented, np.r_[response*w, np.zeros(p+3+c)], atol=1e-8, btol=1e-8, iter_lim=500)[0]
        residual = response-design@beta
        center = weighted_quantile(residual, weight)
        scale = max(.12, 1.4826*weighted_quantile(np.abs(residual-center), weight))
        robust = np.minimum(1., 1.5*scale/np.maximum(np.abs(residual), 1e-9))
    coefficients = prior+beta[p:p+3]/.05
    coefficients = np.clip(coefficients, [0., -5., 0.], [12., 5., 12.])
    # Recompute local/pace terms after imposing physical bounds on grade.
    if np.max(np.abs(coefficients-(prior+beta[p:p+3]/.05))) > 1e-8:
        fixed = grade_log_cost(grade, coefficients)-grade_log_cost(grade, prior)
        reduced = design[:, np.r_[np.arange(p), np.arange(p+3, p+3+c)]]
        w = np.sqrt(weight*robust)
        answer = lsqr(vstack((reduced.multiply(w[:, None]), diags(np.sqrt(np.r_[np.zeros(p), np.full(c, 2.)])))),
                      np.r_[(response-fixed)*w, np.zeros(p+c)], atol=1e-8, btol=1e-8, iter_lim=500)[0]
        beta[:p], beta[p+3:] = answer[:p], answer[p:]
        residual = response-fixed-reduced@answer
    profile.update(coefficients=coefficients.tolist(), gradeLearned=learn_grade,
                   gradeRange=[min(0., float(np.nanpercentile(grade, 5))), max(0., float(np.nanpercentile(grade, 95)))] if learn_grade else [0., 0.],
                   residualLogMad=round(max(.12, 1.4826*weighted_quantile(np.abs(residual-weighted_quantile(residual, weight)), weight)), 4),
                   paceMps={key: round(float(np.exp(-beta[index])), 3) for key, index in episodes.items()},
                   status='learned')
    for key, index in by_key.items():
        which = np.flatnonzero(column == index)
        ids = {rows[i]['track'] for i in which}
        if len(ids) < MIN_TRACKS:
            continue
        starts = np.array([rows[i]['start'] for i in which], float); ends = np.array([rows[i]['end'] for i in which], float)
        vectors = ends-starts
        vectors /= np.linalg.norm(vectors, axis=1)[:, None]
        w = weight[which]
        axis = np.linalg.eigh(vectors.T@(vectors*w[:, None]))[1][:, -1]
        middle = (starts+ends)/2
        center = np.array([weighted_quantile(middle[:, i], w) for i in (0, 1)])
        along = (np.concatenate((starts, ends))-center)@axis
        # Keep a corridor only if its repeated GPS observations describe one
        # narrow path. Parallel roads or different crossing angles stay unknown.
        normal = np.array([-axis[1], axis[0]])
        across = ((starts+ends)/2-center)@normal
        spread = weighted_quantile(np.abs(across), w, .8)
        if spread > MATCH_M or np.min(np.abs(vectors@axis)) < .8:
            continue
        a, b = (center+axis*weighted_quantile(along, np.r_[w, w], q) for q in (.05, .95))
        log_factor = float(np.clip(beta[p+3+index], math.log(.65), math.log(1.8)))
        profile['corridors'].append({'start': np.round(a, 3).tolist(), 'end': np.round(b, 3).tolist(),
                                    'logFactor': round(log_factor, 6), 'tracks': len(ids),
                                    'observations': len(which), 'scatterM': round(spread, 2)})
    return profile


def build(tracks, frame, sampler=None, bbox=None):
    rows, summaries = [], []
    for track in sorted(tracks, key=lambda t: t['id']):
        if track.get('kind') == 'contours':
            continue
        observed, summary = observations(track, frame, sampler)
        if bbox is not None and observed:
            middle = np.array([(np.array(r['start'])+r['end'])/2 for r in observed])
            lnglat = frame.to_lnglat(middle)
            keep = (lnglat[:, 0] >= bbox[0]) & (lnglat[:, 1] >= bbox[1]) & (lnglat[:, 0] <= bbox[2]) & (lnglat[:, 1] <= bbox[3])
            observed = [r for r, yes in zip(observed, keep) if yes]
        rows.extend(observed); summaries.append(summary)
    profiles = {mode: fit_profile(rows, mode) for mode in MODES}
    return {'schema': SCHEMA, 'frame': frame.as_dict(), 'bbox': bbox, 'profiles': profiles, 'tracks': summaries,
            'observations': len(rows), 'activityCounts': dict(Counter(t['inferredActivity'] for t in summaries))}


class Predictor:
    """Match a *direction-compatible corridor*, not an entire nearby grid cell."""
    def __init__(self, model, mode='walk'):
        self.model, self.frame = model, Frame.of(model)
        self.profile = model['profiles'].get(mode, {})
        corridors = self.profile.get('corridors', [])
        self.a = np.array([c['start'] for c in corridors], float).reshape(-1, 2)
        self.b = np.array([c['end'] for c in corridors], float).reshape(-1, 2)
        self.log_factor = np.array([c['logFactor'] for c in corridors])
        self.counts = np.array([c['tracks'] for c in corridors])
        self.tree = cKDTree((self.a+self.b)/2) if corridors else None

    def local_effect(self, starts, ends):
        starts, ends = np.asarray(starts, float).reshape(-1, 2), np.asarray(ends, float).reshape(-1, 2)
        n = len(starts)
        factors, supported, counts = np.zeros(n), np.zeros(n, bool), np.zeros(n, int)
        if self.tree is None or not n:
            return factors, supported, counts
        # Bound scratch memory for pixel-resolution graphs with millions of edges.
        for first in range(0, n, 20000):
            last = min(n, first+20000)
            a, b = starts[first:last], ends[first:last]
            middle, direction = (a+b)/2, b-a
            lengths = np.linalg.norm(direction, axis=1)
            direction /= np.maximum(lengths, 1e-9)[:, None]
            _, indices = self.tree.query(middle, k=min(8, len(self.a)))
            indices = indices.reshape(len(middle), -1)
            origin, vectors = self.a[indices], self.b[indices]-self.a[indices]
            squared = np.sum(vectors*vectors, axis=2)
            fraction = np.clip(np.sum((middle[:, None, :]-origin)*vectors, axis=2)/np.maximum(squared, 1e-9), 0, 1)
            distance = np.linalg.norm(middle[:, None, :]-origin-fraction[:, :, None]*vectors, axis=2)
            alignment = np.abs(np.sum(direction[:, None, :]*vectors, axis=2))/np.maximum(np.sqrt(squared), 1e-9)
            valid = (distance <= MATCH_M) & (alignment >= .9) & (lengths[:, None] > .01)
            for endpoint in (a, b):
                projection = np.clip(np.sum((endpoint[:, None, :]-origin)*vectors, axis=2)/np.maximum(squared, 1e-9), 0, 1)
                valid &= np.linalg.norm(endpoint[:, None, :]-origin-projection[:, :, None]*vectors, axis=2) <= MATCH_M
            score = np.where(valid, distance, np.inf)
            chosen = np.argmin(score, axis=1); row = np.arange(len(middle))
            ok = np.isfinite(score[row, chosen])
            chosen = indices[row, chosen]
            factors[first:last] = np.where(ok, self.log_factor[chosen], 0.)
            supported[first:last] = ok
            counts[first:last] = np.where(ok, self.counts[chosen], 0)
        return factors, supported, counts

    def grade_cost(self, grades):
        grades = np.asarray(grades, float)
        prior = self.profile.get('priorCoefficients', PRIOR)
        result = grade_log_cost(grades, prior)
        if self.profile.get('gradeLearned'):
            low, high = self.profile['gradeRange']
            # Outside observed grades, continue with the prior derivative.
            clipped = np.clip(np.nan_to_num(grades), low, high)
            result += grade_log_cost(clipped, self.profile['coefficients'])-grade_log_cost(clipped, prior)
        return result

    def predict(self, starts, ends, grades, base_factors=None):
        local, supported, counts = self.local_effect(starts, ends)
        base = np.ones(len(local)) if base_factors is None else np.asarray(base_factors)
        # Observed flat-ground costs replace the map's generic terrain factor;
        # multiplying both would count grass/stairs difficulty twice.
        terrain = np.where(supported, np.exp(local), base)
        return terrain*np.exp(self.grade_cost(grades)), supported, counts
