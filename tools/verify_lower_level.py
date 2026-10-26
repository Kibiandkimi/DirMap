#!/usr/bin/env python3
"""Compare terrain and control paths with and without lower-level hatching.

Both variants are recomputed from the same working image by the current
terrain pipeline; the baseline only disables the hatching recognizer. This
checks the prepared maps and graph model, not a field survey of the areas.
"""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys
import time
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dirmap import layers, store, vision
from dirmap.access import AccessResolver
from dirmap.geometry import Registration
from dirmap.graph import plan_matrix
from dirmap.ocr import label_controls
from dirmap.protocol import ROUTING_SCHEMA, TERRAIN_SCHEMA
from dirmap.service import implementation_signature, read_image

SEMANTICS = ('building', 'water', 'point_ink', 'vegetation', 'restricted', 'lower_level')


def no_hatching(image, controls):
    empty = np.zeros(image.shape[:2], np.uint8)
    return empty, empty.copy(), []


def graph_for(result, state, registration):
    semantics = result['semantics']
    graph = layers.build(result['costs'], registration, state['sourceHash'], state['edits'], state.get('surfaces', []),
                         state.get('portals', []), semantics['restricted'], step=4, lower_level=semantics['lower_level'])
    graph.access_resolver = AccessResolver(graph, registration, {key: semantics[key] for key in SEMANTICS})
    return graph


def endpoints(state):
    usable = [c for c in state['controls'] if c.get('calibrated') and c.get('state') != 'rejected']
    controls = [c for c in usable if (c.get('role') or 'control') == 'control']
    starts = [c for c in usable if c.get('role') in ('start', 'start_finish')] or controls[:1]
    start = starts[0].get('approachPixel') or starts[0]['pixel']
    return controls, start


def distances(graph, registration, controls, start):
    bundle = plan_matrix(graph, controls, start, start, registration, metric='distance')
    labels = ['start', *[c['label'] for c in controls], 'finish']
    unresolved = sorted({item['id'] for item in bundle['unresolved']})
    return bundle, labels, unresolved


def audit(asset_id):
    state = store.get_state(asset_id)
    asset = store.get_asset(asset_id)
    image = read_image(asset['path'], (state['width'], state['height']))
    started = time.perf_counter()
    # Number OCR also removes the purple labels, so it affects terrain.
    current = vision.analyze(image, labeler=label_controls)
    with mock.patch.object(vision, 'lower_level_hatching', no_hatching):
        baseline = vision.analyze(image, labeler=label_controls)
    with np.load(store.artifact_path(state, 'terrain.npz')) as saved:
        reproduced = bool(np.array_equal(saved['costs'], current['costs']))
    before, after = baseline['costs'], current['costs']
    lower = current['semantics']['lower_level']
    opened, closed = (before == 0) & (after > 0), (before > 0) & (after == 0)
    changed = ~np.isclose(before, after) & ~opened & ~closed
    report = {'assetId': asset_id, 'name': state['name'], 'savedTerrainReproduced': reproduced,
              'stripePixels': current['statistics']['lowerLevelHatchingPixels'],
              'lowerLevelAreaPixels': int(np.count_nonzero(lower)),
              'lowerLevelAreas': sum(f['kind'] == 'lower_level' for f in current['terrainFeatures']),
              'openedPixels': int(np.count_nonzero(opened)),
              'openedOutsideLowerLevelArea': int(np.count_nonzero(opened & ~lower)),
              'closedPixels': int(np.count_nonzero(closed)), 'otherCostChanges': int(np.count_nonzero(changed))}
    if not report['openedPixels'] or not state.get('registration'):
        report['pathComparison'] = None
        report['elapsedSec'] = round(time.perf_counter()-started, 1)
        return report
    registration = Registration.from_dict(state['registration'])
    controls, start = endpoints(state)
    old, labels, old_unresolved = distances(graph_for(baseline, state, registration), registration, controls, start)
    new, _, new_unresolved = distances(graph_for(current, state, registration), registration, controls, start)
    comparison = {'endpoints': len(labels), 'unresolvedWithout': old_unresolved, 'unresolvedWith': new_unresolved}
    if not old['unresolved'] and not new['unresolved']:
        a, b = old['matrix'], new['matrix']
        mask = ~np.eye(len(a), dtype=bool) & np.isfinite(a) & np.isfinite(b)
        mask[:, -1] = mask[-1, :] = False  # finish duplicates the start
        delta = a-b
        shorter = mask & (delta > .01)
        longer = mask & (delta < -.01)
        comparison.update(directedPairs=int(mask.sum()), shorterPairs=int(shorter.sum()), longerPairs=int(longer.sum()),
                          medianShorterM=round(float(np.median(delta[shorter])), 2) if shorter.any() else 0.,
                          maxShorterM=round(float(delta[mask].max()), 2) if mask.any() else 0.)
        if shorter.any():
            i, j = np.unravel_index(np.argmax(np.where(mask, delta, -np.inf)), delta.shape)
            comparison['largestChange'] = {'from': labels[i], 'to': labels[j], 'withoutM': round(float(a[i, j]), 2),
                                           'withM': round(float(b[i, j]), 2)}
    report['pathComparison'] = comparison
    report['elapsedSec'] = round(time.perf_counter()-started, 1)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--asset', action='append', help='Map ID; repeat to select several (default: all prepared maps)')
    parser.add_argument('--output', default='evidence/runtime/lower-level-hatching.json')
    args = parser.parse_args()
    maps = []
    for asset in store.catalog():
        state = store.get_state(asset['id'])
        if (args.asset and asset['id'] not in args.asset) or not state:
            continue
        maps.append(audit(asset['id']))
        print(maps[-1], flush=True)
    report = {'generatedAt': datetime.now(timezone.utc).isoformat(timespec='seconds'),
              'terrainSchema': TERRAIN_SCHEMA, 'routingSchema': ROUTING_SCHEMA,
              'implementation': implementation_signature(),
              'scope': ('Same working image and current terrain pipeline, with the grey/white lower-level hatching '
                        'recognizer enabled or disabled. Distances are graph shortest paths between the map\'s '
                        'calibrated targets and start, using teacher edits and automatic approaches. Not a field survey.'),
              'maps': maps}
    store.atomic_json(store.ROOT/args.output, report)
    failures = [m['assetId'] for m in maps if not m['savedTerrainReproduced'] or m['closedPixels']
                or (m['pathComparison'] or {}).get('longerPairs')]
    if failures:
        raise SystemExit('lower-level hatching audit failed: '+', '.join(failures))


if __name__ == '__main__':
    main()
