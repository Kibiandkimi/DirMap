#!/usr/bin/env python3
"""Honest per-map acceptance: is the complete detected course routable?

For every real map this script builds the native-pixel graph exactly as the
service does, resolves every detected control plus a defensible start/finish,
and asks `plan_course` for one complete course with a single team. It prints a
table and exits non-zero unless every map is fully routable.

Writes are redirected to a byte-identical temporary copy of
`data/projects/<asset>/`, so a failing plan can never clobber the real
`state.json` or leave plan files in the teaching data.

This is a routing check on the detected course, not a recognition benchmark:
automatic approaches are reported and stay review-required.
"""

import argparse
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dirmap import service, store
from dirmap.geometry import Frame, Registration
from dirmap.graph import expand_route, plan_matrix

ASSETS = ('szu-1', 'szu-2', 'szu-3')
TEAM = {'id': 'acceptance', 'people': 1, 'speedMps': 1.4, 'departureSec': 0}


def _course_controls(state):
    return [c for c in state['controls']
            if c.get('role', 'control') == 'control' and c['state'] != 'rejected']


def _start_finish(state, reg):
    """Use explicit start AND finish symbols without inventing either end."""
    starts = [c for c in state['controls'] if c.get('role') in ('start', 'start_finish') and c['state'] != 'rejected']
    finishes = [c for c in state['controls'] if c.get('role') in ('finish', 'start_finish') and c['state'] != 'rejected']
    return (starts[0], finishes[0]) if len(starts) == len(finishes) == 1 else None


def _check_edges(graph, bundle, order):
    """Every traversed edge must be a real graph edge over clear pixels."""
    checked = 0
    for endpoint, node in zip(bundle['endpoints'], bundle['nodes']):
        assert graph.clear_segment(endpoint.get('approachPixel') or endpoint['pixel'], graph.pixels[node])
    route = expand_route(graph, bundle, order)
    for a, b in route['edgeIds']:
        if not graph.weights[a, b] > 0:
            raise AssertionError(f'route uses a non-edge {a}->{b}')
        if not graph.clear_segment(graph.pixels[a], graph.pixels[b]):
            raise AssertionError(f'route crosses a blocked native pixel {a}->{b}')
        checked += 1
    return checked


def audit(asset_id):
    original = store.get_state(asset_id)
    if not original or not original.get('registration'):
        return {'assetId': asset_id, 'status': 'unregistered',
                'reason': '没有已保存的配准结果，请先运行 prepare_maps.py', 'routable': False}

    source_terrain = store.artifact_path(original, 'terrain.npz')
    terrain_hash = store.file_hash(source_terrain)
    with tempfile.TemporaryDirectory(prefix='dirmap-acceptance-') as temporary:
        copy = Path(temporary)/'projects'/asset_id
        copy.mkdir(parents=True)
        # Materialize the chosen immutable generation as a legacy flat
        # fixture; the teaching manifest and its other versions stay intact.
        original = dict(original)
        original.pop('artifacts', None)
        original.pop('lastOperationId', None)
        store.atomic_json(copy/'state.json', original)
        shutil.copy2(source_terrain, copy/'terrain.npz')
        # Geographic transfer dependencies must describe the same prepared
        # base even though the audited map itself is isolated from writes.
        parent = (original.get('quality') or {}).get('baseAssetId')
        seen = {asset_id}
        while parent and parent not in seen:
            seen.add(parent)
            state = store.get_state(parent)
            if not state:
                break
            destination = Path(temporary)/'projects'/parent
            destination.mkdir(parents=True, exist_ok=True)
            shutil.copy2(store.asset_dir(parent)/'state.json', destination/'state.json')
            parent = (state.get('quality') or {}).get('baseAssetId')
        previous = store.DATA
        store.DATA = Path(temporary)
        service.GRAPH_CACHE.clear()
        try:
            result = _audit_copy(asset_id, original, terrain_hash)
        finally:
            store.DATA = previous
            service.GRAPH_CACHE.clear()
    return result


