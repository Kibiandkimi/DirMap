#!/usr/bin/env python3
"""Audit complete detected courses before reporting any connected subset.

Native-pixel graph checks and automatic approaches are not an independent
recognition benchmark, a field-accuracy audit, or a phone sensor test.
"""
from collections import Counter
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dirmap import store
from dirmap.geometry import Frame, Registration
from dirmap.graph import expand_route, plan_matrix
from dirmap.optimizer import solve
from dirmap.service import get_graph


def check_routes(graph, reg, controls, start, finish):
    bundle = plan_matrix(graph, controls, start, finish, reg)
    assert not bundle['unresolved'], bundle['unresolved']
    for endpoint, node in zip(bundle['endpoints'], bundle['nodes']):
        assert graph.clear_segment(endpoint.get('approachPixel') or endpoint['pixel'], graph.pixels[node])
    scenarios, checked = [], set()
    for mode in ('all_visit', 'fixed_order', 'split_coverage'):
        for count in (1, 2, 5, 10):
            if mode == 'split_coverage' and count > len(controls):
                continue
            teams = [{'id': str(i+1), 'people': 3+(i % 2), 'speedMps': 1.2+(i % 3)*.15,
                      'departureSec': i*10} for i in range(count)]
            result = solve(bundle['matrix'], controls, teams, mode, time_limit=3)
            expected = [c['id'] for c in controls]
            if mode == 'split_coverage':
                assert sorted(c for r in result['routes'] for c in r['controlIds']) == sorted(expected)
            else:
                assert all(sorted(r['controlIds']) == sorted(expected) for r in result['routes'])
            if mode == 'fixed_order':
                assert all(r['controlIds'] == expected for r in result['routes'])
            edge_count = 0
            for route in result['routes']:
                geometry = expand_route(graph, bundle, route['order'])
                for a, b in geometry['edgeIds']:
                    key = tuple(sorted((a, b)))
                    if key not in checked:
                        assert graph.weights[a, b] > 0 and graph.clear_segment(graph.pixels[a], graph.pixels[b]), key
                        checked.add(key)
                    edge_count += 1
            scenarios.append({'mode': mode, 'teams': count, 'controls': len(controls), 'status': result['status'],
                              'validEdgesChecked': edge_count, 'solverElapsedSec': round(result['proof']['elapsedSec'], 3)})
    return scenarios, len(checked)


def main():
    out = store.ROOT/'evidence'/'runtime'
    out.mkdir(parents=True, exist_ok=True)
    manifest = store.read_json(store.ROOT/'evidence'/'input-manifest.json')
    originals = [{'file': a['file'], 'unchanged': store.file_hash(store.ROOT/a['file']) == a['sha256']} for a in manifest['assets']]
    assert all(a['unchanged'] for a in originals), 'An original source file changed'
    report = {'auditedAt': datetime.now(timezone.utc).isoformat(), 'originals': originals, 'maps': [],
              'scope': 'All detected normal controls and detected start/finish; full input checked before any connected subset. Automatic approaches need review. Not a manual or field benchmark.',
              'fullObjectiveAchieved': False}
    fixture = None
    for asset in store.catalog():
        state = store.get_state(asset['id'])
        if not state or not state.get('registration'):
            report['maps'].append({'id': asset['id'], 'status': 'unregistered'})
            continue
        assert state['sourceHash'] == asset['sha256'], f'{asset["id"]}: state does not describe the real source'
        started = time.perf_counter()
        graph, reg = get_graph(state), Registration.from_dict(state['registration'])
        controls = [c for c in state['controls'] if c.get('role', 'control') == 'control' and c['state'] != 'rejected']
        starts = [c for c in state['controls'] if c.get('role') in ('start', 'start_finish')]
        finishes = [c for c in state['controls'] if c.get('role') in ('finish', 'start_finish')]
        start = (starts[0].get('approachPixel') or starts[0]['pixel']) if len(starts) == 1 else None
        finish = (finishes[0].get('approachPixel') or finishes[0]['pixel']) if len(finishes) == 1 else None
        full = plan_matrix(graph, controls, start, finish, reg) if start and finish else {
            'unresolved': [{'id': 'course', 'message': '未识别出唯一的起终点'}]}
        component = Counter(graph.components.tolist()).most_common(1)[0][0]
        if start:
            try:
                component = graph.components[graph.snap(start)]
            except ValueError:
                pass
        reachable, unresolved, approaches = [], [], []
        for control in controls:
            try:
                if graph.access_resolver:
                    approach = graph.access_resolver.resolve(control, component)
                else:
                    node = graph.snap(control.get('approachPixel') or control['pixel'])
                    if graph.components[node] != component:
                        raise ValueError('目标所在区域与起点不连通')
                    approach = {'pixel': control.get('approachPixel') or control['pixel'], 'method': 'exact', 'automatic': False, 'offsetM': 0}
                reachable.append(control)
                approaches.append({'id': control['id'], 'label': control['label'], **{k: v for k, v in approach.items() if k != 'node'}})
            except ValueError as exc:
                unresolved.append({'id': control['id'], 'label': control['label'], 'reason': str(exc)})
        entry = {'id': asset['id'], 'sourceHash': state['sourceHash'], 'stateVersion': state['version'],
                 'courseSymbolCandidates': len(state['controls']), 'controlCandidates': len(controls),
                 'calibratedControls': sum(c.get('calibrated', False) for c in controls),
                 'ocr': state['statistics'].get('ocr'), 'connectedControls': len(reachable),
                 'unresolved': unresolved, 'approaches': approaches, 'fullCourseUnresolved': full['unresolved'],
                 'fullCourseAutomaticallyRoutable': not full['unresolved'], 'detectedStartPixel': start, 'detectedFinishPixel': finish,
                 'graphHash': graph.content_hash, 'graphNodes': len(graph.pixels), 'directedEdges': graph.weights.nnz,
                 'quality': {k: v for k, v in state['quality'].items() if k != 'landmarks'}, 'routeChecks': []}
        if reachable:
            if not start or not finish:
                test_start = test_finish = graph.pixels[graph.snap(approaches[0]['pixel'])].tolist()
            else:
                test_start, test_finish = start, finish
            entry['routeCheckScope'] = 'full_detected_course' if not full['unresolved'] else 'connected_subset_only'
            entry['routeChecks'], entry['uniqueLegalEdgesChecked'] = check_routes(graph, reg, reachable, test_start, test_finish)
            if fixture is None or (not full['unresolved'] and fixture['scope'] != 'full_detected_course'):
                world, _ = reg.forward([test_start])
                fixture = {'assetId': asset['id'], 'version': state['version'], 'name': state['name'],
                           'controlIds': [c['id'] for c in reachable], 'startPixel': test_start, 'finishPixel': test_finish,
                           'startLnglat': Frame().to_lnglat(world[0]).tolist(), 'scope': entry['routeCheckScope']}
        entry['elapsedSec'] = round(time.perf_counter()-started, 3)
        report['maps'].append(entry)
        print({k: entry[k] for k in ('id', 'controlCandidates', 'calibratedControls', 'connectedControls', 'fullCourseAutomaticallyRoutable', 'elapsedSec')}, flush=True)
    files = sorted((store.ROOT/'dirmap').glob('*.py'))+sorted((store.ROOT/'web'/'src').glob('*.ts'))
    report['implementationHash'] = hashlib.sha256(b''.join(p.read_bytes() for p in files)).hexdigest()
    store.atomic_json(out/'real-map-verification.json', report)
    if fixture:
        store.atomic_json(out/'browser-course.json', fixture)
    print(out/'real-map-verification.json')


if __name__ == '__main__':
    main()
