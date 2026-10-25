#!/usr/bin/env python3
"""Re-recognize both reported originals and audit complete, legal courses.

Uses the existing geographic transforms only; no saved controls, terrain,
approaches or teacher edits. Scratch images stay in /tmp, not teaching data.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dirmap import store
from dirmap.access import AccessResolver
from dirmap.geometry import Registration
from dirmap.graph import build_graph, expand_route, plan_matrix
from dirmap.images import read_map_image
from dirmap.ocr import label_controls
from dirmap.optimizer import solve
from dirmap.schedule import apply_schedule
from dirmap.vision import analyze, save_analysis


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='evidence/runtime/resolution-regressions.json')
    parser.add_argument('--scratch', default='/tmp/dirmap-resolution-current')
    args = parser.parse_args()
    cases, checked = [], {}
    for asset_id in ('upload-fbdc0f2cf044', 'upload-f4fd82f6605e'):
        started = time.perf_counter()
        state, asset = store.get_state(asset_id), store.get_asset(asset_id)
        assert state['sourceHash'] == asset['sha256'] == store.file_hash(asset['path'])
        print(asset_id, 'fresh recognition', flush=True)
        image, frame = read_map_image(asset['path'], (state['width'], state['height']))
        result = analyze(image, labeler=label_controls)
        directory = Path(args.scratch)/asset_id
        directory.mkdir(parents=True, exist_ok=True)
        save_analysis(result, directory)
        store.atomic_json(directory/'controls.json', result['controls'])
        registration = Registration.from_dict(state['registration'])
        graph = build_graph(result['costs'], registration, asset['sha256'], [], step=4)
        graph.access_resolver = AccessResolver(graph, registration, result['semantics'])
        controls = [c for c in result['controls'] if c.get('role') == 'control']
        starts = [c for c in result['controls'] if c.get('role') == 'start_finish']
        expected = [str(label) for label in range(31, 48)]
        labels = {c['id']: c['label'] for c in controls}
        assert Counter(labels.values()) == Counter(expected) and len(starts) == 1
        start = starts[0]['pixel']
        bundle = plan_matrix(graph, controls, start, start, registration)
        scenarios, edges = [], set()
        if not bundle['unresolved']:
            mandatory = [next(c['id'] for c in controls if c['label'] == label) for label in ('31', '32', '33')]
            teams = [{'id': 'early', 'people': 3, 'speedMps': 1.5, 'departureSec': 0},
                     {'id': 'later', 'people': 5, 'speedMps': 1.2, 'departureSec': 60}]
            for mode in ('all_visit', 'mixed_coverage', 'custom'):
                custom = [{'teamId': 'early', 'controlIds': mandatory+[c['id'] for c in controls if c['id'] not in mandatory]},
                          {'teamId': 'later', 'controlIds': mandatory}]
                plan = solve(bundle['matrix'], controls, teams, mode, time_limit=3,
                             mandatory_ids=mandatory if mode != 'all_visit' else [], mandatory_order=True if mode != 'all_visit' else False,
                             custom_routes=custom if mode == 'custom' else None)
                for route in plan['routes']:
                    geometry = expand_route(graph, bundle, route['order'])
                    for a, b in geometry['edgeIds']:
                        if (a, b) not in edges:
                            assert graph.weights[a, b] > 0 and graph.clear_segment(graph.pixels[a], graph.pixels[b])
                            edges.add((a, b))
                    route.update(geometry)
                meetings = apply_schedule(plan['routes'], controls, teams, mandatory if mode != 'all_visit' else [])
                if mode == 'mixed_coverage':
                    assert Counter(c for r in plan['routes'] for c in r['controlIds']) == Counter({c['id']: 2 if c['id'] in mandatory else 1 for c in controls})
                for meeting in meetings:
                    stops = [next(s for s in r['stops'] if s['controlId'] == meeting['controlId']) for r in plan['routes']]
                    assert len({s['departureSec'] for s in stops}) == 1
                    assert meeting['departureSec'] == max(s['readySec'] for s in stops)
                scenarios.append({'mode': mode, 'status': plan['status'], 'rendezvous': meetings,
                                  'routes': [{k: r[k] for k in ('teamId', 'controlIds', 'distanceM', 'waitSec', 'finishSec')} for r in plan['routes']]})
        checked[asset_id] = {c['label']: (np.array(c['pixel'])+.5)/np.array([state['width'], state['height']]) for c in controls}
        case = {'assetId': asset_id, 'sourceHash': asset['sha256'], 'sourceFrame': frame,
                'freshRecognition': True, 'savedControlsOrTerrainUsed': False, 'geographicTransformReused': True,
                'labels': sorted(labels.values()), 'statistics': result['statistics'],
                'unresolved': [{**u, 'label': labels.get(u['id'], u['id'])} for u in bundle['unresolved']],
                'legalEdgesChecked': len(edges), 'scenarios': scenarios,
                'elapsedSec': round(time.perf_counter()-started, 3)}
        cases.append(case)
        store.atomic_json(directory/'verification.json', case)
        print(asset_id, 'unresolved', case['unresolved'], 'edges', len(edges), flush=True)
    a, b = checked.values()
    paired = {label: float(np.linalg.norm((a[label]-b[label])*4096)) for label in a}
    report = {'auditedAt': datetime.now(timezone.utc).isoformat(), 'cases': cases,
              'pairedCircleDifferenceAt4096Px': paired,
              'allChecksPassed': all(not c['unresolved'] and len(c['scenarios']) == 3 for c in cases),
              'independentFieldValidation': False,
              'scope': 'Reported image pair, complete 17-control courses on freshly recognized terrain, including custom orders and class rendezvous. Existing geographic registration is reused.'}
    sources = sorted((store.ROOT/'dirmap').glob('*.py'))+[store.ROOT/'dirmap/schemas.json']
    report['implementationHash'] = hashlib.sha256(b''.join(p.read_bytes() for p in sources)).hexdigest()
    store.atomic_json(Path(args.output), report)
    if not report['allChecksPassed']:
        raise SystemExit('Full original/compressed course regression still fails; report saved.')


if __name__ == '__main__':
    main()
