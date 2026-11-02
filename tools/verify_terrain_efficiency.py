#!/usr/bin/env python3
"""Verify real map colours, automatic contours and recorded travel data in a copy."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def verify(map_data, asset_id, observations, output_dir):
    from dirmap import contours, elevation, service, store, travel
    from dirmap.geometry import Registration
    store.DATA = map_data
    state = store.get_state(asset_id)
    assert state and state['terrainSchema'] == service.TERRAIN_SCHEMA
    source = Path(store.get_asset(asset_id)['path'])
    assert hashlib.sha256(source.read_bytes()).hexdigest() == state['sourceHash']
    image = cv2.imread(str(source)); image = cv2.resize(image, (state['width'], state['height']))
    with np.load(store.artifact_path(state, 'terrain.npz')) as data:
        costs = data['costs'].copy()
    # Independently read, near-solid printed colours on the supplied sheet.
    # Limit the sample to the map inset, excluding the surrounding photographs.
    inset = np.zeros(costs.shape, np.uint8)
    polygon = np.array([[345, 150], [943, 282], [932, 800], [697, 881], [321, 806]], float)
    cv2.fillPoly(inset, [np.rint(polygon*state['width']/1400).astype(np.int32)], 1)
    rgb = image[..., ::-1].astype(float)
    olive = (np.linalg.norm(rgb-[164, 180, 25], axis=2) < 16) & (inset > 0)
    grass = (np.linalg.norm(rgb-[134, 237, 113], axis=2) < 16) & (inset > 0)
    assert olive.sum() > 10000 and grass.sum() > 10000
    blocked = float(np.mean(costs[olive] == 0)); open_grass = float(np.mean(costs[grass] > 0))
    assert blocked > .98 and open_grass > .98, (blocked, open_grass)
    report = {'checkedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
              'sourceSha256': state['sourceHash'], 'terrainSchema': state['terrainSchema'],
              'scope': '真实大沙河原图和既有 KML 规范化观测；全部计算在新的隔离副本，未修改原贡献或生产工作台。',
              'colourChecks': {'olivePixels': int(olive.sum()), 'oliveBlockedFraction': blocked,
                               'brightGreenPixels': int(grass.sum()), 'brightGreenPassableFraction': open_grass}}
    target = output_dir/'data'
    target.mkdir(parents=True, exist_ok=True)
    if (target/'elevation').exists():
        raise ValueError('请使用新的验证目录，避免覆盖前次证据')
    shutil.copytree(observations, target/'elevation', ignore=lambda _p, names: [n for n in names if n in {'models', 'models.json', 'travel-models'}])
    store.DATA = target
    source_catalogue = elevation.catalogue()
    assert len(source_catalogue['tracks']) == 16
    assert sum(t.get('activity') == 'bicycle' for t in source_catalogue['tracks']) == 5
    print('Rebuilding 16 recorded KML contributions', flush=True)
    before = elevation.rebuild(source_catalogue['version'], lambda p, m: print(p, m, flush=True))
    old_regions = elevation.models()['regions']
    baseline = elevation.Sampler(old_regions)
    reg = Registration.from_dict(state['registration'])
    probes = []
    for curve in state['contours']:
        points, valid = contours.geographic_curve(curve, state, reg)
        if valid.all():
            values = baseline.sample(points)
            finite = values[np.isfinite(values)]
            if len(finite) >= max(3, len(values)*.7):
                probes.append((curve['id'], points, float(np.percentile(finite, 90)-np.percentile(finite, 10))))
    receipt = contours.sync_automatic(state, {'id': 'real-map-verification', 'username': '大沙河原图验证'})
    assert state['automaticContours']['used'] >= 100
    print('Automatically merging all calibrated contours', flush=True)
    after = elevation.rebuild(receipt['version'], lambda p, m: print(p, m, flush=True))
    regions = elevation.models()['regions']
    sampler = elevation.Sampler(regions)
    comparisons = []
    for identifier, points, spread in probes:
        values = sampler.sample(points); finite = values[np.isfinite(values)]
        comparisons.append({'id': identifier, 'beforeSpreadM': spread,
            'afterSpreadM': float(np.percentile(finite, 90)-np.percentile(finite, 10)) if len(finite) else None,
            'afterCoverage': len(finite)/len(values)})
    paired = [c for c in comparisons if c['afterSpreadM'] is not None]
    assert len(paired) >= 10
    old_spread = float(np.median([c['beforeSpreadM'] for c in paired]))
    new_spread = float(np.median([c['afterSpreadM'] for c in paired]))
    assert new_spread < old_spread
    auto_id = state['automaticContours']['trackId']
    auto_region = next(r for r in regions if auto_id in r['trackIds'])
    alignment = next(a for a in auto_region['alignment']['tracks'] if a['id'] == auto_id)
    report['automaticContours'] = {**state['automaticContours'], 'alignment': alignment,
        'comparisonCurves': len(paired), 'medianBeforeSpreadM': old_spread, 'medianAfterSpreadM': new_spread,
        'lines': comparisons, 'interpretation': '沿线一致性检查，不是独立海拔精度验证；未输入任何人工标高。'}
    diagnostics = []
    for region in regions:
        model = travel.diagnostics(region['id'])
        assert set(model['profiles']) == {'foot', 'bicycle'}
        for identifier in region['trackIds']:
            detail = elevation.track_detail(identifier)
            one = elevation.model_grid(region['id'], identifier)
            scoped = travel.diagnostics(region['id'], identifier)
            assert one['trackIds'] == [identifier]
            assert all(t['id'] == identifier for t in scoped['tracks'])
            assert detail['track']['id'] == identifier
        diagnostics.append({'regionId': region['id'], 'name': region['name'],
            'profiles': {mode: {k: p[k] for k in ('tracks', 'observations', 'gradeLearned', 'corridorCount', 'gradeRange', 'residualLogMad')}
                         for mode, p in model['profiles'].items()}})
    assert elevation.catalogue()['version'] == after['version']
    report.update(recordings=16, rawPoints=sum(t['pointCount'] for t in source_catalogue['tracks']),
                  explicitBicycleRecords=5, learning=diagnostics, everyContributionViewChecked=True,
                  noManualContourHeights=not state.get('contourHeights'),
                  implementationSignature=service.implementation_signature(), allChecksPassed=True)
    (output_dir/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({k: v for k, v in report.items() if k != 'automaticContours'}, ensure_ascii=False, indent=2), flush=True)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--map-data', type=Path, required=True)
    parser.add_argument('--asset', required=True)
    parser.add_argument('--observations', type=Path, required=True, help='Existing elevation directory containing tracks.json and normalized KML arrays')
    parser.add_argument('--workdir', type=Path, required=True)
    args = parser.parse_args()
    verify(args.map_data, args.asset, args.observations, args.workdir)
