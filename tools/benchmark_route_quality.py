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
import itertools
import json
from pathlib import Path
import platform
import subprocess
import statistics
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import numpy as np
from dirmap import exact, service, store, optimizer, team_planning
from dirmap.geometry import Registration
from dirmap.graph import expand_route, plan_matrix
from dirmap.route_scores import RouteScores
from dirmap.route_search import initial_routes


def baseline_modules(reference, directory):
    # Freeze the complete relative-import chain. Loading only an old
    # optimizer as dirmap.baseline_optimizer would silently use today's
    # route_search/route_scores and invalidate a before/after comparison.
    paths = subprocess.check_output(['git', 'ls-tree', '-r', '--name-only', reference, 'dirmap'], cwd=ROOT, text=True).splitlines()
    for filename in paths:
        relative = Path(filename).relative_to('dirmap')
        if relative.suffix not in ('.py', '.cpp', '.json'):
            continue
        path = directory/relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(subprocess.check_output(['git', 'show', f'{reference}:{filename}'], cwd=ROOT))
    name = 'dirmap_quality_baseline'
    spec = importlib.util.spec_from_file_location(name, directory/'__init__.py', submodule_search_locations=[str(directory)])
    package = importlib.util.module_from_spec(spec)
    sys.modules[name] = package
    spec.loader.exec_module(package)
    return importlib.import_module(name+'.optimizer'), importlib.import_module(name+'.team_planning')


def scoring_comparison(old_scores):
    """Time the same physical relocations, with all candidate copies prepared."""
    points = np.random.default_rng(530).random((32, 2))*200
    matrix = np.linalg.norm(points[:, None]-points[None, :], axis=2)
    teams = [{'speedMps': .7+i*.05, 'departureSec': i*3} for i in range(10)]
    service = np.array([[7+i % 3]*30 for i in range(10)], float)
    results = []
    for common in ((), (1, 2, 3)):
        routes = initial_routes(matrix, teams, service, 'min_max_finish_then_sum', common)
        scores = RouteScores(matrix, routes, teams, service, 'min_max_finish_then_sum', common)
        previous = old_scores(matrix, routes, teams, service, 'min_max_finish_then_sum', common)
        candidates = []
        for a, b in itertools.permutations(range(len(teams)), 2):
            if len(routes[a]) <= 3:
                continue
            for i in range(1, len(routes[a])-1):
                node = routes[a][i]
                if node in common:
                    continue
                left, right = routes[a][i-1], routes[a][i+1]
                remove = matrix[left, right]-matrix[left, node]-matrix[node, right]
                for j in range(1, len(routes[b])):
                    left, right = routes[b][j-1], routes[b][j]
                    insert = matrix[left, node]+matrix[node, right]-matrix[left, right]
                    source, target = routes[a].copy(), routes[b].copy()
                    target.insert(j, source.pop(i))
                    updates = [(a, scores.stages[a][i-1], remove/teams[a]['speedMps']-service[a, node-1]),
                               (b, scores.stages[b][j-1], insert/teams[b]['speedMps']+service[b, node-1])]
                    candidates.append((remove+insert, updates, {a: source, b: target}))
        elapsed = {'before': [], 'after': []}
        baseline_incremental = hasattr(previous, 'changes')
        for _ in range(3):
            started = time.perf_counter()
            before = np.array([previous.changes(delta, updates) if baseline_incremental else previous.replacements(replacements)
                               for delta, updates, replacements in candidates])
            elapsed['before'].append(time.perf_counter()-started)
            started = time.perf_counter()
            after = np.array([scores.changes(delta, updates) for delta, updates, _ in candidates])
            elapsed['after'].append(time.perf_counter()-started)
            assert np.allclose(before, after, rtol=0, atol=1e-8)
        old_time, new_time = map(statistics.median, (elapsed['before'], elapsed['after']))
        results.append({'mandatoryCount': len(common), 'candidateMoves': len(candidates), 'beforeMedianSec': old_time,
                        'afterMedianSec': new_time, 'speedup': old_time/new_time,
                        'baselineScoring': 'incremental' if baseline_incremental else 'full_profiles',
                        'maxAbsoluteScoreDifference': float(np.max(np.abs(before-after))),
                        'scope': 'Identical candidate scoring only; excludes preparing moves, route search and map loading.'})
    return results


