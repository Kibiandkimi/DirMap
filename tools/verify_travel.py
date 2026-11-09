#!/usr/bin/env python3
"""Audit all KML inputs and hold out complete recordings for travel-time checks.

The held-out recording never supplies the terrain, road effects or slope fit.
Only its first quarter calibrates an otherwise unknown personal flat pace;
all three methods receive that same calibration prefix and score the rest.
"""
import argparse
from datetime import datetime
import json
import hashlib
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import urllib.request

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def read_tracks(paths, labels):
    from dirmap import elevation
    files = sorted({p for path in paths for p in (path.rglob('*') if path.is_dir() else [path]) if p.suffix.lower() in elevation.TRACK_SUFFIXES})
    tracks, seen, duplicates = [], set(), []
    for path in files:
        metadata, arrays = elevation.parse_track(path.read_bytes(), path.name)
        same = [t for t in tracks if t['geometryId'] == metadata['geometryId']]
        duplicate = next((t for t in same if t['timingFingerprint'] == metadata['timingFingerprint'] or
                          not t['timedPointCount'] or not metadata['timedPointCount']), None)
        if duplicate is not None:
            duplicates.append(path.name)
            if metadata['timedPointCount'] > duplicate['timedPointCount']:
                identifier = duplicate['id']
                duplicate.update({**metadata, **arrays, 'id': identifier})
            continue
        if same:
            metadata['id'] = hashlib.sha256(metadata['geometryId'].encode()+np.asarray(arrays['times'], '<f8').tobytes()).hexdigest()
        seen.add(metadata['id'])
        tracks.append({**metadata, **arrays, 'activity': labels.get(metadata['id'], 'auto')})
    for track in tracks:
        track['activity'] = labels.get(track['id'], 'auto')
    unknown = sorted(set(labels)-seen)
    if unknown:
        raise ValueError('活动标注中的轨迹不在输入中：'+', '.join(unknown))
    return tracks, {'files': len(files), 'uniqueRecordings': len(tracks), 'duplicateFiles': duplicates,
                    'points': sum(len(t['points']) for t in tracks)}


def evaluate_recording(track, others):
    from dirmap.elevation_model import reconstruct, HeightModel
    from dirmap import travel, travel_model
    from dirmap.geometry import Frame
    metadata, arrays = reconstruct(others)
    sampler, frame = HeightModel(metadata, arrays), Frame.of(metadata)
    model = travel_model.build(others, frame, sampler, metadata['bbox'])
    rows, summary = travel_model.observations(track, frame, sampler)
    record = {'id': track['id'], 'fileName': track['fileName'], 'activity': summary['inferredActivity'],
              'trainingRecordings': len(others), 'observationCount': len(rows)}
    if summary['inferredActivity'] not in travel_model.MODES or len(rows) < 12:
        return {**record, 'excluded': summary['reason'] if len(rows) >= 12 else 'too_few_moving_windows'}, None
    split = max(3, int(np.ceil(len(rows)*.25)))
    duration = np.array([r['seconds'] for r in rows])
    distance = np.array([r['distance'] for r in rows])
    grades = np.array([r['grade'] if r['grade'] is not None else np.nan for r in rows])
    predictor = travel_model.Predictor(model, summary['inferredActivity'])
    graph = SimpleNamespace(elevation_frame=frame, travel_predictors=[predictor], travel_snapshot={'mode': summary['inferredActivity']})
    learned, supported = travel.edge_factors(graph, [r['start'] for r in rows], [r['end'] for r in rows], grades, np.ones(len(rows)))
    factors = {'flat': np.ones(len(rows)), 'gradeFormula': np.exp(travel_model.grade_log_cost(grades)), 'learned': learned}
    scores, samples = {}, {}
    for name, cost in factors.items():
        # Estimate only a single multiplicative personal pace, not future road
        # residuals. Neither the held-out suffix nor its episode fit is used.
        pace = float(np.median(distance[:split]*cost[:split]/duration[:split]))
        prediction = distance[split:]*cost[split:]/pace
        error = prediction-duration[split:]
        relative = np.abs(error)/duration[split:]
        scores[name] = {'calibratedPaceMps': round(pace, 3), 'observedMovingSec': round(float(duration[split:].sum()), 1),
                        'predictedMovingSec': round(float(prediction.sum()), 1),
                        'totalErrorPercent': round(float(100*abs(error.sum())/duration[split:].sum()), 2),
                        'windowMedianErrorPercent': round(float(100*np.median(relative)), 2),
                        'windowMaeSec': round(float(np.mean(np.abs(error))), 3)}
        samples[name] = {'errors': error.tolist(), 'observed': duration[split:].tolist(),
                         'relative': relative.tolist(), 'tripError': abs(float(error.sum()))/duration[split:].sum()}
    return {**record, 'calibrationWindows': split, 'testWindows': len(rows)-split,
            'learnedCoverageFraction': round(float(np.sum(distance[split:]*supported[split:])/distance[split:].sum()), 4),
            'gradeCoverageFraction': round(float(np.isfinite(grades[split:]).mean()), 4),
            'gradeLearned': predictor.profile.get('gradeLearned', False), 'scores': scores}, samples


