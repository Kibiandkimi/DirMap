#!/usr/bin/env python3
"""Compare insertion speed, then plan all real controls across team counts.

The insertion fixture is synthetic; real-map planning runs in a disposable
copy. Graph loading, matrix construction, count comparison and path expansion
are timed together. Independent edge checks follow the planning timer.
"""
import argparse
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import platform
import resource
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import numpy as np
from dirmap import service, store
from dirmap.optimizer import solve


def insertion_benchmark(reference, directory):
    path = directory/'old_optimizer.py'
    path.write_bytes(subprocess.check_output(['git', 'show', reference+':dirmap/optimizer.py'], cwd=ROOT))
    spec = importlib.util.spec_from_file_location('dirmap.previous_optimizer', path)
    old = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(old)
    points = np.random.default_rng(472).random((32, 2))*1000
    matrix = np.linalg.norm(points[:, None]-points[None, :], axis=2)
    controls = [{'id': str(i)} for i in range(30)]
    teams = [{'id': str(i), 'people': 4, 'speedMps': 1.2+i*.03, 'departureSec': i*8} for i in range(10)]
    rows = []
    for mode in ('split_coverage', 'mixed_coverage'):
        options = {'mandatory_ids': ['0', '10', '20'], 'mandatory_order': True} if mode == 'mixed_coverage' else {}
        row = {'mode': mode}
        for name, function in (('before', old.solve), ('after', solve)):
            started = time.perf_counter()
            plan = function(matrix, controls, teams, mode, time_limit=.000001, **options)
            row[name] = {'elapsedSec': time.perf_counter()-started, 'objective': plan['proof']['objectiveValue']}
        row['sameObjective'] = bool(np.allclose(row['before']['objective'], row['after']['objective'], rtol=0, atol=1e-7))
        row['speedup'] = row['before']['elapsedSec']/row['after']['elapsedSec']
        rows.append(row)
    return {'baselineCommit': reference, 'seed': 472, 'controls': 30, 'teams': 10,
            'scope': 'Feasible insertion only; time budget disables later local/exact search. This is not total API speed.', 'cases': rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--asset', default='szu-3')
    parser.add_argument('--seconds', type=float, default=5)
    parser.add_argument('--baseline-ref', default='7fda6e1')
    parser.add_argument('--output', default='evidence/runtime/team-planning-performance.json')
    args = parser.parse_args()
    before = (store.asset_dir(args.asset)/'state.json').read_bytes()
    state = json.loads(before)
    previous = store.DATA
    report = {'auditedAt': datetime.now(timezone.utc).isoformat(), 'scope': __doc__,
              'platform': platform.platform(), 'python': platform.python_version(), 'cases': []}
    with tempfile.TemporaryDirectory(prefix='dirmap-team-benchmark-') as temporary:
        scratch = Path(temporary)
        report['insertion'] = insertion_benchmark(args.baseline_ref, scratch)
        data = scratch/'data'
        copied = data/'projects'/args.asset
        copied.mkdir(parents=True)
        store.atomic_json(copied/'state.json', state)
        terrain = copied/state.get('artifacts', {}).get('terrain.npz', 'terrain.npz')
        terrain.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(store.artifact_path(state, 'terrain.npz'), terrain)
        parent = (state.get('quality') or {}).get('baseAssetId')
        seen = {args.asset}
        while parent and parent not in seen:
            seen.add(parent)
            base = store.get_state(parent)
            store.atomic_json(data/'projects'/parent/'state.json', base)
            parent = (base.get('quality') or {}).get('baseAssetId')
        controls = [c for c in state['controls'] if c.get('role', 'control') == 'control' and c['state'] != 'rejected']
        start = next(c for c in state['controls'] if c.get('role') in ('start', 'start_finish'))
        finish = next(c for c in state['controls'] if c.get('role') in ('finish', 'start_finish'))
        mandatory = [next(c['id'] for c in controls if c['label'] == label) for label in ('31', '32', '33')]
        store.DATA = data
        service.GRAPH_CACHE.clear()
        try:
            for mode in ('all_visit', 'mixed_coverage'):
                payload = {'assetId': args.asset, 'version': state['version'], 'controlIds': [c['id'] for c in controls],
                           'startPixel': start.get('approachPixel') or start['pixel'],
                           'finishPixel': finish.get('approachPixel') or finish['pixel'],
                           'mode': mode, 'objective': 'min_max_finish_then_sum', 'totalPeople': 20,
                           'minPeoplePerTeam': 2, 'maxTeams': 10, 'speedMps': 1.4, 'staggerSec': 0,
                           'targetGainFraction': .9, 'mandatoryControlIds': mandatory if mode == 'mixed_coverage' else [],
                           'mandatoryOrder': mode == 'mixed_coverage', 'timeLimitSec': args.seconds}
                started = time.perf_counter()
                result = service.plan_course(payload, auto_teams=True)
                elapsed = time.perf_counter()-started
                assert result['status'] in ('OPTIMAL', 'FEASIBLE'), result.get('unresolved')
                graph = service.get_graph(store.get_state(args.asset))
                edges = {tuple(edge) for route in result['routes'] for edge in route['edgeIds']}
                for a, b in edges:
                    assert graph.weights[a, b] > 0 and graph.clear_segment(graph.pixels[a], graph.pixels[b])
                record = {'mode': mode, 'cache': 'cold_graph' if mode == 'all_visit' else 'warm_graph',
                          'sourceHash': state['sourceHash'], 'graphHash': graph.content_hash,
                          'realControls': len(controls), 'totalPlanningSec': elapsed,
                          'underBudget': elapsed <= args.seconds, 'checkedEdgesAfterTiming': len(edges),
                          'recommendation': result['teamRecommendation'], 'proof': result['proof']}
                report['cases'].append(record)
                print({'mode': mode, 'seconds': round(elapsed, 3), 'recommendedTeams': result['teamRecommendation']['recommendedCount'],
                       'checkedEdges': len(edges)}, flush=True)
        finally:
            store.DATA = previous
            service.GRAPH_CACHE.clear()
    report['existingProjectUnchanged'] = (store.asset_dir(args.asset)/'state.json').read_bytes() == before
    report['implementationSignature'] = service.implementation_signature()
    report['peakRSSMiB'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024
    report['cacheScope'] = 'Cold means no in-process graph; the prepared, bounded graph-file cache may be used. Both modes build a point matrix once and reuse it across all candidate team counts.'
    store.atomic_json(Path(args.output), report)
    assert report['existingProjectUnchanged'] and all(c['sameObjective'] for c in report['insertion']['cases'])


if __name__ == '__main__':
    main()