def incumbent_comparison(old_exact):
    """Reproduce rounding and interrupted lexicographic-stage regressions."""
    result = []
    matrix = np.full((4, 4), 10., float)
    np.fill_diagonal(matrix, 0)
    for a, b in ((0, 1), (1, 2), (2, 3)):
        matrix[a, b] = 1.00049
    matrix[0, 2], matrix[2, 1], matrix[1, 3] = 1.00051, 1., 1.
    cases = [('rounding_reversal', matrix, 1, [[0, 2, 1, 3]], 'all_visit', 'min_distance')]
    matrix = np.random.default_rng(1).integers(1, 80, (7, 7)).astype(float)
    np.fill_diagonal(matrix, 0)
    cases.append(('first_stage_only', matrix, 2, [[0, 5, 3, 6], [0, 1, 2, 4, 6]],
                  'split_coverage', 'min_max_finish_then_sum'))
    for name, matrix, count, seed, mode, objective in cases:
        controls = [{'id': str(i), 'serviceBaseSec': 0, 'servicePerPersonSec': 0} for i in range(len(matrix)-2)]
        teams = [{'id': str(i), 'people': 2, 'speedMps': 1} for i in range(count)]
        def actual(routes):
            # Independent complete walks; these fixtures have no meetings or
            # service time, so no incremental score cache is involved.
            costs = [float(sum(matrix[a, b] for a, b in zip(route, route[1:]))) for route in routes]
            return [sum(costs)] if objective == 'min_distance' else [max(costs), sum(costs)]
        row = {'case': name, 'incumbent': actual(seed)}
        for label, module in (('before', old_exact), ('after', exact)):
            run = module._run
            def controlled_run(model, deadline):
                solver, status = run(model, deadline)
                if name == 'first_stage_only' and status == module.cp_model.OPTIMAL:
                    status = module.cp_model.FEASIBLE
                return solver, status
            with patch.object(module, '_run', controlled_run):
                solved = module.optimize(matrix, controls, teams, mode, objective, seed, time.perf_counter()+2)
            row[label] = {'actualObjectiveValue': actual(solved['sequences']), 'returnedRouteProven': solved['proven'],
                          'modelObjectiveValue': solved['proof']['modelObjectiveValue'],
                          'bestBound': solved['proof']['bestBound'], 'stages': solved['proof']['stages']}
        assert row['after']['actualObjectiveValue'] <= row['incumbent']
        row['scope'] = 'First-stage case uses a real CP-SAT result but reports FEASIBLE to skip the second stage, simulating interruption; rounding case runs to completion.'
        result.append(row)
    return result


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
        old, old_teams = baseline_modules(args.baseline, Path(temporary))
        report['baselinePlanningHashes'] = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in Path(temporary).glob('*.py') if path.stem in ('optimizer', 'exact', 'route_search', 'route_scores', 'schedule', 'team_planning')}
        report['candidateScoring'] = scoring_comparison(importlib.import_module(old.__package__+'.route_scores').RouteScores)
        for row in report['candidateScoring']:
            print({'candidateScoring': row}, flush=True)
        report['incumbentRetention'] = incumbent_comparison(importlib.import_module(old.__package__+'.exact'))
        for row in report['incumbentRetention']:
            print({'incumbentRetention': row}, flush=True)
        cases = []
        for count, groups in ((20, 20), (30, 10)):
            points = np.random.default_rng(530).random((count+2, 2))*200
            matrix = np.linalg.norm(points[:, None]-points[None, :], axis=2)
            targets = [{'id': str(i)} for i in range(count)]
            participants = [{'id': str(i), 'people': 2+i % 3, 'speedMps': .7+i*.05, 'departureSec': i*3} for i in range(groups)]
            cases.append((f'synthetic-{count}-points-{groups}-teams', matrix, targets, participants, 'split_coverage', {}, None))
        cases.append(('szu-3-16-points-free-meetings', bundle['matrix'], controls, teams, 'mixed_coverage', {'mandatory_ids': mandatory}, bundle))
        component = graph.components[graph.snap(start)]
        nodes = np.flatnonzero(graph.components == component)
        sampled = np.random.default_rng(1047).choice(nodes, size=30, replace=False)
        targets = [{'id': f'performance-{i}', 'pixel': graph.pixels[node].tolist(), 'state': 'candidate'}
                   for i, node in enumerate(sampled)]
        participants = [{'id': f'group-{i}', 'people': 4+i % 3, 'speedMps': 1.2+(i % 2)*.2, 'departureSec': i*5}
                        for i in range(10)]
        large_bundle = plan_matrix(graph, targets, start, start, Registration.from_dict(state['registration']))
        assert not large_bundle['unresolved']
        cases.append(('szu-3-graph-30-sampled-points-10-teams', large_bundle['matrix'], targets, participants, 'split_coverage', {}, large_bundle))
        report['sampledCourseScope'] = 'The 30 graph nodes are a synthetic performance course on real terrain, not teacher controls or recognition evidence.'
        for name, matrix, targets, participants, mode, options, path_bundle in cases:
            for repetition in range(2):
                row = {'case': name, 'repetition': repetition, 'solverBudgetSec': 5}
                for label, function in (('before', old.solve), ('after', optimizer.solve)):
                    started = time.perf_counter()
                    result = function(matrix, targets, participants, mode, time_limit=5, **options)
                    row[label] = {'seconds': time.perf_counter()-started, 'status': result['status'],
                                  'objective': list(map(float, result['proof']['objectiveValue']))}
                    if path_bundle is not None:
                        for route in result['routes']:
                            edges.update(tuple(e) for e in expand_route(graph, path_bundle, route['order'])['edgeIds'])
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
