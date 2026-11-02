"""Immutable learned travel models and their application to legal path edges."""
from collections import OrderedDict
import copy
import hashlib
import threading

import numpy as np
from scipy.sparse import csr_matrix

from . import store, travel_model
from .elevation_model import fingerprint
from .geometry import Frame

SCHEMA = travel_model.SCHEMA
_models = OrderedDict()
_lock = threading.RLock()


def rebuild(tracks, regions, previous, progress=lambda *args: None):
    from . import elevation
    refs = []
    for index, region in enumerate(regions):
        group = [t for t in tracks if t['id'] in region['trackIds'] and t.get('kind') != 'contours']
        signature = fingerprint({'schema': SCHEMA, 'height': region['fingerprint'],
            'tracks': [[t['id'], t.get('activity', 'auto'),
                        hashlib.sha256(np.nan_to_num(np.asarray(t.get('times', []), float), nan=-1).astype('<f8').tobytes()
                                       +np.asarray(t.get('breaks', []), '<i4').tobytes()).hexdigest()] for t in sorted(group, key=lambda t: t['id'])]})
        old = next((r for r in previous if r.get('inputSignature') == signature), None)
        if old and (elevation.root()/old['artifact']).is_file():
            refs.append(old); continue
        progress(86+int(10*index/max(1, len(regions))), f'区分活动配速，学习区域 {index+1} 的路段耗时')
        model = travel_model.build(group, Frame.of(region), elevation.Sampler([region]), region['bbox'])
        digest = fingerprint(model)
        artifact = 'travel-models/'+digest+'.json'
        store.atomic_json(elevation.root()/artifact, model)
        profiles = {mode: {k: v for k, v in profile.items() if k not in ('paceMps', 'corridors')}
                    | {'corridorCount': len(profile['corridors'])} for mode, profile in model['profiles'].items()}
        refs.append({'regionId': region['id'], 'name': region['name'], 'bbox': region['bbox'],
                     'schema': SCHEMA, 'fingerprint': digest, 'inputSignature': signature, 'artifact': artifact,
                     'profiles': profiles, 'tracks': model['tracks'], 'activityCounts': model['activityCounts']})
    return refs


def snapshot(state, enabled=True, mode='walk'):
    from . import elevation
    ids = {r['id'] for r in elevation.relevant(state)} if enabled else set()
    refs = [{k: r[k] for k in ('artifact', 'fingerprint', 'bbox', 'profiles')}
            for r in elevation.models().get('travelModels', []) if r['regionId'] in ids and r.get('schema') == SCHEMA]
    return {'enabled': enabled, 'mode': mode, 'schema': SCHEMA, 'models': refs,
            'revision': fingerprint({'schema': SCHEMA, 'mode': mode, 'models': [r['fingerprint'] for r in refs]}) if enabled else 'disabled'}


def available(frozen):
    from . import elevation
    return all((elevation.root()/r['artifact']).is_file() for r in (frozen or {}).get('models', []))


def load(record, mode):
    from . import elevation
    path = elevation.root()/record['artifact']
    key = str(path.resolve()), record['fingerprint'], mode
    with _lock:
        # A cached predictor must not hide a deleted frozen artifact.
        if not path.is_file():
            raise ValueError('这份路线所用的速度学习快照已不可用，请重新生成路线')
        if key not in _models:
            model = store.read_json(path)
            if not model or model.get('schema') != SCHEMA or fingerprint(model) != record['fingerprint']:
                raise ValueError('速度学习快照不完整，请重新生成路线')
            _models[key] = travel_model.Predictor(model, mode)
        _models.move_to_end(key)
        while len(_models) > 6:
            _models.popitem(last=False)
        return _models[key]


def edge_factors(graph, starts, ends, grades, base_factors):
    """Integrate local costs along long shortcuts, including uncovered pieces."""
    starts, ends = np.asarray(starts), np.asarray(ends)
    prior = travel_model.prior_for(getattr(graph, 'travel_snapshot', {}).get('mode', 'walk'))
    factors = np.asarray(base_factors)*np.exp(travel_model.grade_log_cost(grades, prior))
    coverage = np.zeros(len(starts))
    predictors = getattr(graph, 'travel_predictors', [])
    if not any(p.profile.get('gradeLearned') or p.tree is not None for p in predictors) or not len(starts):
        return factors, coverage
    for first in range(0, len(starts), 10000):
        last = min(len(starts), first+10000)
        a, b = starts[first:last], ends[first:last]
        length = np.linalg.norm(b-a, axis=1)
        count = np.maximum(1, np.ceil(length/10).astype(int))
        ids = np.repeat(np.arange(len(a)), count)
        prefix = np.repeat(np.r_[0, np.cumsum(count)[:-1]], count)
        fractions = (np.arange(len(ids))-prefix)/count[ids]
        start = a[ids]+(b-a)[ids]*fractions[:, None]
        end = a[ids]+(b-a)[ids]*(fractions+1/count[ids])[:, None]
        start_ll, end_ll = graph.elevation_frame.to_lnglat(start), graph.elevation_frame.to_lnglat(end)
        middle = (start_ll+end_ll)/2
        g = np.asarray(grades)[first:last][ids]
        base = np.asarray(base_factors)[first:last][ids]
        values = base*np.exp(travel_model.grade_log_cost(g, prior))
        known, assigned = np.zeros(len(ids), bool), np.zeros(len(ids), bool)
        for predictor in predictors:
            bounds = predictor.model.get('bbox')
            inside = ~assigned
            if bounds:
                inside &= (middle[:, 0] >= bounds[0]) & (middle[:, 1] >= bounds[1]) & (middle[:, 0] <= bounds[2]) & (middle[:, 1] <= bounds[3])
            if not inside.any():
                continue
            values[inside], known[inside], _ = predictor.predict(predictor.frame.to_local(start_ll[inside]),
                predictor.frame.to_local(end_ll[inside]), g[inside], base[inside])
            assigned |= inside
        factors[first:last] = np.bincount(ids, values, minlength=len(a))/count
        coverage[first:last] = np.bincount(ids, known.astype(float), minlength=len(a))/count
    return factors, coverage


