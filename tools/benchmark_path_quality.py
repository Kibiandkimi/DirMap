#!/usr/bin/env python3
"""Compare prepared-map path graphs on identical terrain and endpoints.

The graph comparison preserves teacher data. Model distances and pixel-edge
legality are checked; this does not certify field passages or registration.
"""
import argparse
from datetime import datetime, timezone
import importlib
import json
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import numpy as np
from dirmap import graph as current_graph, optimizer, service, store
from dirmap.access import AccessResolver
from dirmap.geometry import Registration, affine_points
from dirmap.schedule import apply_schedule
from tools.benchmark_route_quality import baseline_modules


def course_metrics(graph, bundle, result, controls, teams, mandatory):
    edges = set()
    for route, team in zip(result['routes'], teams):
        route.update(current_graph.expand_route(graph, bundle, route['order']))
        route['travelSec'] = route['travelWeightM']/team['speedMps']
        edges.update(tuple(edge) for edge in route['edgeIds'])
    apply_schedule(result['routes'], controls, teams, mandatory)
    for a, b in edges:
        assert graph.weights[a, b] > 0 and graph.clear_segment(graph.pixels[a], graph.pixels[b])
    return {'latestFinishSec': max(r['finishSec'] for r in result['routes']),
            'totalFinishSec': sum(r['finishSec'] for r in result['routes']),
            'totalDistanceM': sum(r['distanceM'] for r in result['routes']), 'legalEdgesChecked': len(edges)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', default='0c3dac6')
    parser.add_argument('--output', default='evidence/runtime/path-quality.json')
    args = parser.parse_args()
    state = store.get_state('szu-3')
    original = (store.asset_dir('szu-3')/'state.json').read_bytes()
    report = {'auditedAt': datetime.now(timezone.utc).isoformat(), 'scope': __doc__, 'baseline': args.baseline,
              'sourceHash': state['sourceHash'], 'routingSchema': current_graph.ROUTING_SCHEMA}
    with tempfile.TemporaryDirectory(prefix='dirmap-path-baseline-') as directory:
        old_optimizer, _ = baseline_modules(args.baseline, Path(directory))
        old_graph = importlib.import_module(old_optimizer.__package__+'.graph')
        corners = [[0, 0], [96, 0], [96, 96], [0, 96]]
        affine = [[1, 0, 0], [0, -1, 96]]
        registration = Registration(affine, corners, affine_points(affine, corners))
        fields = []
        targets = [[1.5+4*x, 1.5+4*y] for x in range(1, 23) for y in range(23)]
        for name, module in (('before', old_graph), ('after', current_graph)):
            graph = module.build_graph(np.ones((97, 97), np.float32), registration, 'uniform-field')
            start = graph.snap([1.5, 1.5]); nodes = [graph.snap(pixel) for pixel in targets]
            distances, _ = graph.shortest_paths([start], targets=nodes)
            direct = np.linalg.norm(graph.local[nodes]-graph.local[start], axis=1)
            errors = distances[0, nodes]/direct-1
            at = graph.snap([81.5, 41.5])
            fields.append({'implementation': name, 'headingsAndLengths': len(targets),
                           'medianDetourPercent': float(np.median(errors)*100), 'maxDetourPercent': float(errors.max()*100),
                           'twoToOneDirectionM': float(distances[0, at]), 'nodes': len(graph.pixels), 'edges': graph.weights.nnz})
        report['openField'] = fields
        with np.load(store.artifact_path(state, 'terrain.npz')) as data:
            costs = data['costs']
            semantics = {kind: data[kind] for kind in ('building', 'water', 'point_ink', 'vegetation', 'restricted') if kind in data}
        registration = Registration.from_dict(state['registration'])
        controls = [c for c in state['controls'] if c.get('role', 'control') == 'control' and c['state'] != 'rejected']
        start = next(c for c in state['controls'] if c.get('role') == 'start_finish')
        endpoint = start.get('approachPixel') or start['pixel']
        graphs, bundles = {}, {}
        report['graphs'] = {}
        for name, module in (('before', old_graph), ('after', current_graph)):
            started = time.perf_counter()
            graph = module.build_graph(costs, registration, state['sourceHash'], state['edits'])
            graph.access_resolver = AccessResolver(graph, registration, semantics)
            build_elapsed = time.perf_counter()-started
            started = time.perf_counter()
            bundle = module.plan_matrix(graph, controls, endpoint, endpoint, registration)
            matrix_elapsed = time.perf_counter()-started
            assert not bundle['unresolved'], bundle['unresolved']
            graphs[name], bundles[name] = graph, bundle
            report['graphs'][name] = {'nodes': len(graph.pixels), 'directedEdges': graph.weights.nnz,
                                      'components': int(graph.component_count), 'buildSec': build_elapsed,
                                      'pointMatrixSec': matrix_elapsed, 'graphHash': graph.content_hash}
            print({name: report['graphs'][name]}, flush=True)
        before, after = graphs['before'], graphs['after']
        np.testing.assert_array_equal(before.pixels, after.pixels)
        np.testing.assert_array_equal(before.owner, after.owner)
        np.testing.assert_array_equal(before.components, after.components)
        row, col = before.weights.nonzero()
        np.testing.assert_allclose(np.asarray(after.weights[row, col]).ravel(), before.weights.data, rtol=0, atol=1e-10)
        np.testing.assert_allclose(np.asarray(after.lengths[row, col]).ravel(), before.lengths.data, rtol=0, atol=1e-10)
        assert bundles['before']['nodes'] == bundles['after']['nodes']
        a, b = bundles['before']['matrix'], bundles['after']['matrix']
        assert np.all(b <= a+1e-7)
        active = a > 1e-7
        improvement = (a[active]-b[active])/a[active]*100
        report['pointPaths'] = {'directedPairs': int(active.sum()), 'allNoWorse': True,
                               'medianReductionPercent': float(np.median(improvement)),
                               'maxReductionPercent': float(improvement.max()),
                               'originalEdgesUnchanged': True, 'componentsUnchanged': True}
        teams = [{'id': str(i), 'people': 5, 'speedMps': 1.4, 'departureSec': 0} for i in range(4)]
        mandatory = [next(c['id'] for c in controls if c['label'] == label) for label in ('31', '32', '33')]
        original_plan = optimizer.solve(a, controls, teams, 'mixed_coverage', time_limit=5, mandatory_ids=mandatory)
        orders = [r['order'].copy() for r in original_plan['routes']]
        same_orders = optimizer.evaluate_routes(b, controls, teams, orders, 'mixed_coverage', 'min_max_finish_then_sum', mandatory)
        report['sameVisitOrders'] = {
            'before': course_metrics(before, bundles['before'], original_plan, controls, teams, mandatory),
            'after': course_metrics(after, bundles['after'], same_orders, controls, teams, mandatory)}
        assert report['sameVisitOrders']['after']['latestFinishSec'] <= report['sameVisitOrders']['before']['latestFinishSec']+1e-6
        assert report['sameVisitOrders']['after']['totalFinishSec'] <= report['sameVisitOrders']['before']['totalFinishSec']+1e-6
        improved = optimizer.solve(b, controls, teams, 'mixed_coverage', time_limit=5, mandatory_ids=mandatory, initial_sequences=orders)
        report['newGraphRefined'] = course_metrics(after, bundles['after'], improved, controls, teams, mandatory)
        report['refinementScope'] = 'An additional five-second solve on the new graph starts from the same previously chosen visit orders.'
    assert (store.asset_dir('szu-3')/'state.json').read_bytes() == original
    report.update(passed=True, teachingStateUnchanged=True, implementationSignature=service.implementation_signature())
    store.atomic_json(Path(args.output), report)
    print(json.dumps({key: report[key] for key in ('pointPaths', 'sameVisitOrders', 'newGraphRefined')}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
