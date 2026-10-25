#!/usr/bin/env python3
"""Paired solver-quality and team-recommendation checks on fixed inputs.

All graph paths come from the existing prepared map. The baseline is loaded
from a Git revision and receives the exact same matrix, teams and budgets.
Timings exclude graph preparation and imports; this is a quality comparison,
not an end-to-end API latency claim. Teaching data is read-only.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import numpy as np
from dirmap import service, store, optimizer, team_planning
from dirmap.geometry import Registration
from dirmap.graph import expand_route, plan_matrix


def old_module(reference, name, directory):
    path = directory/(name+'.py')
    path.write_bytes(subprocess.check_output(['git', 'show', f'{reference}:dirmap/{name}.py'], cwd=ROOT))
    spec = importlib.util.spec_from_file_location('dirmap.baseline_'+name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', default='6d82914')
    parser.add_argument('--output', default='evidence/runtime/route-quality.json')
    args = parser.parse_args()
    state = store.get_state('szu-3')
    before = (store.asset_dir('szu-3')/'state.json').read_bytes()
    graph = service.get_graph(state)
    controls = [c for c in state['controls'] if c.get('role', 'control') == 'control' and c['state'] != 'rejected']
    start = next(c['pixel'] for c in state['controls'] if c.get('role') == 'start_finish')
    bundle = plan_matrix(graph, controls, start, start, Registration.from_dict(state['registration']))
    assert not bundle['unresolved']
    mandatory = [next(c['id'] for c in controls if c['label'] == label) for label in ('31', '32', '33')]
    teams = [{'id': str(i), 'people': 5, 'speedMps': 1.4, 'departureSec': 0} for i in range(4)]
    report = {'auditedAt': datetime.now(timezone.utc).isoformat(), 'scope': __doc__,
              'platform': platform.platform(), 'python': platform.python_version(),
              'baseline': subprocess.check_output(['git', 'rev-parse', args.baseline], cwd=ROOT, text=True).strip(),
              'graphHash': graph.content_hash, 'sourceHash': state['sourceHash'],
              'matrixHash': hashlib.sha256(bundle['matrix'].tobytes()).hexdigest(), 'quality': [], 'recommendations': []}
    edges = set()
    with tempfile.TemporaryDirectory(prefix='dirmap-quality-baseline-') as temporary:
        old = old_module(args.baseline, 'optimizer', Path(temporary))
        old_teams = old_module(args.baseline, 'team_planning', Path(temporary))
        for name in ('solve', 'subset_tours', 'evaluate_routes', 'route_cost', 'balanced_people'):
            setattr(old_teams, name, getattr(old, name))
        cases = []
        for count, groups in ((20, 20), (30, 10)):
            points = np.random.default_rng(530).random((count+2, 2))*200
            matrix = np.linalg.norm(points[:, None]-points[None, :], axis=2)
            targets = [{'id': str(i)} for i in range(count)]
            participants = [{'id': str(i), 'people': 2+i % 3, 'speedMps': .7+i*.05, 'departureSec': i*3} for i in range(groups)]
            cases.append((f'synthetic-{count}-points-{groups}-teams', matrix, targets, participants, 'split_coverage', {}))
        cases.append(('szu-3-16-points-free-meetings', bundle['matrix'], controls, teams, 'mixed_coverage', {'mandatory_ids': mandatory}))
        for name, matrix, targets, participants, mode, options in cases:
            for repetition in range(2):
                row = {'case': name, 'repetition': repetition, 'solverBudgetSec': 5}
                for label, function in (('before', old.solve), ('after', optimizer.solve)):
                    started = time.perf_counter()
                    result = function(matrix, targets, participants, mode, time_limit=5, **options)
                    row[label] = {'seconds': time.perf_counter()-started, 'status': result['status'],
                                  'objective': list(map(float, result['proof']['objectiveValue']))}
                    if name.startswith('szu-3'):
                        for route in result['routes']:
                            edges.update(tuple(e) for e in expand_route(graph, bundle, route['order'])['edgeIds'])
                row['primaryImprovementPercent'] = 100*(row['before']['objective'][0]-row['after']['objective'][0])/row['before']['objective'][0]
                report['quality'].append(row)
                print(row, flush=True)
        distance_cache = {}
        def measure(result, participants):
            total = 0.
            for route in result['routes']:
                for a, b in zip(route['order'], route['order'][1:]):
                    if (a, b) not in distance_cache:
                        path = graph.expand(bundle['parents'][a], bundle['nodes'][a], bundle['nodes'][b])
                        length = float(graph.lengths[path[:-1], path[1:]].sum()) if len(path) > 1 else 0.
                        distance_cache[a, b] = length+float(bundle['connectors'][a]+bundle['connectors'][b])
                    total += distance_cache[a, b]
            return {'totalDistanceM': total, 'latestFinishSec': max(r['finishSec'] for r in result['routes']),
                    'totalFinishSec': sum(r['finishSec'] for r in result['routes']),
                    'totalWaitSec': sum(r['waitSec'] for r in result['routes'])}
        request = {'mode': 'mixed_coverage', 'objective': 'min_max_finish_then_sum', 'mandatoryControlIds': mandatory,
                   'mandatoryOrder': False, 'totalPeople': 20, 'minPeoplePerTeam': 2, 'maxTeams': 10,
                   'speedMps': 1.4, 'staggerSec': 0, 'targetGainFraction': .9}
        for label, module, budgets in (('before', old_teams, (5,)), ('after', team_planning, (5, 5, 30))):
            for budget in budgets:
                started = time.perf_counter()
                result, participants = module.recommend_teams(bundle['matrix'], controls, request, budget, measure)
                rec = result['teamRecommendation']
                row = {'implementation': label, 'budgetSec': budget, 'seconds': time.perf_counter()-started,
                       'recommendedCount': rec['recommendedCount'], 'bestCount': rec['bestMetricCount'],
                       'recommendedFinishSec': rec['recommendedValue'], 'gainPercent': rec['gainPercent'],
                       'allCountsOptimal': rec['allCountsOptimalityProven'], 'candidates': rec['candidates']}
                report['recommendations'].append(row)
                for route in result['routes']:
                    edges.update(tuple(e) for e in expand_route(graph, bundle, route['order'])['edgeIds'])
                print({k: v for k, v in row.items() if k != 'candidates'}, flush=True)
    for a, b in edges:
        assert graph.weights[a, b] > 0 and graph.clear_segment(graph.pixels[a], graph.pixels[b])
    report['legalEdgesChecked'] = len(edges)
    report['existingProjectUnchanged'] = (store.asset_dir('szu-3')/'state.json').read_bytes() == before
    assert report['existingProjectUnchanged']
    report['implementationSignature'] = service.implementation_signature()
    store.atomic_json(Path(args.output), report)


if __name__ == '__main__':
    main()
