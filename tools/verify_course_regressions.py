#!/usr/bin/env python3
"""Check the reported course-line defect and actual 31/32/33 mixed courses.

Uses the current prepared map, all 16 actual controls, independently marked
image regions, and every expanded route edge. No teacher state is changed.
"""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import itertools
from pathlib import Path
import sys
import time

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dirmap import store
from dirmap.geometry import Registration
from dirmap.graph import expand_route, plan_matrix, segment_pixels
from dirmap.optimizer import solve
from dirmap.protocol import ROUTING_SCHEMA, TERRAIN_SCHEMA
from dirmap.service import get_graph
from dirmap.vision import colour_masks, read_image


def main():
    fixture = store.read_json(store.ROOT/'evidence/course-regression-fixture.json')
    asset = store.get_asset(fixture['assetId'])
    state = store.get_state(asset['id'])
    assert state['sourceHash'] == asset['sha256'] == fixture['sourceHash']
    assert state['terrainSchema'] == TERRAIN_SCHEMA
    defaults = store.course_defaults(asset['sha256'])
    assert defaults['mode'] == 'mixed_coverage' and defaults['mandatoryLabels'] == fixture['mandatoryLabels']
    assert defaults['mandatoryOrder'] == fixture['defaultMandatoryOrder']
    graph, reg = get_graph(state), Registration.from_dict(state['registration'])
    controls = [c for c in state['controls'] if c.get('role', 'control') == 'control' and c['state'] != 'rejected']
    by_label = {c['label']: c['id'] for c in controls}
    assert Counter(c['label'] for c in controls) == Counter(fixture['mandatoryLabels']+fixture['sharedLabels'])
    required = [by_label[label] for label in fixture['mandatoryLabels']]
    shared = [by_label[label] for label in fixture['sharedLabels']]
    endpoints = [c for c in state['controls'] if c.get('role') == 'start_finish']
    assert len(endpoints) == 1
    start = endpoints[0].get('approachPixel') or endpoints[0]['pixel']
    bundle = plan_matrix(graph, controls, start, start, reg)
    assert not bundle['unresolved'], bundle['unresolved']
    image = read_image(asset['path'])
    purple = colour_masks(image)['purple']
    independent_blocked = np.zeros(image.shape[:2], bool)
    regions = []
    for region in fixture['blockedRegions']:
        x0, y0, x1, y1 = region['box']
        independent_blocked[y0:y1, x0:x1] = True
        ink_count = int(np.count_nonzero(purple[y0:y1, x0:x1]))
        free_count = int(np.count_nonzero(graph.costs[y0:y1, x0:x1]))
        assert ink_count > 0, (region['id'], 'fixture does not include the reported purple ink')
        assert free_count == 0, (region['id'], free_count)
        regions.append({**region, 'pixelsChecked': (x1-x0)*(y1-y0), 'purpleInkPixels': ink_count, 'freePixels': free_count})
    lookup = {c['id']: c for c in controls}
    straight = []
    for a, b in itertools.combinations(required, 2):
        traversable = graph.clear_segment(lookup[a]['pixel'], lookup[b]['pixel'])
        assert not traversable
        straight.append({'from': lookup[a]['label'], 'to': lookup[b]['label'], 'directlyTraversable': traversable})
    scenarios, checked_edges = [], set()
    for ordered in (False, True):
        for count in (1, 2, 5, 10):
            started = time.perf_counter()
            teams = [{'id': f'team-{i+1}', 'people': 3+i % 2, 'speedMps': 1.2+i % 3*.15, 'departureSec': i*10}
                     for i in range(count)]
            result = solve(bundle['matrix'], controls, teams, 'mixed_coverage', time_limit=3,
                           mandatory_ids=required, mandatory_order=ordered)
            assert result['status'] in ('OPTIMAL', 'FEASIBLE')
            expected = Counter({c['id']: count if c['id'] in required else 1 for c in controls})
            assert Counter(c for route in result['routes'] for c in route['controlIds']) == expected
            routes = []
            for route in result['routes']:
                ids = route['controlIds']
                assert len(ids) == len(set(ids)) and set(required).issubset(ids)
                if ordered:
                    assert [c for c in ids if c in required] == required
                geometry = expand_route(graph, bundle, route['order'])
                for a, b in geometry['edgeIds']:
                    edge = tuple(sorted((a, b)))
                    if edge in checked_edges:
                        continue
                    assert graph.weights[a, b] > 0 and graph.clear_segment(graph.pixels[a], graph.pixels[b]), edge
                    pixels = segment_pixels(graph.pixels[a], graph.pixels[b])
                    assert not independent_blocked[pixels[:, 1], pixels[:, 0]].any(), edge
                    checked_edges.add(edge)
                routes.append({'teamId': route['teamId'], 'people': route['people'],
                               'labels': [lookup[c]['label'] for c in ids], 'distanceM': round(geometry['distanceM'], 2),
                               'edgeCount': len(geometry['edgeIds'])})
            record = {'teams': count, 'people': sum(t['people'] for t in teams), 'mandatoryOrder': ordered,
                      'status': result['status'], 'mandatoryVisitsPerPoint': count, 'sharedVisitsPerPoint': 1,
                      'routes': routes, 'elapsedSec': round(time.perf_counter()-started, 3), 'proof': result['proof']}
            scenarios.append(record)
            print({key: record[key] for key in ('teams', 'mandatoryOrder', 'status', 'elapsedSec')}, flush=True)
    # Confirm even the endpoint-to-graph connectors respect the independent regions.
    for endpoint, node in zip(bundle['endpoints'], bundle['nodes']):
        point = endpoint.get('approachPixel') or endpoint['pixel']
        assert graph.clear_segment(point, graph.pixels[node])
        pixels = segment_pixels(point, graph.pixels[node])
        assert not independent_blocked[pixels[:, 1], pixels[:, 0]].any()
    report = {'auditedAt': datetime.now(timezone.utc).isoformat(), 'assetId': asset['id'],
              'sourceHash': state['sourceHash'], 'stateVersion': state['version'], 'graphHash': graph.content_hash,
              'routingSchema': ROUTING_SCHEMA, 'terrainSchema': TERRAIN_SCHEMA, 'fixtureScope': fixture['scope'],
              'independentFieldValidation': False, 'regions': regions, 'straightConnections': straight,
              'controlCount': len(controls), 'mandatoryLabels': fixture['mandatoryLabels'],
              'sharedLabels': fixture['sharedLabels'], 'scenarios': scenarios,
              'uniqueLegalEdgesChecked': len(checked_edges), 'allChecksPassed': True}
    sources = sorted((store.ROOT/'dirmap').glob('*.py'))+[store.ROOT/'dirmap/schemas.json']
    report['implementationHash'] = hashlib.sha256(b''.join(p.read_bytes() for p in sources)).hexdigest()
    store.atomic_json(store.ROOT/'evidence/runtime/course-regressions.json', report)


if __name__ == '__main__':
    main()
