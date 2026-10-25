#!/usr/bin/env python3
"""Continue and confirm real-map plans in a disposable workbench.

The existing workbench is read-only and no classroom is published. Review
is simulated to test route preservation, not to claim field passage or
registration accuracy. Graph-edge checks use the prepared terrain model.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import resource
import shutil
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dirmap import comparisons, service, store
from dirmap.route_scores import improves


def timed(function, *args, **kwargs):
    started = time.perf_counter()
    result = function(*args, **kwargs)
    return result, time.perf_counter()-started


def simulate_review(plan):
    state = store.get_state(plan['assetId'])
    positions = {a['controlId']: a['pixel'] for a in plan['approaches'] if a['automatic']}
    for control in state['controls']:
        control['required'] = control['id'] in plan['request']['controlIds']
        if control['required']:
            control['state'] = 'accepted'
            if control['id'] in positions:
                control['approachPixel'] = positions[control['id']]
    state.update(version=state['version']+1, reviewed=True,
                 review={'registrationChecked': True, 'controlsChecked': True, 'passagesChecked': True})
    service.project_controls(state)
    store.save_state(state['assetId'], state)
    service.GRAPH_CACHE.clear()
    return state['version']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='evidence/runtime/plan-refinement.json')
    args = parser.parse_args()
    live_data = store.DATA
    original = store.get_state('szu-3')
    before = (store.asset_dir('szu-3')/'state.json').read_bytes()
    rows, edges = [], set()
    with tempfile.TemporaryDirectory(prefix='dirmap-refinement-') as directory:
        data = Path(directory)/'data'
        project = data/'projects/szu-3'
        store.atomic_json(project/'state.json', original)
        terrain = project/original.get('artifacts', {}).get('terrain.npz', 'terrain.npz')
        terrain.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(store.artifact_path(original, 'terrain.npz'), terrain)
        parent, seen = (original.get('quality') or {}).get('baseAssetId'), {'szu-3'}
        while parent and parent not in seen:
            seen.add(parent)
            state = store.get_state(parent)
            assert state
            store.atomic_json(data/'projects'/parent/'state.json', state)
            parent = (state.get('quality') or {}).get('baseAssetId')
        controls = [c for c in original['controls'] if c.get('role', 'control') == 'control' and c['state'] != 'rejected']
        start = next(c for c in original['controls'] if c.get('role') == 'start_finish')
        endpoint = start.get('approachPixel') or start['pixel']
        base = {'assetId': 'szu-3', 'controlIds': [c['id'] for c in controls], 'startPixel': endpoint, 'finishPixel': endpoint,
                'mode': 'mixed_coverage', 'objective': 'min_max_finish_then_sum', 'timeLimitSec': 5,
                'mandatoryControlIds': [next(c['id'] for c in controls if c['label'] == label) for label in ('31', '32', '33')],
                'mandatoryOrder': False}
        store.DATA = data
        service.GRAPH_CACHE.clear()
        try:
            for kind, budget in (('fixed_team_count', 30), ('compared_team_count', 5)):
                request = {**base, 'version': store.get_state('szu-3')['version']}
                if kind == 'fixed_team_count':
                    request['teams'] = [{'id': str(i), 'people': 5, 'speedMps': 1.4, 'departureSec': 0} for i in range(4)]
                    source, initial_elapsed = timed(service.plan_course, request)
                else:
                    request.update(totalPeople=20, minPeoplePerTeam=2, maxTeams=10, speedMps=1.4, staggerSec=0, targetGainFraction=.9)
                    automatic, initial_elapsed = timed(service.plan_course, request, auto_teams=True)
                    recommendation = automatic['teamRecommendation']
                    source = service.choose_team_count({'assetId': 'szu-3', 'version': automatic['version'], 'teamCount': 6,
                        'comparisonId': recommendation['comparisonId'], 'comparisonToken': recommendation['comparisonToken']})
                source_path = comparisons.plan_path('szu-3', source['id'])
                source_bytes = source_path.read_bytes()
                inputs_path = comparisons.path('szu-3', source['continuation']['id'])
                inputs_bytes = inputs_path.read_bytes()
                refined, refine_elapsed = timed(service.continue_plan, {'assetId': 'szu-3', 'version': source['version'],
                    'planId': source['id'], 'timeLimitSec': budget})
                metadata = refined['refinement']
                assert refined['request']['teams'] == source['request']['teams']
                assert not improves(comparisons.objective_value(metadata['before'], base['objective']),
                                    comparisons.objective_value(metadata['after'], base['objective']))
                assert source_path.read_bytes() == source_bytes and inputs_path.read_bytes() == inputs_bytes
                version = simulate_review(refined)
                with patch.object(service, 'solve', side_effect=AssertionError('Confirmation reoptimized a route')), \
                     patch.object(service, 'recommend_teams', side_effect=AssertionError('Confirmation recomputed team counts')):
                    confirmed, confirm_elapsed = timed(service.continue_plan,
                        {'assetId': 'szu-3', 'version': version, 'planId': refined['id']}, confirm=True)
                    assert confirmed['refinement'] == refined['refinement']
                    for first, second in zip(refined['routes'], confirmed['routes']):
                        for key in ('order', 'geometry', 'distanceM', 'finishSec'):
                            assert first[key] == second[key], key
                    if kind == 'compared_team_count':
                        rec = confirmed['teamRecommendation']
                        assert rec['selectedCount'] == 6 and rec['refinedCounts'] == [6]
                        other = service.choose_team_count({'assetId': 'szu-3', 'version': version, 'teamCount': 4,
                            'comparisonId': rec['comparisonId'], 'comparisonToken': rec['comparisonToken']})
                        assert len(other['routes']) == 4
                edges.update(tuple(edge) for plan in (source, refined, confirmed) for route in plan['routes'] for edge in route['edgeIds'])
                rows.append({'case': kind, 'teams': len(refined['routes']), 'controls': len(controls),
                    'initialElapsedSec': initial_elapsed, 'refinementBudgetSec': budget, 'refineElapsedSec': refine_elapsed,
                    'confirmElapsedSec': confirm_elapsed, 'improved': metadata['improved'],
                    'before': metadata['before'], 'after': metadata['after'], 'sourcePlanUnchanged': True,
                    'sourceInputsUnchanged': True, 'exactRoutesPreservedThroughReview': True,
                    'confirmationDidNotReoptimize': True, 'continuationBytes': comparisons.path('szu-3', refined['continuation']['id']).stat().st_size})
                print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
            graph = service.get_graph(store.get_state('szu-3'))
            assert all(graph.weights[a, b] > 0 and graph.clear_segment(graph.pixels[a], graph.pixels[b]) for a, b in edges)
        finally:
            store.DATA = live_data
            service.GRAPH_CACHE.clear()
    assert (store.asset_dir('szu-3')/'state.json').read_bytes() == before
    report = {'auditedAt': datetime.now(timezone.utc).isoformat(), 'scope': __doc__, 'passed': True,
              'cases': rows, 'legalEdgesChecked': len(edges), 'workingStateUnchanged': True,
              'sourceHash': original['sourceHash'], 'graphHash': graph.content_hash,
              'implementationSignature': service.implementation_signature(),
              'peakRSSMiB': round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024, 1)}
    store.atomic_json(Path(args.output), report)


if __name__ == '__main__':
    main()