def summarize(samples):
    output = {}
    for name in ('flat', 'gradeFormula', 'learned'):
        errors = np.concatenate([s[name]['errors'] for s in samples])
        relative = np.concatenate([s[name]['relative'] for s in samples])
        trip_errors = [s[name]['tripError'] for s in samples]
        output[name] = {'windowMaeSec': round(float(np.mean(np.abs(errors))), 3),
                        'windowMedianErrorPercent': round(float(100*np.median(relative)), 2),
                        'windowP90ErrorPercent': round(float(100*np.percentile(relative, 90)), 2),
                        'recordingMeanErrorPercent': round(float(100*np.mean(trip_errors)), 2),
                        'recordingMedianErrorPercent': round(float(100*np.median(trip_errors)), 2)}
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('inputs', type=Path, nargs='+')
    parser.add_argument('--labels', type=Path)
    parser.add_argument('--server', help='optional read-only /api/elevation catalogue comparison')
    parser.add_argument('--holdout-after', help='also train only on earlier recordings and score later ones; ISO date/time with timezone')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    cutoff = datetime.fromisoformat(args.holdout_after) if args.holdout_after else None
    if cutoff and cutoff.tzinfo is None:
        parser.error('--holdout-after needs an explicit timezone')
    from dirmap import travel_model
    from dirmap.elevation_model import reconstruct, spatial_groups, HeightModel
    from dirmap.geometry import Frame
    labels = json.loads(args.labels.read_text())['activities'] if args.labels else {}
    tracks, inventory = read_tracks(args.inputs, labels)
    report = {'schema': travel_model.SCHEMA, 'inventory': inventory,
              'validation': 'leave-one-recording-out; terrain and travel model refitted on other recordings only; first 25% of held-out moving windows calibrate pace; score remaining 75%',
              'limitations': ['GPS-only foot/bicycle inference remains uncertain; ambiguous unlabelled records excluded.',
                              'Errors describe moving windows, not stops or a guarantee for a new person.',
                              'Few independent recordings; walking and running are pooled, with per-episode pace calibration.'], 'regions': []}
    if args.server:
        with urllib.request.urlopen(args.server.rstrip('/')+'/api/elevation', timeout=30) as response:
            server = json.load(response)
        remote = {t['id'] for t in server['tracks'] if t.get('kind') != 'contours'}
        local = {t['id'] for t in tracks}
        remote_tracks = {t['id']: t for t in server['tracks']}
        different = [t['id'] for t in tracks if t['id'] in remote_tracks and
                     remote_tracks[t['id']].get('sourceSha256') != t['sourceSha256']]
        report['serverComparison'] = {'version': server['version'], 'matches': local == remote and not different,
                                      'sourceFileHashMismatches': different,
                                      'localOnly': sorted(local-remote), 'serverOnly': sorted(remote-local)}
    for group in spatial_groups(tracks):
        started = time.perf_counter()
        metadata, arrays = reconstruct(group)
        model = travel_model.build(group, Frame.of(metadata), HeightModel(metadata, arrays))
        region = {'origin': metadata['frame']['origin'], 'recordings': len(group),
                  'profiles': {k: {key: value for key, value in p.items() if key not in ('corridors', 'paceMps')}
                               | {'corridors': len(p['corridors'])} for k, p in model['profiles'].items()},
                  'tracks': [{**s, 'fileName': next(t['fileName'] for t in group if t['id'] == s['id'])} for s in model['tracks']],
                  'heldOut': []}
        samples = {}
        if len(group) > 1:
            for index, track in enumerate(group):
                # Unknown activities cannot be a ground-truth activity label.
                diagnostic = next(t for t in model['tracks'] if t['id'] == track['id'])
                if diagnostic['inferredActivity'] not in travel_model.MODES:
                    region['heldOut'].append({'id': track['id'], 'fileName': track['fileName'], 'excluded': diagnostic['reason']})
                    continue
                row, sample = evaluate_recording(track, [t for i, t in enumerate(group) if i != index])
                region['heldOut'].append(row)
                if sample:
                    samples.setdefault(row['activity'], []).append(sample)
        region['summaryByActivity'] = {mode: summarize(items) for mode, items in samples.items()}
        if cutoff:
            training = [t for t in group if t.get('recordedAt') is not None and t['recordedAt'] < cutoff.timestamp()]
            future = [t for t in group if t.get('recordedAt') is not None and t['recordedAt'] >= cutoff.timestamp()]
            prospective, forward_samples = [], {}
            if training:
                for track in future:
                    row, sample = evaluate_recording(track, training)
                    prospective.append(row)
                    if sample:
                        forward_samples.setdefault(row['activity'], []).append(sample)
            region['chronologicalHoldout'] = {'cutoff': cutoff.isoformat(), 'trainingRecordings': len(training),
                'heldOut': prospective, 'summaryByActivity': {mode: summarize(items) for mode, items in forward_samples.items()}}
        region['elapsedSec'] = round(time.perf_counter()-started, 2)
        report['regions'].append(region)
        print(json.dumps({'recordings': len(group), 'profiles': region['profiles'], 'validation': region['summaryByActivity']}, ensure_ascii=False), flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)+'\n')


if __name__ == '__main__':
    main()
