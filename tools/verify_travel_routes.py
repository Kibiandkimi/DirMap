#!/usr/bin/env python3
"""Compare old and learned travel costs on existing, read-only map graphs.

Endpoints are sampled graph nodes, not independently surveyed course points.
This checks routing integration/performance, not real-world passage validity.
"""
import argparse
import json
from pathlib import Path
import resource
import sys
import time

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--assets', nargs='+', default=['szu-1', 'szu-3'])
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    from dirmap import service, store, travel
    store.DATA = args.data_dir
    results = []
    for identifier in args.assets:
        state = store.get_state(identifier)
        if state is None:
            raise ValueError('缺少地图：'+identifier)
        started = time.perf_counter()
        baseline = service.get_graph(state, use_travel=False)
        base_sec = time.perf_counter()-started
        started = time.perf_counter()
        learned = service.get_graph(state)
        learned_sec = time.perf_counter()-started
        np.testing.assert_array_equal(learned.weights.indices, baseline.weights.indices)
        np.testing.assert_array_equal(learned.weights.indptr, baseline.weights.indptr)
        np.testing.assert_array_equal(learned.lengths.data, baseline.lengths.data)
        heights = learned.elevation_node_heights
        known = np.flatnonzero(np.isfinite(heights)) if heights is not None else np.arange(len(learned.local))
        # Use one connected component; an isolated courtyard is not an OD pair.
        if len(known):
            component = np.argmax(np.bincount(learned.components[known]))
            known = known[learned.components[known] == component]
        nodes = known[np.linspace(0, max(0, len(known)-1), min(8, len(known)), dtype=int)]
        if len(nodes) < 2:
            raise ValueError('没有足够的可比较节点')
        before, parents_before = baseline.shortest_paths(nodes, targets=nodes)
        after, parents_after = learned.shortest_paths(nodes, targets=nodes)
        pairs = []
        for i, a in enumerate(nodes):
            for j, b in enumerate(nodes):
                if i == j:
                    continue
                old_path = baseline.expand(parents_before[i], int(a), int(b))
                new_path = learned.expand(parents_after[i], int(a), int(b))
                measured = float(learned.weights[new_path[:-1], new_path[1:]].sum())
                assert np.isclose(measured, after[i, b])
                pairs.append({'fromSample': i, 'toSample': j, 'routeChanged': old_path != new_path,
                              'oldMovingSecAt1_4Mps': round(float(before[i, b]/1.4), 3),
                              'learnedMovingSecAt1_4Mps': round(measured/1.4, 3)})
        ratio = learned.weights.data/baseline.weights.data
        result = {'assetId': identifier, 'mapVersion': state['version'], 'nodes': len(learned.local), 'edges': learned.weights.nnz,
                  'baselineGraphSec': round(base_sec, 3), 'attachLearnedCostsSec': round(learned_sec, 3),
                  'legalEdgesAndLengthsUnchanged': True, 'changedEdgeCosts': int(np.count_nonzero(np.abs(ratio-1) > 1e-6)),
                  'costRatioRange': [round(float(ratio.min()), 4), round(float(ratio.max()), 4)],
                  'sampledPairs': len(pairs), 'changedRoutes': sum(p['routeChanged'] for p in pairs), 'pairs': pairs,
                  'modelRevision': learned.travel_snapshot['revision']}
        print(json.dumps({k: v for k, v in result.items() if k != 'pairs'}), flush=True)
        results.append(result)
        service.GRAPH_CACHE.clear()
    report = {'schema': travel.SCHEMA, 'scope': 'sampled connected ground nodes on existing real map graphs; not a field passage survey',
              'peakRssMiB': round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024, 1), 'maps': results}
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')


if __name__ == '__main__':
    main()
