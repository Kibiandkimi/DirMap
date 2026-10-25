#!/usr/bin/env python3
"""Compare current recognition with the visual development label inventory."""
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dirmap import store


def main():
    inventory = store.read_json(store.ROOT/'evidence/course-inventory.json')
    report = {'auditedAt': datetime.now(timezone.utc).isoformat(), 'scope': inventory['scope'], 'maps': []}
    for expected in inventory['maps']:
        state = store.get_state(expected['id'])
        assert state and state['sourceHash'] == expected['sourceHash'] == store.get_asset(expected['id'])['sha256']
        controls = [c for c in state['controls'] if c.get('role', 'control') == 'control' and c['state'] != 'rejected']
        actual, wanted = Counter(c['label'] for c in controls), Counter(expected['labels'])
        start_finish = sum(c.get('role') == 'start_finish' for c in state['controls'])
        record = {'assetId': expected['id'], 'version': state['version'], 'expectedControls': len(expected['labels']),
                  'detectedControls': len(controls), 'labelsMatched': actual == wanted,
                  'missingLabels': list((wanted-actual).elements()), 'unexpectedLabels': list((actual-wanted).elements()),
                  'startFinishMatched': start_finish == expected['startFinishCount'],
                  'calibratedControls': sum(c.get('calibrated', False) for c in controls)}
        assert record['labelsMatched'] and record['startFinishMatched'], record
        report['maps'].append(record)
        print(record)
    report['allInventoriesMatched'] = True
    report['circleCentrePrecisionRecall'] = None
    report['independentFieldValidation'] = False
    store.atomic_json(store.ROOT/'evidence/runtime/recognition-inventory.json', report)


if __name__ == '__main__':
    main()
