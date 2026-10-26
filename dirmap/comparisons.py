"""Content-checked planning inputs and compact saved solution snapshots."""
import copy
import hashlib
import json
import re
import time

import numpy as np

from . import store
from .protocol import ROUTING_SCHEMA


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, allow_nan=False,
        sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def path_fingerprint(nodes):
    return hashlib.sha256(np.asarray(nodes, dtype=np.int32).tobytes()).hexdigest()


def path(asset_id, identifier):
    if not re.fullmatch(r'(?:[0-9a-f]{16}|[0-9a-f]{32})', identifier):
        raise ValueError('分队比较标识无效')
    return store.asset_dir(asset_id)/f'comparison-{identifier}.json'


def load(asset_id, identifier, token=None):
    saved = store.read_json(path(asset_id, identifier))
    if not saved or saved.get('content', {}).get('schema') != 1:
        raise ValueError('这次记录没有保留其他队数方案，请重新比较')
    if saved.get('token') != fingerprint(saved['content']) or (token is not None and token != saved['token']):
        raise ValueError('分队比较记录已改变，请重新比较后选择')
    if saved['content'].get('assetId') != asset_id:
        raise ValueError('分队比较不属于当前地图')
    return saved


def plan_path(asset_id, identifier):
    checked = path(asset_id, identifier)
    return checked.with_name(f'plan-{identifier}.json')


def model_inputs(state, control_ids):
    controls = {c['id']: c for c in state['controls']}
    return {'sourceHash': state['sourceHash'], 'size': [state['width'], state['height']],
            'registration': state['registration'], 'frame': state['frame'], 'edits': state['edits'],
            'surfaces': state.get('surfaces', []), 'portals': state.get('portals', []),
            'controls': [{'id': identifier, 'pixel': [float(v)+0. for v in controls[identifier]['pixel']],
                          'radiusPx': float(controls[identifier].get('radiusPx', 15))+0.,
                          'role': controls[identifier].get('role') or 'control',
                          'serviceBaseSec': float(controls[identifier].get('serviceBaseSec', 5))+0.,
                          'servicePerPersonSec': float(controls[identifier].get('servicePerPersonSec', 1))+0.}
                         for identifier in control_ids]}


def freeze_bundle(bundle):
    return {'matrix': bundle['matrix'].tolist(), 'nodes': list(map(int, bundle['nodes'])),
            'world': bundle['world'].tolist(), 'connectors': bundle['connectors'].tolist(),
            'connectorWeights': bundle['connectorWeights'].tolist(),
            'pixels': [e.get('approachPixel') or e['pixel'] for e in bundle['endpoints']],
            'surfaces': [e.get('surfaceId', 'ground') for e in bundle['endpoints']]}


def route_metrics(result):
    routes = result['routes']
    return {'latestFinishSec': max(r['finishSec'] for r in routes),
            'totalFinishSec': sum(r['finishSec'] for r in routes),
            'totalDistanceM': sum(r['distanceM'] for r in routes),
            'totalWaitSec': sum(r['waitSec'] for r in routes)}


def objective_value(metrics, objective):
    if objective == 'min_distance':
        return (metrics['totalDistanceM'],)
    if objective == 'min_total_time':
        return (metrics['totalFinishSec'],)
    return (metrics['latestFinishSec'], metrics['totalFinishSec'])


def candidate(result, teams):
    value = {'teams': copy.deepcopy(teams), 'orders': [r['order'].copy() for r in result['routes']],
             'status': result['status'], 'proof': copy.deepcopy(result['proof']), 'metrics': route_metrics(result)}
    if result.get('refinement'):
        value['refinement'] = copy.deepcopy(result['refinement'])
    return value


def used_paths(graph, bundle, orders):
    pairs = {(a, b) for order in orders for a, b in zip(order, order[1:])}
    return {f'{a}:{b}': path_fingerprint(graph.expand(bundle['parents'][a], bundle['nodes'][a], bundle['nodes'][b]))
            for a, b in pairs}


