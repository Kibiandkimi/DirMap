#!/usr/bin/env python3
"""Peak and retained memory of a terrain refresh and a cold planning job.

Each measurement runs in a fresh process on a disposable copy of one prepared
map. The baseline revision is extracted with git archive. Current code applies
the allocator policy that the server enables at startup. This measures this
machine's resident memory, not a production server's swap behaviour.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]

PROBE = r'''
import json, os, sys, threading, time
sys.path.insert(0, sys.argv[1])
from dirmap import store, service
try:
    from dirmap import memory
    memory.configure()
except ImportError:
    pass
def rss():
    for line in open('/proc/self/status'):
        if line.startswith('VmRSS:'):
            return int(line.split()[1])/1024
peak, stop = [0.], threading.Event()
def sample():
    while not stop.is_set():
        peak[0] = max(peak[0], rss()); time.sleep(.005)
asset_id, task = sys.argv[2], sys.argv[3]
state = store.get_state(asset_id)
request = None
if task == 'plan':
    controls = [c for c in state['controls'] if (c.get('role') or 'control') == 'control' and c['state'] != 'rejected']
    start = next(c for c in state['controls'] if c.get('role') in ('start', 'start_finish'))
    pixel = start.get('approachPixel') or start['pixel']
    request = {'assetId': asset_id, 'version': state['version'], 'controlIds': [c['id'] for c in controls],
               'startPixel': pixel, 'finishPixel': pixel, 'mode': 'split_coverage', 'objective': 'min_max_finish_then_sum',
               'timeLimitSec': 5, 'mandatoryControlIds': [], 'mandatoryOrder': False,
               'teams': [{'id': f'team-{i+1}', 'people': 4, 'speedMps': 1.4, 'departureSec': 0} for i in range(4)]}
baseline = rss()
threading.Thread(target=sample, daemon=True).start()
started = time.perf_counter()
if task == 'refresh':
    service.refresh_terrain(asset_id)
else:
    service.plan_course(request)
elapsed = time.perf_counter()-started
stop.set(); time.sleep(.02)
print(json.dumps({'baselineMiB': round(baseline), 'peakMiB': round(peak[0]), 'retainedMiB': round(rss()),
                  'elapsedSec': round(elapsed, 1)}))
'''


def disposable_copy(asset_id, root):
    data = root/'data'
    (data/'projects').mkdir(parents=True)
    for name in ('uploads.json', 'course-presets.json'):
        if (store_data()/name).exists():
            shutil.copy2(store_data()/name, data/name)
    shutil.copytree(store_data()/'reference', data/'reference')
    shutil.copytree(store_data()/'projects'/asset_id, data/'projects'/asset_id,
                    ignore=lambda path, names: [n for n in names if n.startswith(('plan-', 'comparison-'))])
    return data


def store_data():
    return Path(os.environ.get('DIRMAP_DATA_DIR', str(ROOT/'data')))


def measure(code, asset_id, task, data, cache):
    environment = {**os.environ, 'DIRMAP_DATA_DIR': str(data), 'DIRMAP_CACHE_DIR': str(cache)}
    process = subprocess.run([sys.executable, '-B', '-c', PROBE, str(code), asset_id, task], env=environment,
                             capture_output=True, text=True)
    if process.returncode:
        raise RuntimeError(f'{code} {task} failed:\n{process.stderr[-2000:]}')
    return json.loads(process.stdout.strip().splitlines()[-1])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--asset', default='upload-f4fd82f6605e')
    parser.add_argument('--baseline', required=True, help='Git revision to compare against')
    parser.add_argument('--output', default=str(ROOT/'evidence/runtime/memory-footprint.json'))
    args = parser.parse_args()
    report = {'generatedAt': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'asset': args.asset,
              'baseline': subprocess.check_output(['git', 'rev-parse', args.baseline], cwd=ROOT, text=True).strip(),
              'current': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
              'currentWorkingTreeChanged': bool(subprocess.check_output(['git', 'status', '--porcelain', 'dirmap'], cwd=ROOT, text=True).strip()),
              'machine': {'python': platform.python_version(), 'cpu': platform.machine(), 'cpus': os.cpu_count()},
              'scope': ('Fresh process per task on a disposable copy of one prepared map. Refresh includes terrain analysis, '
                        'publication and graph warming; planning loads the graph from the disk cache and plans all targets '
                        'for 4 teams. Resident memory on this machine, sampled every 5 ms.'),
              'runs': {}}
    with tempfile.TemporaryDirectory(prefix='dirmap-memory-') as temporary:
        temporary = Path(temporary)
        baseline = temporary/'baseline'
        baseline.mkdir()
        archive = subprocess.run(['git', 'archive', args.baseline, 'dirmap', 'requirements.lock'], cwd=ROOT,
                                 check=True, capture_output=True).stdout
        subprocess.run(['tar', '-x', '-C', str(baseline)], input=archive, check=True)
        for name, code in (('baseline', baseline), ('current', ROOT)):
            data = disposable_copy(args.asset, temporary/name/'work')
            cache = temporary/name/'cache'
            report['runs'][name] = {task: measure(code, args.asset, task, data, cache) for task in ('refresh', 'plan')}
            print(name, report['runs'][name], flush=True)
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=1)+'\n', encoding='utf-8')


if __name__ == '__main__':
    main()
