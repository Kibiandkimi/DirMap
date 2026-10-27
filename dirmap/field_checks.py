"""Independent GPS check points for a map's registration.

A teacher stands at a feature recognisable on the original sheet, records
the phone's WGS84 fix with its reported accuracy, and marks the same spot on
the sheet. The points never enter the fit and live outside the versioned map
state, so recording one invalidates no route or published course. Errors are
recomputed against the current registration on every read; they include the
phone's own positioning error.
"""
import time
import uuid

import numpy as np

from . import store
from .geometry import Frame, Registration

MAX_ACCURACY_M = 10.
MIN_FOR_VERDICT = 5
MAX_CHECKS = 200
# Validation plan G1: independent points, median/P95/max in metres.
THRESHOLDS = {'medianM': 3., 'p95M': 8., 'maxM': 12.}


def path(asset_id):
    return store.asset_dir(asset_id)/'field-checks.json'


def add(asset_id, check, now=None):
    with store.LOCK:
        state = store.get_state(asset_id)
        if not state:
            raise ValueError('请先识别地图')
        x, y = check['pixel']
        if not (0 <= x < state['width'] and 0 <= y < state['height']):
            raise ValueError('核查点超出原图范围')
        checks = store.read_json(path(asset_id), [])
        if len(checks) >= MAX_CHECKS:
            raise ValueError('核查点过多，请先删除不再需要的点')
        checks.append({'id': uuid.uuid4().hex[:12], 'pixel': [float(x), float(y)],
                       'lnglat': [float(v) for v in check['lnglat']], 'accuracyM': float(check['accuracyM']),
                       'measuredAt': float(check['measuredAt']), 'recordedAt': time.time() if now is None else now})
        store.atomic_json(path(asset_id), checks)
    return report(asset_id)


def remove(asset_id, check_id):
    with store.LOCK:
        checks = store.read_json(path(asset_id), [])
        kept = [check for check in checks if check['id'] != check_id]
        if len(kept) == len(checks):
            raise ValueError('核查点不存在')
        store.atomic_json(path(asset_id), kept)
    return report(asset_id)


def report(asset_id):
    state = store.get_state(asset_id)
    checks = store.read_json(path(asset_id), [])
    if not state or not state.get('registration') or not checks:
        return {'checks': [{**c, 'errorM': None, 'insideRegistration': False, 'predictedLnglat': None, 'usable': False} for c in checks],
                'summary': summarize([], len(checks))}
    predicted, inside = Registration.from_dict(state['registration']).forward([c['pixel'] for c in checks])
    frame = Frame.of(state)
    measured = frame.to_local([c['lnglat'] for c in checks])
    rows = []
    for check, guess, valid, truth in zip(checks, predicted, inside, measured):
        error = float(np.linalg.norm(guess-truth)) if valid else None
        rows.append({**check, 'errorM': error, 'insideRegistration': bool(valid),
                     'predictedLnglat': frame.to_lnglat(guess).tolist() if valid else None,
                     'usable': bool(valid and check['accuracyM'] <= MAX_ACCURACY_M)})
    usable = [row for row in rows if row['usable']]
    return {'checks': rows, 'summary': summarize(usable, len(rows))}


def summarize(usable, count):
    summary = {'count': count, 'usable': len(usable), 'thresholds': THRESHOLDS, 'maxAccuracyM': MAX_ACCURACY_M,
               'minimumForVerdict': MIN_FOR_VERDICT}
    if not usable:
        return summary
    errors = np.array([row['errorM'] for row in usable])
    summary.update(medianM=round(float(np.median(errors)), 2), p95M=round(float(np.percentile(errors, 95)), 2),
                   maxM=round(float(errors.max()), 2),
                   accuracyMedianM=round(float(np.median([row['accuracyM'] for row in usable])), 1))
    if len(usable) >= MIN_FOR_VERDICT:
        summary['meetsThresholds'] = bool(summary['medianM'] <= THRESHOLDS['medianM'] and summary['p95M'] <= THRESHOLDS['p95M']
                                          and summary['maxM'] <= THRESHOLDS['maxM'])
    return summary
