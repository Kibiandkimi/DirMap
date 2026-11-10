#!/usr/bin/env python3
"""Process a real, native-resolution new-area map in an empty workbench.

The real site/upload/process HTTP handlers and persisted job worker run in
a disposable directory. Reference acquisition replays the downloaded OSM
snapshot, not invented features; no existing map, anchors or terrain cache
is copied. The organiser's world file frames the teacher viewport and is
used for measurement only after processing. This is an application-path
regression, not an independent field survey or a live network availability
test. Failed workbenches are retained for inspection.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'tools')]


def source_signature():
    paths = sorted((ROOT/'dirmap').glob('*.py'))+sorted((ROOT/'dirmap').glob('*.cpp'))+[ROOT/'dirmap/schemas.json']
    return hashlib.sha256(b''.join(path.read_bytes() for path in paths)).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--event', required=True, type=int)
    parser.add_argument('--host', default='https://www.happyherts.routegadget.co.uk')
    parser.add_argument('--cache', type=Path, default=Path('/tmp/dirmap-new-areas'))
    parser.add_argument('--workdir', type=Path)
    parser.add_argument('--output', type=Path, default=ROOT/'evidence/runtime/new-place-processing.json')
    args = parser.parse_args()
    parent = Path.home()/'.cache/dirmap-verification'
    parent.mkdir(parents=True, exist_ok=True)
    directory = args.workdir or Path(tempfile.mkdtemp(prefix='first-import-', dir=parent))
    directory.mkdir(parents=True, exist_ok=True)
    data_dir = directory/'data'
    if data_dir.exists():
        parser.error('--workdir must not already contain a data directory')
    original_data = Path(os.environ.get('DIRMAP_DATA_DIR', str(ROOT/'data')))
    original_states = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (original_data/'projects').glob('*/state.json')}
    os.environ['DIRMAP_DATA_DIR'] = str(data_dir)
    os.environ['DIRMAP_CACHE_DIR'] = str(directory/'cache')
    os.environ['DIRMAP_MIN_FREE_MIB'] = '512'
    os.environ.pop('DIRMAP_ADMIN_TOKEN', None)

    import cv2
    from fastapi.testclient import TestClient
    import verify_new_areas as areas
    from dirmap import service, sites, store
    from dirmap.app import app
    from dirmap.geometry import Frame, Registration

    host = args.host.rstrip('/')
    if host != areas.HOST:
        from urllib.parse import urlparse
        parsed = urlparse(host)
        if parsed.scheme not in ('https', 'http') or not parsed.netloc:
            parser.error('--host must be an HTTP(S) server URL')
        args.cache = args.cache/parsed.netloc
    areas.HOST = host
    args.cache.mkdir(parents=True, exist_ok=True)
    event = areas.events(args.cache)[args.event]
    case = areas.load_case(event, args.cache, native_resolution=True)
    image = case.pop('image')
    source_size = list(image.shape[1::-1])
    encoded = cv2.imencode('.png', image)[1].tobytes()
    reference = json.loads(case['reference'].read_text())
    implementation = service.implementation_signature()
    report = {'checkedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'scope': __doc__.strip(),
              'implementationSignature': implementation, 'verifierSha256': store.file_hash(Path(__file__)),
              'event': args.event, 'name': event['name'], 'source': host, 'worldFile': case['world'].tolist(),
              'sourceSha256': store.file_hash(args.cache/f'{event["mapid"]}.gif'),
              'uploadedPngSha256': hashlib.sha256(encoded).hexdigest(), 'nativeImageSize': source_size,
              'referenceSha256': store.file_hash(case['reference']), 'workdir': str(directory),
              'blankWorkbench': True, 'liveReferenceDownload': False, 'complete': False, 'jobs': []}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    partial = args.output.with_name(args.output.stem+'.partial.json')

    def save():
        store.atomic_json(partial, report)

    def replay(bbox, progress=lambda percent, message: None):
        # The application rounds the saved viewport to seven decimal places.
        assert max(abs(a-b) for a, b in zip(bbox, case['bbox'])) < 1e-6
        progress(20, '读取已下载的真实 OSM 参照快照')
        return reference, reference.get('source', {}).get('url', host)

    sites.download_reference = replay

    def wait(client, job):
        assert 'id' in job, job
        started = time.monotonic()
        last = None
        while True:
            response = client.get('/api/jobs/'+job['id'])
            assert response.status_code == 200, response.text
            current = response.json()
            status = current['state'], current.get('progress'), current.get('message')
            if status != last:
                print(json.dumps({'job': job['id'], 'kind': current['kind'], 'status': status}, ensure_ascii=False), flush=True)
                last = status
            if current['state'] in ('succeeded', 'failed'):
                report['jobs'].append({'id': current['id'], 'kind': current['kind'], 'state': current['state'],
                                       'elapsedSec': round(time.monotonic()-started, 2)})
                save()
                assert current['state'] == 'succeeded', current
                return current['result']
            if time.monotonic()-started > 1800:
                raise TimeoutError(f'Observation deadline reached; job {job["id"]} last reported {status}')
            time.sleep(.2)

    save()
    with TestClient(app) as client:
        response = client.post('/api/sites', json={'name': '陌生场地完整导入核查', 'bbox': case['bbox']})
        assert response.status_code == 200, response.text
        added = response.json()
        site = wait(client, added['job'])
        assert site['status'] == 'ready' and site['featureCount'] > 0
        response = client.post('/api/maps/upload', params={'siteId': site['id']},
                               files={'file': ('new-place.png', encoded, 'image/png')})
        assert response.status_code == 200, response.text
        asset = response.json()
        assert asset['sha256'] == report['uploadedPngSha256']
        # The real worker reads the persisted upload. Do not keep an extra
        # full-resolution raster alongside its working-image allocations.
        del response, image, encoded
        inputs = service.job_inputs('process', asset['id'])
        assert inputs['bases'] == {} and inputs['site'][0] == site['id']
        assert store.get_state(asset['id']) is None
        state = wait(client, client.post(f'/api/maps/{asset["id"]}/process').json())
        assert state['sourceHash'] == report['uploadedPngSha256'] and not state['reviewed']
        assert state['siteId'] == site['id'] and max(state['width'], state['height']) <= 4096
        quality = state.get('quality') or {}
        assert not quality.get('baseAssetId')
        report.update(siteId=site['id'], assetId=asset['id'], processingElapsedSec=state.get('processingElapsedSec'),
                      method=quality.get('method'), reportedP95M=quality.get('p95ErrorM'),
                      poseSupport=quality.get('poseSupport'), failures=state.get('failures'),
                      workingImageSize=[state['width'], state['height']], sourceImage=state.get('sourceImage'),
                      controls=len(state['controls']), calibratedControls=sum(bool(c.get('calibrated')) for c in state['controls']))
        if state.get('registration'):
            from dirmap.vision import read_image
            working = read_image(store.get_asset(asset['id'])['path'], (state['width'], state['height']))
            transform = case['world'].copy()
            transform[:, 0] *= source_size[0]/state['width']
            transform[:, 1] *= source_size[1]/state['height']
            error = areas.truth_error(Registration.from_dict(state['registration']),
                                      {**case, 'world': transform, 'factor': 1., 'frame': Frame.of(state)}, working)
            report['truthErrorM'] = error
            report['outcome'] = 'correct' if error['all']['median'] <= areas.CORRECT_MEDIAN_M and error['all']['p95'] <= areas.CORRECT_P95_M else 'wrong'
            assert client.get(f'/assets/maps/{asset["id"]}/overlay.png?v={state["version"]}').status_code == 200
            assert client.get(f'/assets/maps/{asset["id"]}/terrain-warp.png?v={state["version"]}').status_code == 200
            report['overlayAndTerrainAvailable'] = True
        else:
            report['outcome'] = 'refused'
            report['offeredPlaces'] = len(state.get('registrationCandidates', []))
    report['implementationUnchanged'] = implementation == source_signature()
    report['originalWorkbenchUnchanged'] = all(hashlib.sha256(p.read_bytes()).hexdigest() == digest for p, digest in original_states.items())
    report['complete'] = True
    store.atomic_json(args.output, report)
    partial.unlink(missing_ok=True)
    good = report['outcome'] == 'correct' and report['implementationUnchanged'] and report['originalWorkbenchUnchanged']
    print(json.dumps({k: report.get(k) for k in ('outcome', 'truthErrorM', 'method', 'originalWorkbenchUnchanged')}, ensure_ascii=False))
    if good and args.workdir is None:
        shutil.rmtree(directory)
    return 0 if good else 1


if __name__ == '__main__':
    raise SystemExit(main())