def _audit_copy(asset_id, state, terrain_hash):
    started = time.perf_counter()
    controls = _course_controls(state)
    reg = Registration.from_dict(state['registration'])
    graph = service.get_graph(state)
    marker = _start_finish(state, reg)
    reasons = []
    start = finish = None
    if marker is None:
        reasons.append({'id': 'course', 'message': '未识别出唯一的起终点符号，不能自动认定完整课程'})
    else:
        first, last = marker
        start = first.get('approachPixel') or first['pixel']
        finish = last.get('approachPixel') or last['pixel']
        _, inside = reg.forward([start, finish])
        if not inside.all():
            reasons.append({'id': 'course', 'message': '起终点符号超出有效配准范围'})
        else:
            try:
                graph.snap(start); graph.snap(finish)
            except ValueError as exc:
                reasons.append({'id': 'course', 'message': f'起终点不可达：{exc}'})

    bundle = None
    if start is not None:
        bundle = plan_matrix(graph, controls, start, finish, reg)
        for entry in bundle['unresolved']:
            reasons.append({'id': entry['id'], 'message': entry['message']})

    plan = None
    routes = []
    if bundle is not None and not bundle['unresolved']:
        request = {'assetId': asset_id, 'version': state['version'],
                   'controlIds': [c['id'] for c in controls],
                   'startPixel': start, 'finishPixel': finish,
                   'mode': 'all_visit', 'objective': 'min_max_finish_then_sum',
                   'teams': [dict(TEAM)], 'timeLimitSec': 10}
        plan = service.plan_course(request)
        if plan['status'] == 'NEEDS_REVIEW':
            for entry in plan['unresolved']:
                reasons.append({'id': entry['id'], 'message': entry['message']})
        else:
            ids = [c['id'] for c in controls]
            for route in plan['routes']:
                order = route['order']
                if sorted(route['controlIds']) != sorted(ids):
                    raise AssertionError(f'{asset_id}: route misses controls')
                route['checkedEdges'] = _check_edges(graph, bundle, order)
                routes.append({'controlIds': route['controlIds'], 'distanceM': round(route['distanceM'], 1),
                               'durationSec': round(route['durationSec'], 1),
                               'checkedEdges': route['checkedEdges']})

    approaches = []
    if bundle is not None:
        for approach in bundle.get('approaches', []):
            approaches.append({k: approach[k] for k in
                               ('controlId', 'method', 'automatic', 'offsetM') if k in approach})
    automatic = [a for a in approaches if a['automatic']]
    routable = plan is not None and plan['status'] != 'NEEDS_REVIEW' and not reasons and bool(routes)
    p95 = (state.get('quality') or {}).get('p95ErrorM')
    assert store.file_hash(store.DATA/'projects'/asset_id/'terrain.npz') == terrain_hash, 'terrain changed under audit'
    return {'assetId': asset_id, 'status': ('ROUTABLE' if routable else 'NOT ROUTABLE'),
            'routable': routable, 'controls': len(controls), 'resolved': len(controls)-len({r['id'] for r in reasons if r['id'] != 'course'}),
            'unresolved': [r for r in reasons if r['id'] != 'course'],
            'courseProblem': next((r['message'] for r in reasons if r['id'] == 'course'), None),
            'startFinish': None if start is None else {'startLabel': marker[0]['label'], 'startPixel': [round(v, 2) for v in start],
                                                       'finishLabel': marker[1]['label'], 'finishPixel': [round(v, 2) for v in finish],
                                                       'method': 'detected_course_symbols'},
            'planStatus': None if plan is None else plan['status'],
            'routes': routes, 'approaches': approaches, 'automaticApproaches': len(automatic),
            'reviewRequiredApproaches': sum(bool(a['automatic']) for a in approaches),
            'registrationP95M': p95, 'graphNodes': len(graph.pixels), 'graphEdges': graph.weights.nnz,
            'graphHash': graph.content_hash, 'elapsedSec': round(time.perf_counter()-started, 2)}


def _cell(values):
    return ' | '.join(str(v) for v in values)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--asset', action='append', choices=ASSETS, help='audit only this map (repeatable)')
    parser.add_argument('--json', help='also write the full report to this path')
    args = parser.parse_args()
    assets = args.asset or list(ASSETS)
    report = {'auditedAt': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'assets': assets,
              'scope': 'Detected course, one team, all controls required. Automatic approaches need review.',
              'maps': []}
    for asset_id in assets:
        print(f'{asset_id}: auditing', flush=True)
        report['maps'].append(audit(asset_id))

    header = ('map', 'controls', 'resolved', 'status', 'plan', 'nodes', 'edges', 'p95 m', 'auto approaches', 'unresolved reasons')
    rows = [header]
    for entry in report['maps']:
        reasons = '; '.join(f"{r['id']}: {r['message']}" for r in entry.get('unresolved', []))
        if entry.get('courseProblem'):
            reasons = (reasons+'; ' if reasons else '')+entry['courseProblem']
        if entry['status'] == 'NOT ROUTABLE' and not entry['unresolved'] and not entry.get('courseProblem'):
            reasons = '完整路线返回 NEEDS_REVIEW 但没有逐点原因' if entry.get('planStatus') == 'NEEDS_REVIEW' else '未生成完整路线'
        rows.append((entry['assetId'], entry.get('controls', 0), entry.get('resolved', 0), entry['status'],
                     entry.get('planStatus') or '-', entry.get('graphNodes', 0), entry.get('graphEdges', 0),
                     entry.get('registrationP95M', '-'), entry.get('automaticApproaches', 0), reasons or '-'))
    widths = [max(len(str(row[i])) for row in rows) for i in range(len(header))]
    for row in rows:
        print('  '.join(str(v).ljust(widths[i]) for i, v in enumerate(row)).rstrip(), flush=True)

    for entry in report['maps']:
        if entry['status'] == 'ROUTABLE' and entry['automaticApproaches']:
            print(f"{entry['assetId']}: {entry['automaticApproaches']} automatic approach(es) still need field review", flush=True)
    report['allRoutable'] = all(entry['routable'] for entry in report['maps'])
    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(f"RESULT: {'all maps fully routable' if report['allRoutable'] else 'NOT all maps fully routable'}", flush=True)
    return 0 if report['allRoutable'] else 1


if __name__ == '__main__':
    sys.exit(main())
