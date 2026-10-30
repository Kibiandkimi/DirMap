"""Directional grade costs and frozen height inputs for published courses."""
import copy
import hashlib

import numpy as np
from scipy.sparse import csr_matrix

from . import elevation
from .elevation_model import SCHEMA, fingerprint, slope_factors
from .geometry import Frame

GRADE_SCHEMA = 'tobler-relative-flat-speed-v1'
MODEL_KEYS = ('id', 'name', 'fingerprint', 'artifact', 'frame', 'origin', 'spacingM', 'columns', 'rows', 'bbox', 'supportedCells')


def snapshot(state, enabled=True):
    records = elevation.relevant(state) if enabled else []
    return {'enabled': enabled, 'schema': SCHEMA, 'gradeSchema': GRADE_SCHEMA,
            'revision': fingerprint(sorted(r['fingerprint'] for r in records)) if enabled else 'disabled',
            'models': [{k: r[k] for k in MODEL_KEYS} for r in records]}


def available(frozen):
    """A course can only be rerouted while its frozen height models exist."""
    return all((elevation.root()/record['artifact']).is_file() for record in (frozen or {}).get('models', []))


def attach(base, state, frozen):
    graph = copy.copy(base)
    graph.base_content_hash = base.content_hash
    graph.elevation_snapshot = copy.deepcopy(frozen)
    graph.elevation_frame = Frame.of(state)
    graph.elevation_sampler = None
    graph.elevation_node_heights = None
    if not frozen.get('enabled'):
        return graph
    graph.elevation_sampler = elevation.Sampler(frozen['models'])
    if not frozen['models']:
        return graph
    heights = graph.elevation_sampler.sample(graph.elevation_frame.to_lnglat(graph.local))
    # A ground model says nothing about a bridge deck or another floor.
    if getattr(graph, 'node_surfaces', None) is not None:
        heights[graph.node_surfaces != graph.surface_order.index('ground')] = np.nan
    graph.elevation_node_heights = heights
    if not (np.array_equal(base.weights.indices, base.lengths.indices) and np.array_equal(base.weights.indptr, base.lengths.indptr)):
        raise ValueError('通行图代价与长度索引不一致')
    starts = np.repeat(np.arange(len(graph.local)), np.diff(base.weights.indptr))
    factors = slope_factors(heights[starts], heights[base.weights.indices], base.lengths.data)
    # Only the costs differ from the cached flat graph; share its edge index.
    graph.weights = csr_matrix((base.weights.data*factors, base.weights.indices, base.weights.indptr), shape=base.weights.shape)
    graph.content_hash = hashlib.sha256((base.content_hash+frozen['revision']+GRADE_SCHEMA).encode()).hexdigest()
    return graph


def local_heights(graph, positions, surfaces=None):
    sampler = getattr(graph, 'elevation_sampler', None)
    if sampler is None:
        return np.full(len(positions), np.nan)
    result = sampler.sample(graph.elevation_frame.to_lnglat(positions))
    if surfaces is not None:
        result[np.asarray(surfaces) != 'ground'] = np.nan
    return result


def connector_weights(graph, positions, nodes, surfaces, lengths, base_weights):
    if getattr(graph, 'elevation_node_heights', None) is None:
        return base_weights, base_weights
    endpoints = local_heights(graph, positions, surfaces)
    node_heights = graph.elevation_node_heights[nodes]
    return (base_weights*slope_factors(endpoints, node_heights, lengths),
            base_weights*slope_factors(node_heights, endpoints, lengths))


def profile(graph, positions, surfaces):
    frozen = getattr(graph, 'elevation_snapshot', {})
    if not frozen.get('enabled'):
        return None
    points = np.asarray(positions)
    heights = local_heights(graph, points, surfaces)
    distances = np.linalg.norm(np.diff(points, axis=0), axis=1)
    cumulative = np.r_[0., np.cumsum(distances)]
    valid = np.isfinite(heights[1:]) & np.isfinite(heights[:-1])
    valid &= (np.asarray(surfaces[1:]) == 'ground') & (np.asarray(surfaces[:-1]) == 'ground')
    differences = np.diff(heights)[valid]
    known_distance = float(distances[valid].sum())
    total = float(cumulative[-1]) if len(cumulative) else 0.
    # Keep points at every known/unknown transition so a chart cannot bridge a hole.
    selected = set(np.linspace(0, max(0, len(points)-1), min(200, len(points)), dtype=int))
    for index in np.flatnonzero(np.diff(np.isfinite(heights))):
        selected.update((int(index), int(index+1)))
    selected = sorted(selected)
    if len(selected) > 600:
        # Extremely fragmented observations are better represented by gaps than
        # by silently joining them. Each bin is unknown if any member is unknown.
        groups = np.array_split(np.arange(len(points)), 200)
        samples = [{'distanceM': round(float(cumulative[g[-1]]), 1),
                    'heightM': round(float(np.mean(heights[g])), 1) if np.isfinite(heights[g]).all() else None} for g in groups if len(g)]
    else:
        samples = [{'distanceM': round(float(cumulative[i]), 1),
                    'heightM': round(float(heights[i]), 1) if np.isfinite(heights[i]) else None} for i in selected]
    return {'modelRevision': frozen['revision'], 'coverageFraction': round(known_distance/total, 4) if total > 0 else 0.,
            'knownDistanceM': round(known_distance, 1), 'unknownDistanceM': round(max(0., total-known_distance), 1),
            'ascentM': round(float(np.maximum(differences, 0).sum()), 1) if known_distance else None,
            'descentM': round(float(-np.minimum(differences, 0).sum()), 1) if known_distance else None,
            'profile': samples, 'estimated': True, 'datum': 'relative-recorded-height'}
