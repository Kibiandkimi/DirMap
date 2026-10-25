#!/usr/bin/env python3
"""Adopt and confirm a real-map comparison in a disposable workbench.

No classroom is published and no working map is modified. The test confirms
software preservation of a reviewed choice, not a physical field inspection.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dirmap import comparisons, service, store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='evidence/runtime/team-choice.json')
    args = parser.parse_args()
    live_data = store.DATA
    original = store.get_state('szu-3')
    before = (store.asset_dir('szu-3')/'state.json').read_bytes()
    with tempfile.TemporaryDirectory(prefix='dirmap-team-choice-') as directory:
        data = Path(directory)/'data'
        project = data/'projects/szu-3'
        project.mkdir(parents=True)
        store.atomic_json(project/'state.json', original)
        terrain = project/original.get('artifacts', {}).get('terrain.npz', 'terrain.npz')
        terrain.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(store.artifact_path(original, 'terrain.npz'), terrain)
        parent = (original.get('quality') or {}).get('baseAssetId')
        seen = {'szu-3'}
        while parent and parent not in seen:
            seen.add(parent)
            state = store.get_state(parent)
            store.atomic_json(data/'projects'/parent/'state.json', state)
            parent = (state.get('quality') or {}).get('baseAssetId')
        controls = [c for c in original['controls'] if c.get('role', 'control') == 'control' and c['state'] != 'rejected']
        start = next(c for c in original['controls'] if c.get('role') == 'start_finish')
        endpoint = start.get('approachPixel') or start['pixel']
        request = {'assetId': 'szu-3', 'version': original['version'], 'controlIds': [c['id'] for c in controls],
                   'startPixel': endpoint, 'finishPixel': endpoint, 'mode': 'mixed_coverage',
                   'objective': 'min_max_finish_then_sum', 'mandatoryOrder': False,
                   'mandatoryControlIds': [next(c['id'] for c in controls if c['label'] == label) for label in ('31', '32', '33')],
                   'totalPeople': 20, 'minPeoplePerTeam': 2, 'maxTeams': 10, 'speedMps': 1.4,
                   'staggerSec': 0, 'targetGainFraction': .9, 'timeLimitSec': 5}
        store.DATA = data
        service.GRAPH_CACHE.clear()
        try:
            started = time.perf_counter()
            automatic = service.plan_course(request, auto_teams=True)
            compare_elapsed = time.perf_counter()-started
            rec = automatic['teamRecommendation']
            count = 6 if rec['recommendedCount'] != 6 else 5
            payload = {'assetId': 'szu-3', 'version': original['version'], 'comparisonId': rec['comparisonId'],
                       'comparisonToken': rec['comparisonToken'], 'teamCount': count}
            cache_size = comparisons.path('szu-3', rec['comparisonId']).stat().st_size
            with patch.object(service, 'solve', side_effect=AssertionError('Unexpected reoptimization')), \
                 patch.object(service, 'recommend_teams', side_effect=AssertionError('Unexpected recomparison')):
                started = time.perf_counter()
                selected = service.choose_team_count(payload)
                adopt_elapsed = time.perf_counter()-started
                state = store.get_state('szu-3')
                approaches = {a['controlId']: a['pixel'] for a in selected['approaches'] if a['automatic']}
                for control in state['controls']:
                    control['required'] = control['id'] in request['controlIds']
                    if control['required']:
                        control['state'] = 'accepted'
                        if control['id'] in approaches:
                            control['approachPixel'] = approaches[control['id']]
                state.update(version=state['version']+1, reviewed=True,
                             review={'registrationChecked': True, 'controlsChecked': True, 'passagesChecked': True})
                service.project_controls(state)
                store.save_state('szu-3', state)
                service.GRAPH_CACHE.clear()
                started = time.perf_counter()
                confirmed = service.choose_team_count({**payload, 'version': state['version'], 'confirmPlanId': selected['id']})
                confirm_elapsed = time.perf_counter()-started
            assert len(selected['routes']) == len(confirmed['routes']) == count
            assert confirmed['teamRecommendation']['recommendedCount'] == rec['recommendedCount']
            assert confirmed['teamRecommendation']['selectedCount'] == count
            for first, second in zip(selected['routes'], confirmed['routes']):
                for key in ('controlIds', 'geometry', 'distanceM', 'finishSec'):
                    assert first[key] == second[key], key
            graph = service.get_graph(store.get_state('szu-3'))
            edges = {tuple(edge) for route in confirmed['routes'] for edge in route['edgeIds']}
            assert all(graph.weights[a, b] > 0 and graph.clear_segment(graph.pixels[a], graph.pixels[b]) for a, b in edges)
            report = {'auditedAt': datetime.now(timezone.utc).isoformat(), 'scope': __doc__, 'passed': True,
                      'controls': len(controls), 'recommendedCount': rec['recommendedCount'], 'selectedCount': count,
                      'compareElapsedSec': compare_elapsed, 'adoptElapsedSec': adopt_elapsed,
                      'confirmElapsedSec': confirm_elapsed, 'comparisonBytes': cache_size,
                      'preservedExactRoutes': True, 'noReoptimization': True,
                      'legalEdgesChecked': len(edges), 'graphHash': graph.content_hash,
                      'sourceHash': original['sourceHash'], 'implementationSignature': service.implementation_signature()}
        finally:
            store.DATA = live_data
            service.GRAPH_CACHE.clear()
    report['workingStateUnchanged'] = (store.asset_dir('szu-3')/'state.json').read_bytes() == before
    assert report['workingStateUnchanged']
    store.atomic_json(Path(args.output), report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
