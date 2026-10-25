#!/usr/bin/env python3
"""Rebuild all three maps from the source images in a disposable empty project.

No prior fit, checkpoint edits or graph cache is copied. This verifies the
automatic pipeline, not independent geographic accuracy or field passage.
"""
from collections import Counter
from datetime import datetime, timezone
import os
import hashlib
from pathlib import Path
import resource
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dirmap import service, store
from dirmap.geometry import Registration
from dirmap.graph import plan_matrix


def main():
    inventory = store.read_json(store.ROOT/'evidence/course-inventory.json')['maps']
    previous = store.DATA
    saved_states = {row['id']: (store.asset_dir(row['id'])/'state.json').read_bytes() for row in inventory}
    cache = os.environ.get('DIRMAP_CACHE_DIR')
    write_json = store.atomic_json
    def isolated_write(path, value):
        # The live workbench may legitimately change its own maps during
        # this check. Guard this process's writes instead of treating those
        # external changes as evidence that the disposable run wrote them.
        if Path(path).resolve().is_relative_to(previous.resolve()):
            raise AssertionError('Fresh-processing audit attempted a live project write')
        return write_json(path, value)
    report = {'auditedAt': datetime.now(timezone.utc).isoformat(), 'scope': __doc__,
              'copiedProjectStates': False, 'reusedPreparedGraphCache': False, 'maps': [],
              'independentFieldValidation': False, 'fullObjectiveAchieved': False}
    with tempfile.TemporaryDirectory(prefix='dirmap-fresh-') as directory:
        store.DATA = Path(directory)/'data'
        os.environ['DIRMAP_CACHE_DIR'] = str(Path(directory)/'graph-cache')
        store.atomic_json = isolated_write
        service.GRAPH_CACHE.clear()
        try:
            for expected in inventory:
                asset_id = expected['id']
                assert store.get_state(asset_id) is None
                assert store.get_asset(asset_id)['sha256'] == expected['sourceHash']
                started = time.perf_counter()
                state = service.process_asset(asset_id, lambda percent, message:
                    print(f'{asset_id}: {percent}% {message}', flush=True))
                elapsed = time.perf_counter()-started
                controls = [c for c in state['controls'] if c.get('role', 'control') == 'control']
                starts = [c for c in state['controls'] if c.get('role') == 'start_finish']
                quality = {key: value for key, value in (state.get('quality') or {}).items() if key != 'landmarks'}
                entry = {'assetId': asset_id, 'sourceHash': state['sourceHash'], 'version': state['version'],
                         'detectedLabels': sorted(c['label'] for c in controls),
                         'labelsMatched': Counter(c['label'] for c in controls) == Counter(expected['labels']),
                         'startFinishMatched': len(starts) == expected['startFinishCount'],
                         'calibratedControls': sum(c.get('calibrated', False) for c in controls),
                         'controls': len(controls), 'registrationHash': service.registration_stamp(state),
                         'quality': quality, 'processingElapsedSec': round(elapsed, 3),
                         'preprocessingUnder120Sec': elapsed <= 120}
                if state.get('registration') and len(starts) == 1:
                    graph = service.get_graph(state)
                    start = starts[0]['pixel']
                    bundle = plan_matrix(graph, controls, start, start, Registration.from_dict(state['registration']))
                    labels = {c['id']: c['label'] for c in controls}
                    entry['fullCourseUnresolved'] = [{**item, 'label': labels.get(item['id'], item['id'])}
                                                     for item in bundle['unresolved']]
                    entry['graphHash'] = graph.content_hash
                report['maps'].append(entry)
                print({key: entry[key] for key in ('assetId', 'labelsMatched', 'calibratedControls', 'processingElapsedSec')}, flush=True)
        finally:
            store.DATA = previous
            store.atomic_json = write_json
            service.GRAPH_CACHE.clear()
            if cache is None:
                os.environ.pop('DIRMAP_CACHE_DIR', None)
            else:
                os.environ['DIRMAP_CACHE_DIR'] = cache
    report['existingProjectStatesUnchanged'] = all((store.asset_dir(asset_id)/'state.json').read_bytes() == raw
                                                  for asset_id, raw in saved_states.items())
    report['liveProjectWriteGuardPassed'] = True
    report['projectStateComparisons'] = [{'assetId': asset_id,
        'before': hashlib.sha256(raw).hexdigest(),
        'after': store.file_hash(store.asset_dir(asset_id)/'state.json')} for asset_id, raw in saved_states.items()]
    report['concurrentWorkbenchUpdatesDetected'] = not report['existingProjectStatesUnchanged']
    report['automaticRecognitionSmokePassed'] = all(entry['labelsMatched'] and entry['startFinishMatched']
        and entry['calibratedControls'] == entry['controls'] for entry in report['maps'])
    report['implementationSignature'] = service.implementation_signature()
    report['peakRSSMiB'] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024, 1)
    report['finishedAt'] = datetime.now(timezone.utc).isoformat()
    store.atomic_json(store.ROOT/'evidence/runtime/fresh-processing.json', report)
    assert report['liveProjectWriteGuardPassed'] and report['automaticRecognitionSmokePassed'], report


if __name__ == '__main__':
    main()
