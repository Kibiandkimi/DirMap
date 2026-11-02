#!/usr/bin/env python3
"""Check the supplied real Dashahe sheet in an isolated local workbench.

Printed numbers are independently read from the supplied sheet. Road-holdout
errors check automatic correspondences, not GPS ground truth or field safety.
"""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Visually annotated on a 1400-pixel-wide preview of the supplied sheet.
# These locate printed circles; they are not independent geographic fixes.
CIRCLE_CENTRES = {
    '31': [687, 891], '32': [730, 814], '33': [568, 505], '34': [580, 365],
    '35': [727, 377], '36': [899, 517], '37': [461, 257], '38': [594, 686],
    '39': [360, 135], '40': [939, 300], '41': [426, 815], '42': [598, 785],
    '43': [414, 721], '44': [515, 753], '45': [506, 611], '46': [646, 825],
    '47': [480, 838], '48': [721, 728], '49': [817, 730], '50': [629, 332],
    '51': [812, 626], '52': [781, 573], '53': [506, 517], '54': [523, 397],
    '55': [835, 385], '56': [683, 512], '57': [661, 625], '58': [872, 671],
    '59': [509, 142], '60': [845, 517], '61': [917, 726], '62': [918, 797],
}


def photo_centre(label):
    number = int(label)
    if number <= 40:
        return [86+142*((number-31) % 2), 231+112*((number-31)//2)]
    if number <= 58:
        return [1046+140*((number-41) % 3), 227+106*((number-41)//3)]
    return [906+140*(number-59), 874]


def finish_job(client, response):
    response.raise_for_status()
    job, previous = response.json(), None
    while job['state'] in ('queued', 'running'):
        message = job.get('message')
        if message != previous:
            print(message, flush=True); previous = message
        time.sleep(.5)
        response = client.get('/api/jobs/'+job['id']); response.raise_for_status(); job = response.json()
    if job['state'] != 'succeeded':
        raise RuntimeError(job.get('message', str(job)))
    return job['result']


def verify_course(client, state, bundle, graph):
    """Exercise teacher confirmation and student release only in the test workspace."""
    from dirmap.app import Control
    approaches = {a['controlId']: a['pixel'] for a in bundle['approaches'] if a.get('automatic')}
    controls = []
    for control in state['controls']:
        item = {key: value for key, value in control.items() if key in Control.model_fields}
        item['state'] = 'accepted'
        if control['id'] in approaches:
            item['approachPixel'] = approaches[control['id']]
        controls.append(item)
    response = client.patch(f'/api/maps/{state["assetId"]}', json={'version': state['version'], 'controls': controls})
    response.raise_for_status(); state = response.json()
    lookup = {c['label']: c['id'] for c in controls if c.get('role', 'control') == 'control'}
    required = [lookup[str(i)] for i in range(31, 41)]
    expected = Counter({lookup[str(i)]: 3 if i <= 40 else 1 for i in range(31, 63)})
    response = client.post(f'/api/maps/{state["assetId"]}/review', json={
        'version': state['version'], 'registrationChecked': True, 'controlsChecked': True, 'passagesChecked': True})
    response.raise_for_status()
    start = next(c['pixel'] for c in controls if c.get('role') == 'start_finish')
    plan = finish_job(client, client.post('/api/plans', json={
        'assetId': state['assetId'], 'version': state['version'], 'controlIds': list(lookup.values()),
        'startPixel': start, 'finishPixel': start, 'mode': 'mixed_coverage', 'useElevation': False,
        'mandatoryControlIds': required, 'mandatoryOrder': False,
        'teams': [{'id': f'team-{i+1}', 'people': 4, 'speedMps': 1.4} for i in range(3)],
        'timeLimitSec': 5}))
    assert plan['status'] in ('FEASIBLE', 'OPTIMAL'), plan
    assert plan['graphHash'] == graph.content_hash
    assert Counter(c for route in plan['routes'] for c in route['controlIds']) == expected
    assert len(plan['routes']) == 3 and not any(a['automatic'] for a in plan['approaches'])
    checked = set()
    for route in plan['routes']:
        assert len(route['controlIds']) == len(set(route['controlIds']))
        assert set(required) <= set(route['controlIds'])
        for a, b in route['edgeIds']:
            edge = tuple(sorted((a, b)))
            if edge not in checked:
                assert graph.weights[a, b] > 0 and graph.clear_segment(graph.pixels[a], graph.pixels[b]), edge
                checked.add(edge)
    assert checked
    response = client.post('/api/releases', json={
        'assetId': state['assetId'], 'planId': plan['id'], 'title': '大沙河实图 · 自动检查', 'collectHeights': False})
    response.raise_for_status(); released = response.json()['id']
    response = client.get('/api/releases/'+released); response.raise_for_status()
    course = response.json()
    assert len(course['sheetPhotos']) == 32 and not course['superseded']
    for photo in course['sheetPhotos']:
        response = client.get(f'/api/releases/{released}/sheet-photos/{photo["id"]}.jpg')
        assert response.status_code == 200 and response.content.startswith(b'\xff\xd8')
    return {
        'scope': '仅在隔离测试工作台模拟教师确认自动到访建议和核验；不代表现场通行已确认。',
        'mode': 'mixed_coverage', 'teams': 3, 'mandatoryLabels': [str(i) for i in range(31, 41)],
        'sharedLabels': [str(i) for i in range(41, 63)], 'coverageVerified': True,
        'planStatus': plan['status'], 'uniqueLegalEdgesChecked': len(checked),
        'referencePhotosPublished': len(course['sheetPhotos']), 'releaseId': released,
        'automaticApproachesConfirmed': len(approaches),
    }


def verify(source, directory):
    from fastapi.testclient import TestClient
    from dirmap import store, service, sites
    from dirmap.app import app
    from dirmap.geometry import Registration, Frame
    from dirmap.graph import plan_matrix
    store.DATA = directory/'data'
    os.environ['DIRMAP_CACHE_DIR'] = str(directory/'graph-cache')
    os.environ.pop('DIRMAP_ADMIN_TOKEN', None)
    report = {'checkedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
              'sourceSha256': hashlib.sha256(source.read_bytes()).hexdigest(),
              'scope': '真实大沙河原图；上传、识别、自动道路配准、照片裁切、目标连通性与隔离工作台中的混合课程发布。道路留出误差不是现场 GPS 误差。',
              'independentFieldValidation': False}
    with TestClient(app) as client:
        uploaded = client.post('/api/maps/upload', params={'siteId': 'dashahe-park'},
                               files={'file': (source.name, source.read_bytes(), 'image/jpeg')})
        uploaded.raise_for_status()
        asset = uploaded.json()
        finish_job(client, client.post(f'/api/maps/{asset["id"]}/process'))
        state = client.get(f'/api/maps/{asset["id"]}').json()
        report.update(assetId=asset['id'], workingSize=[state['width'], state['height']],
                      processingSec=state['processingElapsedSec'], statistics=state['statistics'])
        controls = [c for c in state['controls'] if c.get('role', 'control') == 'control']
        expected = Counter(str(i) for i in range(31, 63))
        report['controls'] = {'count': len(controls), 'labels': sorted(c['label'] for c in controls),
                              'calibrated': sum(c.get('calibrated', False) for c in controls)}
        report['photos'] = {'count': len(state.get('sheetPhotos', [])),
                            'labels': sorted(str(p['label']) for p in state.get('sheetPhotos', [])),
                            'linked': sum(bool(p.get('controlId')) for p in state.get('sheetPhotos', []))}
        report['registration'] = {k: v for k, v in (state.get('quality') or {}).items() if k != 'landmarks'}
        assert Counter(c['label'] for c in controls) == expected, report['controls']
        assert Counter(p['label'] for p in state['sheetPhotos']) == expected, report['photos']
        assert all(p.get('controlId') for p in state['sheetPhotos'])
        assert all(c.get('calibrated') for c in state['controls'])
        bounds = sites.get('dashahe-park')['bbox']
        assert state['frame']['origin'] == sites.get('dashahe-park')['origin']
        assert all(bounds[0] <= c['lnglat'][0] <= bounds[2] and bounds[1] <= c['lnglat'][1] <= bounds[3] for c in state['controls'])
        report['frameOrigin'] = state['frame']['origin']
        pixel_errors = [float(np.linalg.norm(np.array(c['pixel'])*1400/state['width']-CIRCLE_CENTRES[c['label']])) for c in controls]
        assert max(pixel_errors) < 5
        report['controls']['maxErrorAt1400pxPreview'] = round(max(pixel_errors), 2)
        for photo in state['sheetPhotos']:
            box = np.array(photo['pixelBox'])*1400/state['width']
            assert np.linalg.norm((box[:2]+box[2:])/2-photo_centre(photo['label'])) < 16, photo
        report['photos']['printedLabelLocationsMatched'] = True
        for photo in state['sheetPhotos']:
            response = client.get(f'/assets/maps/{asset["id"]}/sheet-photos/{photo["id"]}.jpg', params={'v': state['version']})
            assert response.status_code == 200 and response.content.startswith(b'\xff\xd8')
        report['photos']['allCropsServed'] = True
        reg = Registration.from_dict(state['registration'])
        graph = service.get_graph(state, use_elevation=False)
        endpoints = [c for c in state['controls'] if c.get('role') == 'start_finish']
        assert len(endpoints) == 1
        start = endpoints[0]['pixel']
        bundle = plan_matrix(graph, controls, start, start, reg, frame=Frame.of(state))
        report['navigation'] = {'unresolved': bundle['unresolved'], 'approaches': bundle.get('approaches', []),
                                'allPairsConnected': bool(np.isfinite(bundle['matrix']).all()) if not bundle['unresolved'] else False}
        by_id = {c['id']: c['label'] for c in controls}
        unresolved_labels = [by_id.get(issue['id'], issue['id']) for issue in bundle['unresolved']]
        assert not unresolved_labels, bundle['unresolved']
        assert np.isfinite(bundle['matrix']).all()
        report['navigation']['unresolvedLabels'] = unresolved_labels
        report['navigation']['automaticApproachLabels'] = sorted(by_id[a['controlId']] for a in bundle['approaches'] if a['automatic'])
        for endpoint, node in zip(bundle['endpoints'], bundle['nodes']):
            assert graph.clear_segment(endpoint.get('approachPixel') or endpoint['pixel'], graph.pixels[node])
        covered = next(c for c in controls if c['label'] == '52')
        arrival = next(a for a in bundle['approaches'] if a['controlId'] == covered['id'])
        assert arrival['method'] == 'exact' and arrival['pixel'] == covered['pixel']
        centre = np.array(covered['pixel']); delta = np.array([0, covered['radiusPx']*.5])
        assert graph.clear_segment(centre-delta, centre+delta)
        report['navigation']['coveredPassage52'] = {'keepsCircleCentre': True, 'northAndSouthConnected': True}
        print('Navigation: all 32 targets connected;', len(report['navigation']['automaticApproachLabels']),
              'automatic approach suggestions; target 52 accessible through both ends.', flush=True)
        report['overlayScope'] = state.get('overlayScope')
        assert report['overlayScope'] == 'full-sheet'
        (directory/'state.json').write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
        report['courseWorkflow'] = verify_course(client, state, bundle, graph)
        report.update(implementationSignature=service.implementation_signature(), allChecksPassed=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', type=Path, default=Path('/home/kibi/下载/微信图片_20261031002551_91_548.jpg'))
    parser.add_argument('--output', type=Path, default=ROOT/'evidence/runtime/dashahe-real.json')
    parser.add_argument('--workdir', type=Path)
    args = parser.parse_args()
    if args.workdir:
        args.workdir.mkdir(parents=True, exist_ok=True)
        report = verify(args.image, args.workdir)
    else:
        with tempfile.TemporaryDirectory(prefix='dirmap-dashahe-real-') as directory:
            report = verify(args.image, Path(directory))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({k: v for k, v in report.items() if k != 'navigation'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