def create(state, request, result, bundle, metrics, identifier, graph_hash, implementation):
    candidates = result.pop('_teamCandidates', None)
    if candidates is None:
        candidates = {str(len(request['teams'])): candidate(result, request['teams'])}
    content = {'schema': 1, 'id': identifier, 'assetId': state['assetId'], 'version': state['version'],
               'routingSchema': ROUTING_SCHEMA, 'graphHash': graph_hash,
               'implementation': implementation, 'requirementsHash': store.file_hash(store.ROOT/'requirements.lock'),
               'modelInputHash': fingerprint(model_inputs(state, request['controlIds'])),
               'bundle': freeze_bundle(bundle), 'candidates': candidates,
               'pathHashes': {f'{a}:{b}': value['pathHash'] for (a, b), value in metrics.items()},
               'request': copy.deepcopy(request), 'recommendation': copy.deepcopy(result.get('teamRecommendation')),
               'createdAt': time.time()}
    return {'content': content, 'token': fingerprint(content)}


def reference(saved):
    return {'id': saved['content']['id'], 'token': saved['token'], 'version': saved['content']['version']}


def source_request(saved):
    content = saved['content']
    return content.get('request') or content['recommendation']['sourceRequest']


def public(saved, count, selection_mode):
    content = saved['content']
    return {**copy.deepcopy(content['recommendation']), 'comparisonId': content['id'],
            'comparisonToken': saved['token'], 'comparisonVersion': content['version'],
            'selectedCount': count, 'selectionMode': selection_mode}


def verify_and_restore(saved, state, graph, bundle, implementation):
    content = saved['content']
    request = source_request(saved)
    if content['routingSchema'] != ROUTING_SCHEMA or content['implementation'] != implementation \
            or content['requirementsHash'] != store.file_hash(store.ROOT/'requirements.lock'):
        raise ValueError('规划程序已更新，请重新比较后选择')
    if content['graphHash'] != graph.content_hash or content['modelInputHash'] != fingerprint(model_inputs(state, request['controlIds'])):
        raise ValueError('地图、点位或耗时设置已变化，请重新比较')
    frozen = content['bundle']
    pixels = [e.get('approachPixel') or e['pixel'] for e in bundle['endpoints']]
    if (list(map(int, bundle['nodes'])) != frozen['nodes'] or pixels != frozen['pixels']
            or [e.get('surfaceId', 'ground') for e in bundle['endpoints']] != frozen.get('surfaces', ['ground']*len(pixels))):
        raise ValueError('到访位置已变化，请重新比较')
    matrix = np.asarray(frozen['matrix'], dtype=float)
    if bundle['matrix'].shape != matrix.shape or not np.allclose(bundle['matrix'], matrix, rtol=0, atol=1e-7):
        raise ValueError('点间路程已变化，请重新比较')
    for key, expected in content['pathHashes'].items():
        a, b = map(int, key.split(':'))
        nodes = graph.expand(bundle['parents'][a], bundle['nodes'][a], bundle['nodes'][b])
        if path_fingerprint(nodes) != expected:
            raise ValueError('候选路径已变化，请重新比较')
    # Confirming an inferred approach at the identical pixel may introduce
    # sub-micrometre floating roundoff in a repeated transform. Restore the
    # original numeric model after checking every physical input/path above.
    for key in ('matrix', 'world', 'connectors', 'connectorWeights'):
        bundle[key] = np.asarray(frozen[key], dtype=float)


def revalidated_copy(saved, state, identifier):
    content = copy.deepcopy(saved['content'])
    content.setdefault('originComparisonId', content['id'])
    content.update(id=identifier, version=state['version'], revalidatedAt=time.time())
    if content.get('request'):
        content['request']['version'] = state['version']
    if content.get('recommendation'):
        content['recommendation']['sourceRequest']['version'] = state['version']
    return {'content': content, 'token': fingerprint(content)}


def refined_copy(saved, state, identifier, result, request, graph, bundle):
    from .team_planning import choose_count
    copied = revalidated_copy(saved, state, identifier)
    content, count = copied['content'], len(request['teams'])
    item = candidate(result, request['teams'])
    content['candidates'][str(count)] = item
    content['pathHashes'].update(used_paths(graph, bundle, item['orders']))
    rec = content.get('recommendation')
    if rec:
        row = next(row for row in rec['candidates'] if row['teamCount'] == count)
        row.update(item['metrics'], status=result['status'], optimalityProven=result['proof']['globalOptimalityProven'])
        rec.update(choose_count(rec['candidates'], request['objective'], rec['targetGainFraction']))
        rec['allCountsOptimalityProven'] = all(row['optimalityProven'] for row in rec['candidates'])
        if result['refinement']['attempted']:
            rec['refinedCounts'] = sorted(set(rec.get('refinedCounts', [])) | {count})
    else:
        content['request'] = copy.deepcopy(request)
    copied['token'] = fingerprint(content)
    return copied
