#!/usr/bin/env python3
"""Leave-one-recording-out check of the shared terrain reconstruction.

Each recording in turn is withheld; the surface rebuilt from the others
predicts its heights. A recording's own constant offset is unknown, so it is
removed before comparing; its slow drift is not, so the figures include it.
Grades are compared over straight stretches of the withheld walk.
"""
import argparse
import json
from pathlib import Path
import sys
import time

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def withheld(tracks, baselines=(30, 60, 100)):
    from dirmap.elevation_model import HeightModel, reconstruct
    from dirmap.geometry import Frame
    residuals, grades, rows = [], {b: [] for b in baselines}, []
    for index, track in enumerate(tracks):
        others = [t for i, t in enumerate(tracks) if i != index]
        metadata, arrays = reconstruct(others)
        predicted = HeightModel(metadata, arrays).sample(track['points'][:, :2])
        known = np.isfinite(predicted)
        if known.sum() < 20:
            rows.append({'id': track['id'], 'fileName': track['fileName'], 'predictedFraction': round(float(known.mean()), 3)})
            continue
        difference = track['points'][known, 2]-predicted[known]
        residual = difference-np.median(difference)
        residuals.append(residual)
        xy = Frame.of(metadata).to_local(track['points'][:, :2])
        walked = np.r_[0, np.cumsum(np.linalg.norm(np.diff(xy, axis=0), axis=1))]
        for baseline in baselines:
            end = np.searchsorted(walked, walked+baseline)
            start = np.flatnonzero(end < len(walked)); end = end[start]
            keep = known[start] & known[end]
            start, end = start[keep], end[keep]
            along = walked[end]-walked[start]
            straight = np.linalg.norm(xy[end]-xy[start], axis=1) > .8*along
            start, end, along = start[straight], end[straight], along[straight]
            grades[baseline].append(np.column_stack(((track['points'][end, 2]-track['points'][start, 2])/along,
                                                     (predicted[end]-predicted[start])/along)))
        rows.append({'id': track['id'], 'fileName': track['fileName'], 'predictedFraction': round(float(known.mean()), 3),
                     'offsetM': round(float(np.median(difference)), 2),
                     'residualMadM': round(float(1.4826*np.median(np.abs(residual))), 2)})
    residual = np.concatenate(residuals)
    summary = {'residualMadM': round(float(1.4826*np.median(np.abs(residual))), 2),
               'residualP90M': round(float(np.percentile(np.abs(residual), 90)), 2), 'grades': {}}
    for baseline, parts in grades.items():
        pairs = np.concatenate(parts)
        summary['grades'][f'{baseline}m'] = {
            'pairs': len(pairs), 'correlation': round(float(np.corrcoef(pairs[:, 0], pairs[:, 1])[0, 1]), 3),
            'medianModelGrade': round(float(np.median(np.abs(pairs[:, 1]))), 4),
            'medianWalkGrade': round(float(np.median(np.abs(pairs[:, 0]))), 4),
            'differenceMad': round(float(1.4826*np.median(np.abs(pairs[:, 0]-pairs[:, 1]))), 4)}
    return summary, rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('inputs', nargs='+', type=Path, help='KML files or directories')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    from dirmap import elevation
    from dirmap.elevation_model import SCHEMA, reconstruct, spatial_groups
    paths = []
    for path in args.inputs:
        paths.extend(sorted(p for p in path.iterdir() if p.suffix.lower() == '.kml') if path.is_dir() else [path])
    tracks = []
    for path in paths:
        summary, arrays = elevation.parse_kml(path.read_bytes(), path.name)
        tracks.append({**summary, 'points': arrays['points'], 'times': arrays['times']})
    report = {'schema': SCHEMA, 'files': len(tracks), 'points': int(sum(len(t['points']) for t in tracks)), 'regions': []}
    for group in spatial_groups(tracks):
        started = time.perf_counter()
        metadata, _ = reconstruct(group)
        region = {'tracks': len(group), 'centre': metadata['frame']['origin'], 'spacingM': metadata['spacingM'],
                  'supportedHectares': round(metadata['supportedCells']*metadata['spacingM']**2/1e4, 1),
                  'heightRangeM': [metadata['minHeightM'], metadata['maxHeightM']],
                  'alignment': {k: v for k, v in metadata['alignment'].items() if k != 'tracks'},
                  'driftRangeM': sorted(t['driftRangeM'] for t in metadata['alignment']['tracks']),
                  'rebuildSec': round(time.perf_counter()-started, 2)}
        if len(group) >= 3:
            region['withheld'], region['perRecording'] = withheld(group)
        report['regions'].append(region)
        print(json.dumps(region, ensure_ascii=False), flush=True)
    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')


if __name__ == '__main__':
    main()
