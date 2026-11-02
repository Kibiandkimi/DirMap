#!/usr/bin/env python3
"""Use an existing local Dashahe KML and the public server terrain to check contour assistance.

Only temporary models and the requested report are written. No observations
are contributed to the running service. The two visually checked adjacent
contours around control 35 are split into four fragments by printed ink.
Their relative difference is 2 m; their absolute height comes from the track.
This checks the constraints and workflow, not independent survey accuracy.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
import urllib.request

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

LEVELS = {
    'contour-d585bc4744c7': 0., 'contour-acae13108aca': 0.,
    'contour-84fcbb8990ce': 2., 'contour-742814aa65b3': 2.,
}


def sampled(model, coordinates):
    values = model.sample(coordinates)
    known = values[np.isfinite(values)]
    return {'coverage': round(len(known)/max(1, len(values)), 3),
            'medianM': round(float(np.median(known)), 3) if len(known) else None,
            'spreadM': round(float(np.ptp(np.percentile(known, [10, 90]))), 3) if len(known) >= 3 else None}


def preview(before, after, metadata, coordinates, output):
    from dirmap.elevation_render import PALETTE
    from dirmap.geometry import Frame
    from dirmap.photos import _fonts
    latin, cjk = _fonts(19)
    valid = np.r_[before[np.isfinite(before)], after[np.isfinite(after)]]
    low, high = float(valid.min()), float(valid.max())
    scale = min(8, max(1, 650//max(before.shape)))
    w, h = before.shape[1]*scale, before.shape[0]*scale
    canvas = Image.new('RGB', (w*2+70, h+112), '#fafaf5'); draw = ImageDraw.Draw(canvas)
    def text(position, value, colour):
        x, y = position
        for char in value:
            font = latin if ord(char) < 256 else cjk
            draw.text((x, y+19), char, fill=colour, font=font, anchor='ls')
            x += font.getlength(char)
    for index, (grid, title) in enumerate([(before, '本地轨迹'), (after, '本地轨迹 + 相对等高线')]):
        data = np.flipud(grid); known = np.isfinite(data)
        location = np.clip((np.nan_to_num(data, nan=low)-low)/max(1., high-low), 0, .99999)*(len(PALETTE)-1)
        segment = np.floor(location).astype(int); fraction = (location-segment)[..., None]
        rgb = PALETTE[segment]*(1-fraction)+PALETTE[segment+1]*fraction
        rgb[~known] = [248, 248, 240]
        panel = Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8)).resize((w, h), Image.Resampling.NEAREST)
        left = 20+index*(w+30); canvas.paste(panel, (left, 42)); text((left, 12), title, '#244735')
        for points, level in coordinates:
            local = (Frame.of(metadata).to_local(points)-metadata['origin'])/metadata['spacingM']
            pixels = [(left+float(x)*scale, 42+(before.shape[0]-1-float(y))*scale) for x, y in local]
            draw.line(pixels, fill='#d43535' if level == 0 else '#3c42dd', width=2)
    text((20, h+57), f'同一色阶：{low:.1f}-{high:.1f} m；红/蓝为图上相差 2 m 的等高线。', '#344936')
    text((20, h+82), '仅验证形状约束，不代表独立测量精度。', '#596451')
    canvas.save(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, default=ROOT/'data/projects/upload-55496a067ba7/state.json')
    parser.add_argument('--track', type=Path, default=Path('/home/kibi/下载/20261029_183448.kml'))
    parser.add_argument('--reference-url', help='existing service base URL, used read-only for its public terrain')
    parser.add_argument('--output', type=Path, default=ROOT/'evidence/runtime/contour-terrain.json')
    parser.add_argument('--workdir', type=Path)
    args = parser.parse_args()
    from dirmap import contours, elevation, service, store
    from dirmap.elevation_model import reconstruct, HeightModel
    state = json.loads(args.state.read_text())
    if state['sourceHash'] != '21800411acc7cc83220de66d1c31b7b33b9ac20d1bcff0da699ea9c45dc8d4b8':
        raise ValueError('This probe uses the supplied real Dashahe sheet')
    available = {curve['id']: curve for curve in state['contours']}
    assert set(LEVELS) <= set(available), 'The contour extraction changed; review the visual fixture first'
    directory = args.workdir or Path(tempfile.mkdtemp(prefix='dirmap-contour-terrain-'))
    directory.mkdir(parents=True, exist_ok=True)
    report = {'checkedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
              'scope': __doc__, 'mapSourceSha256': state['sourceHash'], 'implementationSignature': service.implementation_signature()}
    coordinates = []
    for identifier, level in LEVELS.items():
        points, valid = contours.geographic_curve(available[identifier], state)
        assert valid.all()
        coordinates.append((points, level))
    if args.reference_url:
        base = args.reference_url.rstrip('/')
        def fetch(path):
            with urllib.request.urlopen(base+path, timeout=30) as response:
                return json.load(response)
        status = fetch('/api/elevation')
        region = next(r for r in status['regions'] if r['name'] == '大沙河公园')
        grid = fetch('/api/elevation/regions/'+region['id'])
        assert grid['fingerprint'] == region['fingerprint'], 'Shared terrain changed while taking the reference snapshot'
        reference = HeightModel(grid, {'height': np.array(grid['height'], float).reshape(grid['rows'], grid['columns'])})
        rows = []
        for curve in state['contours']:
            points, valid = contours.geographic_curve(curve, state)
            if not valid.all():
                continue
            item = sampled(reference, points)
            rows.append({'id': curve['id'], **item})
        eligible = [row for row in rows if row['coverage'] >= .7]
        report['serverReference'] = {'url': base, 'sourceVersion': status['version'], 'allTracks': len(status['tracks']),
            'dashaheTracks': len(region['trackIds']), 'dashahePoints': region['pointCount'],
            'modelFingerprint': grid['fingerprint'], 'coveredContours': len(eligible),
            'spreadAtMost2M': sum(row['spreadM'] is not None and row['spreadM'] <= 2 for row in eligible)}
        (directory/'server-reference.json').write_text(json.dumps(grid, ensure_ascii=False))
    record, arrays = elevation.parse_kml(args.track.read_bytes(), args.track.name)
    track = {**record, **arrays}
    report['localTrack'] = {'fileName': args.track.name, 'sha256': hashlib.sha256(args.track.read_bytes()).hexdigest(),
                            'points': record['pointCount']}
    before_meta, before_arrays = reconstruct([track])
    # Exercise the same normalization/storage path as the application, in a
    # disposable data directory. Nothing is written to the user's workbench.
    store.DATA = directory/'data'
    selected = [{'id': key, 'heightM': value} for key, value in LEVELS.items()]
    receipt = contours.contribute(state, selected, {'id': 'verification', 'username': 'Verification'}, 'relative')
    identifier = (receipt['added'] or [d['id'] for d in receipt['duplicates']])[0]
    contour_record = next(r for r in elevation.catalogue()['tracks'] if r['id'] == identifier)
    with np.load(elevation.root()/'tracks'/(contour_record['id']+'.npz')) as data:
        constraint = {**contour_record, 'points': data['points'], 'times': data['times']}
    after_meta, after_arrays = reconstruct([track, constraint])
    aligned = next(t for t in after_meta['alignment']['tracks'] if t['id'] == contour_record['id'])
    assert aligned['anchored'] and aligned['driftRangeM'] == 0
    before_model = HeightModel(before_meta, before_arrays); after_model = HeightModel(after_meta, after_arrays)
    comparisons = [{'id': identifier, 'relativeHeightM': level, 'before': sampled(before_model, points),
                    'after': sampled(after_model, points)} for (identifier, level), (points, _) in zip(LEVELS.items(), coordinates)]
    means = {level: np.mean([row['after']['medianM'] for row in comparisons if row['relativeHeightM'] == level]) for level in (0., 2.)}
    assert abs(means[2.]-means[0.]-2) < .5, comparisons
    shifted = {**constraint, 'points': constraint['points'].copy()}; shifted['points'][:, 2] += 1000
    _, shifted_arrays = reconstruct([track, shifted])
    np.testing.assert_allclose(after_arrays['height'], shifted_arrays['height'], atol=2e-5, equal_nan=True)
    for name, meta, values in [('track-only', before_meta, before_arrays), ('with-contours', after_meta, after_arrays)]:
        (directory/(name+'.json')).write_text(json.dumps(meta, ensure_ascii=False, indent=2)+'\n')
        np.savez_compressed(directory/(name+'.npz'), **values)
    assert before_meta['origin'] == after_meta['origin'] and before_arrays['height'].shape == after_arrays['height'].shape
    preview(before_arrays['height'], after_arrays['height'], before_meta, coordinates, directory/'comparison.png')
    report.update(contours=comparisons, inferredOffsetM=aligned['correctionM'],
                  arbitraryZeroShiftDidNotChangeTerrain=True, supportedCellsBefore=before_meta['supportedCells'],
                  supportedCellsAfter=after_meta['supportedCells'], preview=str(directory/'comparison.png'),
                  allChecksPassed=True, independentSurveyValidation=False)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