def attach(graph, base, frozen):
    graph.travel_snapshot = copy.deepcopy(frozen or {'enabled': False})
    graph.travel_predictors = []
    if not frozen or not frozen.get('enabled') or not frozen.get('models'):
        return graph
    if frozen.get('schema') != SCHEMA:
        raise ValueError('速度学习方法已更新，请重新生成路线')
    graph.travel_predictors = [load(r, frozen['mode']) for r in frozen['models']]
    if not any(p.profile.get('gradeLearned') or p.tree is not None or
               not np.array_equal(p.profile.get('priorCoefficients', travel_model.PRIOR), travel_model.PRIOR)
               for p in graph.travel_predictors):
        graph.content_hash = hashlib.sha256((graph.content_hash+frozen['revision']+SCHEMA).encode()).hexdigest()
        return graph
    starts = np.repeat(np.arange(len(graph.local)), np.diff(base.weights.indptr))
    ends = base.weights.indices
    selected = np.ones(len(starts), bool)
    if getattr(graph, 'node_surfaces', None) is not None:
        ground = graph.surface_order.index('ground')
        selected &= (graph.node_surfaces[starts] == ground) & (graph.node_surfaces[ends] == ground)
    indices = np.flatnonzero(selected)
    values = graph.weights.data.copy()
    for first in range(0, len(indices), 30000):
        idx = indices[first:first+30000]
        a, b, length = starts[idx], ends[idx], base.lengths.data[idx]
        grades = np.full(len(a), np.nan)
        heights = getattr(graph, 'elevation_node_heights', None)
        if heights is not None:
            grades = np.clip((heights[b]-heights[a])/np.maximum(length, .01), -1., 1.)
        factors, _ = edge_factors(graph, graph.local[a], graph.local[b], grades,
                                 base.weights.data[idx]/np.maximum(length, .01))
        values[idx] = np.where(length > .01, length*factors, values[idx])
    graph.weights = csr_matrix((values, base.weights.indices, base.weights.indptr), shape=base.weights.shape)
    graph.content_hash = hashlib.sha256((graph.content_hash+frozen['revision']+SCHEMA).encode()).hexdigest()
    return graph


def connector_weights(graph, positions, nodes, surfaces, lengths, base_weights, fallback):
    if not getattr(graph, 'travel_predictors', []):
        return fallback
    from .elevation_routing import local_heights
    grades = np.full(len(nodes), np.nan)
    if getattr(graph, 'elevation_node_heights', None) is not None:
        grades = np.clip((graph.elevation_node_heights[nodes]-local_heights(graph, positions, surfaces))/np.maximum(lengths, .01), -1., 1.)
    base = base_weights/np.maximum(lengths, .01)
    outward, _ = edge_factors(graph, positions, graph.local[nodes], grades, base)
    inward, _ = edge_factors(graph, graph.local[nodes], positions, -grades, base)
    ground = (np.asarray(surfaces) == 'ground') & (lengths > .01)
    if getattr(graph, 'node_surfaces', None) is not None:
        ground &= graph.node_surfaces[nodes] == graph.surface_order.index('ground')
    return np.where(ground, lengths*outward, fallback[0]), np.where(ground, lengths*inward, fallback[1])


def profile(graph, positions, surfaces):
    frozen = getattr(graph, 'travel_snapshot', {})
    if not frozen.get('enabled'):
        return None
    positions = np.asarray(positions).reshape(-1, 2)
    length = np.linalg.norm(np.diff(positions, axis=0), axis=1)
    _, coverage = edge_factors(graph, positions[:-1], positions[1:], np.full(len(length), np.nan), np.ones(len(length)))
    ground = (np.asarray(surfaces[:-1]) == 'ground') & (np.asarray(surfaces[1:]) == 'ground')
    known = float(np.sum(length*coverage*ground))
    total = float(length.sum())
    grade_learned = False
    if getattr(graph, 'elevation_node_heights', None) is not None and any(p.profile.get('gradeLearned') for p in getattr(graph, 'travel_predictors', [])):
        from .elevation_routing import local_heights
        height = local_heights(graph, positions, surfaces)
        grade_learned = bool(np.any(np.isfinite(height[:-1]) & np.isfinite(height[1:]) & ground & (length > .01)))
    return {'enabled': True, 'mode': frozen['mode'], 'modelRevision': frozen['revision'],
            'coverageFraction': round(known/total, 4) if total else 0.,
            'learnedDistanceM': round(known, 1), 'fallbackDistanceM': round(max(0., total-known), 1),
            'gradeLearned': grade_learned, 'movingTimeOnly': True,
            'status': 'learned' if known > 0 or grade_learned else 'insufficient_data'}
