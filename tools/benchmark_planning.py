#!/usr/bin/env python3
"""30 synthetic checkpoints on the real map graph, 10 teams, cold/warm cache.

Checkpoint sampling is explicitly a performance fixture, not a real course
or evidence of recognition accuracy. Project snapshots are never modified.
"""
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import platform
import resource
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np

from dirmap import service, store
from dirmap.geometry import Registration
from dirmap.graph import expand_route, plan_matrix
from dirmap.optimizer import solve


def main():
    state = store.get_state('szu-3')
    source_bytes = (store.asset_dir('szu-3')/'state.json').read_bytes()
    cases = []
    for cache_state in ('cold_graph', 'warm_graph'):
        if cache_state == 'cold_graph':
            service.GRAPH_CACHE.clear()
        started = time.perf_counter()
        graph = service.get_graph(state)
        graph_elapsed = time.perf_counter()-started
        reg = Registration.from_dict(state['registration'])
        start = next(c['pixel'] for c in state['controls'] if c.get('role') == 'start_finish')
        component = graph.components[graph.snap(start)]
        nodes = np.flatnonzero(graph.components == component)
        rng = np.random.default_rng(1047)
        sampled = rng.choice(nodes, size=30, replace=False)
        controls = [{'id': f'performance-{i}', 'pixel': graph.pixels[node].tolist(), 'state': 'candidate'}
                    for i, node in enumerate(sampled)]
        teams = [{'id': f'group-{i}', 'people': 4+(i % 3), 'speedMps': 1.2+(i % 2)*.2, 'departureSec': i*5}
                 for i in range(10)]
        before_matrix = time.perf_counter()
        bundle = plan_matrix(graph, controls, start, start, reg)
        assert not bundle['unresolved'], bundle['unresolved']
        matrix_elapsed = time.perf_counter()-before_matrix
        # Reserve room for route expansion inside the overall five-second
        # benchmark. The server's explicit slider is solver budget only.
        budget = max(.01, 5-(time.perf_counter()-started)-.65)
        result = solve(bundle['matrix'], controls, teams, 'split_coverage', time_limit=budget)
        before_expand = time.perf_counter()
        routes = [expand_route(graph, bundle, route['order']) for route in result['routes']]
        total = time.perf_counter()-started
        assert Counter(c for r in result['routes'] for c in r['controlIds']) == Counter(c['id'] for c in controls)
        edges = {tuple(sorted(edge)) for route in routes for edge in route['edgeIds']}
        for a, b in edges:
            assert graph.weights[a, b] > 0 and graph.clear_segment(graph.pixels[a], graph.pixels[b])
        record = {'cache': cache_state, 'graphSec': round(graph_elapsed, 3), 'matrixSec': round(matrix_elapsed, 3),
                  'solverBudgetSec': round(budget, 3), 'solverSec': round(result['proof']['elapsedSec'], 3),
                  'expansionSec': round(total-(before_expand-started), 3), 'totalPlanningSec': round(total, 3),
                  'underFiveSeconds': total <= 5, 'status': result['status'], 'proof': result['proof'],
                  'controls': 30, 'teams': 10, 'legalEdgesCheckedAfterTiming': len(edges)}
        cases.append(record)
        print({k: v for k, v in record.items() if k != 'proof'}, flush=True)
    assert (store.asset_dir('szu-3')/'state.json').read_bytes() == source_bytes
    report = {'auditedAt': datetime.now(timezone.utc).isoformat(), 'scope': __doc__,
              'platform': platform.platform(), 'python': platform.python_version(),
              'solverWorkers': 2, 'peakRSSMiB': round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024, 1),
              'graphHash': graph.content_hash, 'sourceHash': state['sourceHash'],
              'dependencyImportExcluded': True, 'nativeEdgeValidationExcludedFromTiming': True, 'cases': cases}
    report['cacheScope'] = 'cold_graph means empty in-process graph cache after map preparation; versioned graph artifacts in the bounded scratch cache may be loaded. warm_graph retains the same graph; both cases recompute the point matrix.'
    store.atomic_json(store.ROOT/'evidence/runtime/planning-performance.json', report)


if __name__ == '__main__':
    main()
