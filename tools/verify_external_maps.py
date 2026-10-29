#!/usr/bin/env python3
"""Run the real pipeline on published orienteering maps of another park.

The repository has no orienteering map of 大沙河公园 or of any park besides
the campus. NYC Parks publishes course maps of Alley Pond Park (New York,
© NYC Parks 2020) for free download and printing. They are forest-style ISOM
maps: black houses, overprinted course ink, sequence numbers 1, 2, 3. This
downloads two of them into a temporary directory (they never enter the
repository), renders them at 200 dpi, adds the park as a place from its
OpenStreetMap features and runs upload → recognition → registration →
planning. Detected circles and read numbers are compared with the printed
course; the map is drawn to magnetic north, so the fitted rotation can be
compared with the WMM-2020 declination there, which no fit uses.

This checks robustness on another cartographer's sheets. It is not an
accuracy measurement on the ground and not a test of a 大沙河公园 map.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
SITE = {'name': 'Alley Pond Park', 'bbox': [-73.755, 40.737, -73.731, 40.7515]}
MAPS = [
    {'id': 'beginner-1', 'url': 'https://www.nycgovparks.org/pagefiles/163/Alley-Pond-2020-Beginners1-horizontal-11x8-5__6050e9d89dc7b.pdf',
     'sha256': 'af3e84f03fdfec22d580cac91e97c729a4143c0ad654b04100f0fa0a74a423fc',
     # Printed course, located by eye on the 200 dpi rendering (pixels).
     'controls': {'1': [1458, 1126], '2': [1259, 1402], '3': [1003, 1230], '4': [651, 990],
                  '5': [940, 895], '6': [1186, 782], '7': [1534, 790], '8': [1590, 1018]},
     'start': [1595, 1134]},
    {'id': 'intermediate-1', 'url': 'https://www.nycgovparks.org/pagefiles/170/Alley-Pond-Park-Intermediate-1-Orienteering-Course__616869fa5d110.pdf',
     'sha256': '141ca54e7400f7230717577a4b59441f901cef1bf8be19ce1cef319cfb79f50b',
     'controls': {'1': [1369, 1386], '2': [1094, 1288], '3': [375, 1012], '4': [446, 877], '5': [1128, 722],
                  '6': [1501, 648], '7': [1426, 510], '8': [1478, 282], '9': [1592, 193], '10': [1736, 322],
                  '11': [1892, 851], '12': [1725, 989]},
     'start': [1567, 1191]},
]
MATCH_PX = 12
DECLINATION_YEAR = 2020.7  # "Update – September, 2020" on both sheets


def fetch(item, cache):
    path = cache/(item['id']+'.pdf')
    if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
        request = urllib.request.Request(item['url'], headers={'User-Agent': 'DirMap/0.1 recognition check'})
        with urllib.request.urlopen(request, timeout=180) as response:
            path.write_bytes(response.read())
    if hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
        raise SystemExit(f'{item["url"]} changed; update the printed course before comparing')
    image = cache/(item['id']+'.jpg')
    if not image.exists():
        subprocess.run(['pdftoppm', '-r', '200', '-jpeg', '-jpegopt', 'quality=92', '-singlefile',
                        str(path), str(image.with_suffix(''))], check=True)
    return image


def run(code, cache, reference=None):
    """The whole check with the dirmap package found in `code`."""
    sys.path.insert(0, str(code))
    import numpy as np
    from dirmap import sites, store
    with tempfile.TemporaryDirectory(prefix='dirmap-external-') as temporary:
        os.environ['DIRMAP_CACHE_DIR'] = str(Path(temporary)/'cache')
        os.environ.pop('DIRMAP_ADMIN_TOKEN', None)
        store.DATA = Path(temporary)/'data'
        from fastapi.testclient import TestClient
        from dirmap.app import app

        def wait(client, job):
            while job['state'] in ('queued', 'running'):
                time.sleep(.5)
                job = client.get('/api/jobs/'+job['id']).json()
            if job['state'] != 'succeeded':
                raise RuntimeError(f'{job["kind"]}: {job.get("message")}')
            return job['result']

        report = {'maps': []}
        with TestClient(app) as client:
            if reference:
                site = sites.add(SITE['name'], SITE['bbox'])
                target = store.workspace_root()/'sites'/site['id']/'reference.json'
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(reference, target)
                sites.update(site['id'], status='ready', retrievedAt=time.time())
            else:
                created = client.post('/api/sites', json=SITE).json()
                site = created['site']
                wait(client, created['job'])
            # When reusing a reference, the next (baseline) process needs
            # its durable input path, not this disposable workbench copy.
            report['referencePath'] = str(reference or store.workspace_root()/'sites'/site['id']/'reference.json')
            report['referenceFeatures'] = len(sites.reference(site['id']))
            for item in MAPS:
                image = cache/(item['id']+'.jpg')
                started = time.perf_counter()
                uploaded = client.post('/api/maps/upload', params={'siteId': site['id']},
                                       files={'file': (image.name, image.read_bytes(), 'image/jpeg')}).json()
                state = wait(client, client.post(f'/api/maps/{uploaded["id"]}/process').json())
                entry = {'id': item['id'], 'processingSec': round(time.perf_counter()-started, 1),
                         'size': [state['width'], state['height']]}
                controls = [c for c in state['controls'] if c.get('role', 'control') == 'control']
                found, read, wrong = [], [], []
                for label, point in item['controls'].items():
                    near = [c for c in controls if math.dist(c['pixel'], point) <= MATCH_PX]
                    if near:
                        found.append(label)
                        if near[0]['label'] == label:
                            read.append(label)
                        elif not near[0]['label'].startswith('待核'):
                            wrong.append({'printed': label, 'read': near[0]['label']})
                starts = [c for c in state['controls'] if c.get('role') in ('start', 'start_finish')]
                entry['detection'] = {
                    'printedControls': len(item['controls']), 'found': len(found),
                    'missed': sorted(set(item['controls'])-set(found), key=int),
                    'extraCircles': sum(all(math.dist(c['pixel'], p) > MATCH_PX for p in item['controls'].values()) for c in controls),
                    'startFound': any(math.dist(c['pixel'], item['start']) <= MATCH_PX for c in starts)}
                entry['numbers'] = {'readCorrectly': len(read), 'readWrongly': wrong,
                                    'unread': len(found)-len(read)-len(wrong)}
                quality = state.get('quality') or {}
                entry['registration'] = {key: quality.get(key) for key in
                                         ('method', 'fitCount', 'validationCount', 'medianErrorM', 'p95ErrorM', 'maxErrorM')}
                if state.get('registration'):
                    affine = np.asarray(state['registration']['affine'])
                    up = -affine[:, 1]  # local direction of the sheet's "up"
                    entry['registration']['sheetUpBearingDeg'] = round(math.degrees(math.atan2(up[0], up[1])), 2)
                    entry['registration']['metresPerPixel'] = round(math.sqrt(abs(np.linalg.det(affine[:, :2]))), 3)
                entry['calibratedControls'] = sum(bool(c.get('calibrated')) for c in controls)
                usable = [c['id'] for c in controls if c.get('calibrated')]
                if usable and starts:
                    pixel = starts[0]['pixel']
                    try:
                        plan = wait(client, client.post('/api/plans', json={
                            'assetId': state['assetId'], 'version': state['version'], 'controlIds': usable,
                            'startPixel': pixel, 'finishPixel': pixel, 'mode': 'all_visit',
                            'objective': 'min_max_finish_then_sum', 'timeLimitSec': 5,
                            'teams': [{'id': '1', 'people': 2, 'speedMps': 1.6}]}).json())
                        entry['plan'] = {'status': plan['status'], 'routes': len(plan['routes']),
                                         'unresolved': len(plan.get('unresolved', []))}
                    except RuntimeError as error:
                        entry['plan'] = {'error': str(error)}
                report['maps'].append(entry)
                print(json.dumps(entry, ensure_ascii=False), flush=True)
            if reference is None:
                kept = cache/'reference.json'
                shutil.copyfile(report['referencePath'], kept)
                report['referencePath'] = str(kept)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache', type=Path, default=Path(tempfile.gettempdir())/'dirmap-external-maps')
    parser.add_argument('--baseline', help='also run this git revision on the same sheets and reference')
    parser.add_argument('--code', type=Path, default=ROOT, help=argparse.SUPPRESS)
    parser.add_argument('--reference', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--output', type=Path, default=ROOT/'evidence/runtime/external-park-maps.json')
    args = parser.parse_args()
    args.cache.mkdir(parents=True, exist_ok=True)
    for item in MAPS:
        fetch(item, args.cache)
    if args.code != ROOT:  # a baseline child: report only
        print(json.dumps(run(args.code, args.cache, args.reference), ensure_ascii=False))
        return
    report = {'checkedAt': datetime.now(timezone.utc).isoformat(), 'scope': __doc__.strip(),
              'sources': [{'id': m['id'], 'url': m['url'], 'sha256': m['sha256'], 'rendering': 'pdftoppm 200 dpi JPEG q92'} for m in MAPS],
              'license': 'Maps © NYC Parks 2020, used as local test input only; not stored in the repository.',
              'site': SITE}
    sys.path.insert(0, str(ROOT))
    from pygeomag import GeoMag
    centre = ((SITE['bbox'][1]+SITE['bbox'][3])/2, (SITE['bbox'][0]+SITE['bbox'][2])/2)
    report['magneticDeclinationDeg'] = round(GeoMag(coefficients_file='wmm/WMM_2020.COF').calculate(
        glat=centre[0], glon=centre[1], alt=0, time=DECLINATION_YEAR).d, 2)
    report['current'] = run(ROOT, args.cache, args.reference)
    if args.baseline:
        with tempfile.TemporaryDirectory(prefix='dirmap-baseline-') as directory:
            archive = subprocess.run(['git', '-C', str(ROOT), 'archive', args.baseline, 'dirmap'], check=True, capture_output=True).stdout
            subprocess.run(['tar', '-x', '-C', directory], input=archive, check=True)
            child = subprocess.run([sys.executable, '-B', __file__, '--code', directory, '--cache', str(args.cache),
                                    '--reference', report['current']['referencePath']], capture_output=True, text=True)
            if child.returncode:
                raise SystemExit(child.stderr[-2000:])
            report['baseline'] = {'revision': subprocess.run(['git', '-C', str(ROOT), 'rev-parse', args.baseline], check=True,
                                                             capture_output=True, text=True).stdout.strip(),
                                  **json.loads(child.stdout.strip().splitlines()[-1])}
    for run_report in (report['current'], report.get('baseline')):
        if run_report:
            run_report.pop('referencePath', None)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')


if __name__ == '__main__':
    main()
