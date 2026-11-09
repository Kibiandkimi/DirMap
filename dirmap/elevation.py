"""Community height observations: shared reads, contributor-owned tracks."""
from collections import OrderedDict
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET

import numpy as np

from . import store, travel
from .elevation_model import HeightModel, SCHEMA, fingerprint, reconstruct, spatial_groups, supported_nodes
from .geometry import Frame, affine_points

MAX_FILE_BYTES = 20*1024**2
MAX_BATCH_BYTES = 64*1024**2
MAX_TRACK_POINTS = 250_000
MAX_TOTAL_POINTS = 2_000_000
MAX_TRACKS = 2000
_models = OrderedDict()
_model_lock = threading.RLock()


class Conflict(ValueError):
    pass


class NotOwner(ValueError):
    pass


def root():
    # Deliberately shared across workbenches. Ownership is checked on every edit.
    return store.DATA/'elevation'


def catalogue():
    return store.read_json(root()/'tracks.json', {'version': 0, 'tracks': []})


def models():
    return store.read_json(root()/'models.json', {'sourceVersion': 0, 'schema': SCHEMA, 'regions': [],
                                                'travelSchema': travel.SCHEMA, 'travelModels': []})


def public_status():
    with store.LOCK:
        source, built = catalogue(), models()
    datums = {r.get('datum', 'relative-recorded-height') for r in built['regions']}
    travel_models = built.get('travelModels', [])
    diagnostics = {t['id']: t for r in travel_models for t in r['tracks']}
    return {'version': source['version'], 'modelVersion': built.get('sourceVersion', 0),
            'needsRebuild': source['version'] != built.get('sourceVersion') or built.get('schema') != SCHEMA or built.get('travelSchema') != travel.SCHEMA,
            'builtAt': built.get('builtAt'), 'tracks': [{**t, 'speedLearning': diagnostics.get(t['id'])} for t in source['tracks']],
            'regions': built['regions'], 'travelModels': travel_models,
            'pointCount': sum(t['pointCount'] for t in source['tracks'] if t['enabled']),
            'shared': True, 'datum': next(iter(datums)) if len(datums) == 1 else
            'mixed-height-references' if datums else 'relative-recorded-height'}


def parse_time(text):
    try:
        value = datetime.fromisoformat((text or '').strip().replace('Z', '+00:00'))
    except ValueError:
        return np.nan
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).timestamp()


TRACK_SUFFIXES = ('.kml', '.gpx', '.tcx')


def read_track_xml(content, filename):
    """The XML root of a recording; DTDs and entities are never expanded."""
    if not filename.lower().endswith(TRACK_SUFFIXES) or len(content) > MAX_FILE_BYTES:
        raise ValueError('请选择不超过 20 MiB 的 KML、GPX 或 TCX 轨迹文件')
    if re.search(br'<!\s*(?:DOCTYPE|ENTITY)\b', content, re.I):
        raise ValueError('轨迹文件不能包含 DTD 或实体定义')
    try:
        return ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError('轨迹文件无法解析') from exc


def tag(element):
    return element.tag.rsplit('}', 1)[-1]


def child_text(element, *path):
    for name in path:
        element = next((e for e in element if tag(e) == name), None)
        if element is None:
            return None
    return element.text


class TrackPoints:
    """Accumulates validated observations; invalid points split the segment."""
    def __init__(self):
        self.rows, self.times, self.breaks, self.rejected = [], [], [], 0

    def segment(self):
        self.start = len(self.rows)

    def add(self, values, stamp=np.nan):
        try:
            if len(values) < 3 or any(v is None for v in values[:3]):
                raise ValueError()
            point = np.asarray(values[:3], float)
            if not np.isfinite(point).all() or abs(point[0]) > 180 or abs(point[1]) > 85 or not -500 <= point[2] <= 9000:
                raise ValueError()
        except ValueError:
            self.rejected += 1
            if self.rows and (not self.breaks or self.breaks[-1] != len(self.rows)):
                self.breaks.append(len(self.rows))
            return
        self.rows.append(point); self.times.append(stamp)
        if len(self.rows) > MAX_TRACK_POINTS:
            raise ValueError('单份轨迹超过 25 万个观测点，请分段导出')

    def end(self):
        if len(self.rows) > self.start:
            self.breaks.append(self.start)

    def record(self, document, content, filename, modes=(), names=('name',)):
        if len(self.rows) < 3:
            raise ValueError('轨迹至少需要 3 个带有效高程的坐标；仅有经纬度不能重建地形')
        points = np.asarray(self.rows)
        if np.all(points[:, 2] == 0) and 'absolute' not in modes:
            raise ValueError('轨迹高程全部为 0，可能未记录海拔；请导出包含实际高程的数据')
        name = next(((e.text or '').strip() for e in document.iter() if tag(e) in names and (e.text or '').strip()), Path(filename).name)
        summary, arrays = track_record(points, self.breaks, self.times, name, filename, hashlib.sha256(content).hexdigest(), sorted(modes))
        summary['rejectedPoints'] = self.rejected
        return summary, arrays


def parse_track(content, filename):
    """A KML, GPX or TCX recording, dispatched by file extension."""
    suffix = Path(filename).suffix.lower()
    return {'.gpx': parse_gpx, '.tcx': parse_tcx}.get(suffix, parse_kml)(content, filename)


def parse_kml(content, filename):
    """KML LineString and gx:Track, ignoring descriptions and network links."""
    document = read_track_xml(content, filename)
    # Some apps write the same walk as a timed gx:Track and again as a
    # LineString; counting both would double its weight.
    geometries = ([e for e in document.iter() if tag(e) == 'Track'] or [e for e in document.iter() if tag(e) == 'LineString']
                  or [e for e in document.iter() if tag(e) == 'Point'])
    track, modes = TrackPoints(), set()
    # Per-point Placemarks (Huawei Health) carry their clock in a TimeSpan or
    # TimeStamp; their untimed Start/End markers repeat track points.
    point_times = {}
    for placemark in (e for e in document.iter() if tag(e) == 'Placemark'):
        stamp = child_text(placemark, 'TimeStamp', 'when') or child_text(placemark, 'TimeSpan', 'begin')
        for point in (e for e in placemark if tag(e) == 'Point'):
            point_times[id(point)] = parse_time(stamp)
    timed_points = geometries and tag(geometries[0]) == 'Point' and any(np.isfinite(t) for t in point_times.values())
    if timed_points:
        geometries = [g for g in geometries if np.isfinite(point_times.get(id(g), np.nan))]
    for geometry in geometries:
        modes.update((e.text or '').strip() for e in geometry if tag(e) == 'altitudeMode')
        if modes & {'relativeToGround', 'relativeToSeaFloor'}:
            raise ValueError('相对地面或海底的高度不能用于重建地形，请导出记录海拔的轨迹')
        track.segment()
        elements = [e for e in geometry.iter() if tag(e) in ('coordinates', 'coord')]
        # gx:Track pairs its n <when> with its n <gx:coord>. Never synthesize
        # missing clocks: these now also supply observed moving speeds.
        whens = [e for e in geometry if tag(e) == 'when'] if tag(geometry) == 'Track' else []
        stamps = [parse_time(e.text) for e in whens] if len(whens) == len(elements) else [np.nan]*len(elements)
        for element, stamp in zip(elements, stamps):
            kind = tag(element)
            # Tolerate "lng,lat, height" as written by some exporters.
            values = re.sub(r'\s*,\s*', ',', (element.text or '').strip()).split()
            if kind == 'coordinates':
                stamp = point_times.get(id(geometry), np.nan) if tag(geometry) == 'Point' else np.nan
            for token in ([word.split(',') for word in values] if kind == 'coordinates' else [values]):
                track.add(token, stamp)
        track.end()
    # Timed per-point exports are one walk, not a segment per point.
    if timed_points:
        track.breaks = []
    return track.record(document, content, filename, modes)


def parse_gpx(content, filename):
    """GPX track segments (or routes) with <ele> heights and optional <time>."""
    document = read_track_xml(content, filename)
    segments = [e for e in document.iter() if tag(e) == 'trkseg'] or [e for e in document.iter() if tag(e) == 'rte']
    track = TrackPoints()
    for segment in segments:
        track.segment()
        for point in (e for e in segment if tag(e) in ('trkpt', 'rtept')):
            track.add([point.get('lon'), point.get('lat'), child_text(point, 'ele')], parse_time(child_text(point, 'time')))
        track.end()
    return track.record(document, content, filename, names=('name',))


def parse_tcx(content, filename):
    """TCX laps' Track elements; trackpoints without a position are skipped."""
    document = read_track_xml(content, filename)
    track = TrackPoints()
    for segment in (e for e in document.iter() if tag(e) == 'Track'):
        track.segment()
        for point in (e for e in segment if tag(e) == 'Trackpoint'):
            if child_text(point, 'Position', 'LatitudeDegrees') is None:
                continue
            track.add([child_text(point, 'Position', 'LongitudeDegrees'), child_text(point, 'Position', 'LatitudeDegrees'),
                       child_text(point, 'AltitudeMeters')], parse_time(child_text(point, 'Time')))
        track.end()
    return track.record(document, content, filename, names=('Name',))


def track_record(points, breaks, times, name, file_name, source_sha256, modes):
    """A shared-track summary and arrays for normalized height observations."""
    points = np.array(points, float)
    # Export timestamps, filenames and whitespace do not create duplicate surveys.
    points[:, :2] = np.round(points[:, :2], 6)
    points[:, 2] = np.round(points[:, 2], 1)
    breaks = np.asarray(sorted(set([0, *[i for i in breaks if i < len(points)]])), np.int32)
    digest = hashlib.sha256(points.astype('<f8').tobytes()+breaks.astype('<i4').tobytes()).hexdigest()
    times = np.asarray(times, float)
    summary = {'id': digest, 'geometryId': digest, 'name': name[:120], 'fileName': Path(file_name).name[:180],
               'pointCount': len(points), 'rejectedPoints': 0,
               'recordedAt': float(np.nanmin(times)) if np.isfinite(times).any() else None,
               'bbox': [*points[:, :2].min(axis=0).tolist(), *points[:, :2].max(axis=0).tolist()],
               'minHeightM': float(points[:, 2].min()), 'maxHeightM': float(points[:, 2].max()),
               'sourceSha256': source_sha256, 'altitudeModes': list(modes), 'enabled': True,
               'activity': 'auto', 'timedPointCount': int(np.isfinite(times).sum()),
               'timingFingerprint': hashlib.sha256(np.nan_to_num(times, nan=-1).astype('<f8').tobytes()).hexdigest()}
    return summary, {'points': points, 'breaks': breaks, 'times': times}


def write_npz(path, arrays):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix='.tmp', delete=False) as stream:
        temporary = Path(stream.name)
        try:
            np.savez_compressed(stream, **arrays)
            stream.flush(); os.fsync(stream.fileno())
            temporary.replace(path)
            store.sync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)


def add_tracks(files, owner):
    if not owner or not owner.get('id'):
        raise NotOwner('请登录后贡献轨迹')
    if not files or len(files) > 32 or sum(len(content) for _, content in files) > MAX_BATCH_BYTES:
        raise ValueError('每批最多 32 份轨迹，总大小不超过 64 MiB')
    parsed, errors = [], []
    for name, content in files:
        try:
            parsed.append(parse_track(content, name))
        except ValueError as exc:
            errors.append({'fileName': Path(name).name, 'message': str(exc)})
    if not parsed:
        raise ValueError('；'.join(item['fileName']+'：'+item['message'] for item in errors))
    return store_tracks(parsed, owner, errors)


def store_tracks(parsed, owner, errors=()):
    """Add parsed recordings under `owner`; identical observations are kept once."""
    if not owner or not owner.get('id'):
        raise NotOwner('请登录后贡献轨迹')
    errors = list(errors)
    with store.LOCK:
        source = catalogue()
        existing = {t['id']: t for t in source['tracks']}
        # Equal coordinates can be separate walks/rides at different times.
        # Keep legacy geometry IDs for the first survey and untimed upgrades,
        # but do not discard a later, fully timed traversal of the same path.
        timelines, resolved = {}, []
        def complete_time(values):
            return values is not None and np.ndim(values) == 1 and len(values) > 1 and np.isfinite(values).all() and (np.diff(values) > 0).all()
        for summary, arrays in parsed:
            identifier, times = summary['id'], arrays.get('times')
            if identifier not in timelines:
                previous = None
                if identifier in existing:
                    with np.load(root()/'tracks'/(identifier+'.npz'), allow_pickle=False) as saved:
                        previous = saved['times'] if 'times' in saved.files else None
                timelines[identifier] = previous
            previous = timelines[identifier]
            digest = hashlib.sha256(identifier.encode()+np.asarray(times, '<f8').tobytes()).hexdigest() if complete_time(times) else None
            if digest in existing or (complete_time(previous) and complete_time(times) and not np.array_equal(previous, times)):
                summary = {**summary, 'id': digest, 'geometryId': identifier}
            elif not complete_time(previous) and complete_time(times):
                timelines[identifier] = times
            resolved.append((summary, arrays))
        parsed = resolved
        added, duplicates, upgraded = [], [], []
        new = {summary['id']: (summary, arrays) for summary, arrays in parsed if summary['id'] not in existing}
        total = sum(t['pointCount'] for t in source['tracks'])+sum(summary['pointCount'] for summary, _ in new.values())
        if total > MAX_TOTAL_POINTS or len(existing)+len(new) > MAX_TRACKS:
            raise ValueError('共享轨迹库已达到容量限制，请先整理不再使用的轨迹')
        root().mkdir(parents=True, exist_ok=True); root().chmod(0o700)
        if shutil.disk_usage(root()).free < 128*1024**2+sum(arrays['points'].nbytes for _, arrays in new.values()):
            raise ValueError('服务器存储空间不足，已有地形仍可查看')
        for summary, arrays in parsed:
            if summary['id'] in existing:
                duplicates.append({'id': summary['id'], 'fileName': summary['fileName'], 'ownerName': existing[summary['id']]['ownerName']})
                # A later timed export can enrich the owner's untimed survey,
                # without duplicating its terrain weight or transferring it.
                record = existing[summary['id']]
                if record['ownerId'] == owner['id'] and summary.get('timedPointCount', 0):
                    path = root()/'tracks'/(record['id']+'.npz')
                    with np.load(path, allow_pickle=False) as saved:
                        old_count = int(np.isfinite(saved['times']).sum()) if 'times' in saved.files else 0
                    if summary['timedPointCount'] > old_count:
                        write_npz(path, arrays)
                        record.update({k: summary[k] for k in ('timedPointCount', 'timingFingerprint', 'recordedAt')})
                        upgraded.append(record['id'])
                continue
            record = {**summary, 'ownerId': owner['id'], 'ownerName': owner['username'], 'createdAt': time.time()}
            write_npz(root()/'tracks'/(record['id']+'.npz'), arrays)
            source['tracks'].append(record); existing[record['id']] = record; added.append(record['id'])
        if added or upgraded:
            source['version'] += 1
            store.atomic_json(root()/'tracks.json', source)
        return {'version': source['version'], 'added': added, 'duplicates': duplicates, 'timingUpgraded': upgraded, 'errors': errors}


def change_track(identifier, owner, expected_version, *, name=None, enabled=None, activity=None, delete=False):
    with store.LOCK:
        source = catalogue()
        record = next((t for t in source['tracks'] if t['id'] == identifier), None)
        if record is None:
            raise ValueError('该轨迹不存在或已删除')
        if not owner or owner.get('id') != record['ownerId']:
            raise NotOwner('只能管理自己上传的轨迹')
        if source['version'] != expected_version:
            raise Conflict('共享轨迹库已更新，请刷新后重试')
        changed = False
        if delete:
            source['tracks'].remove(record); changed = True
        else:
            if activity is not None:
                if activity not in travel.travel_model.ACTIVITIES or record.get('kind') == 'contours':
                    raise ValueError('请选择有效的轨迹活动类型；等高线不参与速度学习')
                if activity != record.get('activity', 'auto'):
                    record['activity'] = activity; changed = True
            if name is not None and name.strip() != record['name']:
                if not 1 <= len(name.strip()) <= 120:
                    raise ValueError('轨迹名称须为 1–120 个字符')
                record['name'] = name.strip(); changed = True
            if enabled is not None and bool(enabled) != record['enabled']:
                record['enabled'] = bool(enabled); changed = True
        if changed:
            source['version'] += 1
            store.atomic_json(root()/'tracks.json', source)
            if delete:
                (root()/'tracks'/(identifier+'.npz')).unlink(missing_ok=True)
        return {'version': source['version'], 'changed': changed}


def label_activities(activities, owner, expected_version):
    """Apply a contributor's known labels atomically, including existing tracks."""
    if not isinstance(activities, dict) or len(activities) > MAX_TRACKS:
        raise ValueError('活动标注必须按轨迹 ID 提供，且不能超过轨迹库容量')
    with store.LOCK:
        source = catalogue()
        if source['version'] != expected_version:
            raise Conflict('共享轨迹库已更新，请刷新后重试')
        records = {t['id']: t for t in source['tracks']}
        for identifier, activity in activities.items():
            if identifier not in records:
                raise ValueError('活动标注中的轨迹不存在：'+str(identifier))
            if not owner or owner.get('id') != records[identifier]['ownerId']:
                raise NotOwner('只能标注自己上传的轨迹')
            if activity not in travel.travel_model.ACTIVITIES or records[identifier].get('kind') == 'contours':
                raise ValueError('活动类型无效或所选记录不是轨迹')
        changed = []
        for identifier, activity in activities.items():
            if records[identifier].get('activity', 'auto') != activity:
                records[identifier]['activity'] = activity; changed.append(identifier)
        if changed:
            source['version'] += 1
            store.atomic_json(root()/'tracks.json', source)
        return {'version': source['version'], 'changed': changed}


def rebuild(expected_version, progress=lambda p, m: None, operation_id=None):
    with store.LOCK:
        source, previous = catalogue(), models()
        if previous.get('operationId') == operation_id and operation_id:
            return public_status()
        if source['version'] != expected_version:
            raise Conflict('重建期间又有轨迹更新，请使用最新数据重新重建')
        if previous.get('sourceVersion') == expected_version and previous.get('schema') == SCHEMA and previous.get('travelSchema') == travel.SCHEMA:
            return public_status()
        tracks = []
        for record in source['tracks']:
            if record['enabled']:
                with np.load(root()/'tracks'/(record['id']+'.npz'), allow_pickle=False) as data:
                    tracks.append({**record, 'points': data['points'], 'times': data['times'] if 'times' in data.files else None,
                                   'breaks': data['breaks'] if 'breaks' in data.files else np.array([0], np.int32)})
    progress(10, '按地理位置整理共享轨迹')
    regions, used_ids = [], set()
    groups = spatial_groups(tracks)
    for index, group in enumerate(groups):
        ids = {t['id'] for t in group}
        low = np.min([t['points'][:, :2].min(axis=0) for t in group], axis=0)
        high = np.max([t['points'][:, :2].max(axis=0) for t in group], axis=0)
        old = max((r for r in previous['regions'] if r['id'] not in used_ids and
                   r['bbox'][0] <= high[0] and r['bbox'][2] >= low[0] and r['bbox'][1] <= high[1] and r['bbox'][3] >= low[1]),
                  key=lambda r: len(ids.intersection(r['trackIds'])), default=None)
        if old and not ids.intersection(old['trackIds']):
            old = None
        identifier = old['id'] if old else uuid.uuid4().hex[:16]
        used_ids.add(identifier)
        signature = fingerprint({'schema': SCHEMA, 'tracks': sorted(ids),
                                 'timing': sorted([t['id'], hashlib.sha256(np.nan_to_num(np.asarray(t.get('times', []), float), nan=-1).astype('<f8').tobytes()).hexdigest()] for t in group),
                                 'bounds': [[t['id'], t['points'][:, :2].min(axis=0).tolist(), t['points'][:, :2].max(axis=0).tolist()] for t in group]})
        if old and old.get('inputSignature', old.get('fingerprint')) == signature and (root()/old['artifact']).exists():
            regions.append(old)
            continue
        progress(15+int(70*index/max(1, len(groups))), f'校正轨迹高程并重建区域 {index+1}/{len(groups)}')
        metadata, arrays = reconstruct(group)
        name = f'地形区域 {index+1}'
        # Built-in place names supply context; reconstruction needs no OSM download.
        from . import sites
        for site in sites.builtin():
            west, south, east, north = site['bbox']
            lng, lat = metadata['frame']['origin']
            if west <= lng <= east and south <= lat <= north:
                name = site['name']; break
        # Published courses pin a model file by name: it is named after its
        # content, so no later reconstruction can ever rewrite those heights.
        digest = hashlib.sha256()
        for key in sorted(arrays):
            digest.update(key.encode()); digest.update(np.ascontiguousarray(arrays[key]).tobytes())
        artifact = 'models/'+digest.hexdigest()[:40]+'.npz'
        if not (root()/artifact).exists():
            write_npz(root()/artifact, arrays)
        regions.append({**metadata, 'id': identifier, 'name': (old or {}).get('name', name), 'inputSignature': signature,
                        'fingerprint': fingerprint({'input': signature, 'content': digest.hexdigest()}), 'artifact': artifact})
    travel_models = travel.rebuild(tracks, regions, previous.get('travelModels', []), progress)
    with store.LOCK:
        if catalogue()['version'] != expected_version:
            raise Conflict('重建期间又有轨迹更新，请使用最新数据重新重建')
        history, counts = [], {}
        current_files = {r['artifact'] for r in regions}
        for record in [*previous['regions'], *previous.get('history', [])]:
            if record['artifact'] not in current_files and counts.get(record['id'], 0) < 2:
                history.append(record); current_files.add(record['artifact']); counts[record['id']] = counts.get(record['id'], 0)+1
        store.atomic_json(root()/'models.json', {'schema': SCHEMA, 'sourceVersion': expected_version,
                          'regions': regions, 'history': history, 'travelSchema': travel.SCHEMA, 'travelModels': travel_models,
                          'builtAt': time.time(), 'operationId': operation_id})
        try:
            prune_models()
        except (OSError, ValueError):
            pass  # housekeeping must not turn a committed reconstruction into a failed import
    progress(100, '共享地形与路线耗时模型已更新')
    return public_status()


def prune_models():
    """Published courses pin height inputs, just as they pin their map image."""
    with store.LOCK:
        built = models()
        keep = {r['artifact'] for r in [*built['regions'], *built.get('history', [])]}
        keep.update(r['artifact'] for r in built.get('travelModels', []))
        candidates = [p for p in [*(root()/'models').glob('*.npz'), *(root()/'travel-models').glob('*.json')]
                      if p.relative_to(root()).as_posix() not in keep and time.time()-p.stat().st_mtime > 86400]
        if not candidates:
            return
        # Both saved calculations and published courses pin their inputs.
        for pattern in ('releases/*/release.json', 'projects/*/plan-*.json', 'projects/*/comparison-*.json',
                        'workspaces/*/projects/*/plan-*.json', 'workspaces/*/projects/*/comparison-*.json'):
            for path in store.DATA.glob(pattern):
                value = store.read_json(path, {})
                frozen = (value.get('plan', value.get('content', value)).get('elevationSnapshot') or {})
                keep.update(r['artifact'] for r in frozen.get('models', []))
                keep.update(r['artifact'] for r in (frozen.get('travel') or {}).get('models', []))
        for path in candidates:
            if path.relative_to(root()).as_posix() not in keep:
                path.unlink(missing_ok=True)


def region(identifier):
    record = next((r for r in models()['regions'] if r['id'] == identifier), None)
    if record is None:
        raise ValueError('该地形区域不存在或已合并，请刷新区域列表')
    return record


def model_grid(identifier, track_id=None):
    record = region(identifier)
    if track_id is not None and track_id not in record['trackIds']:
        raise ValueError('这条轨迹未参与该地区的当前地形')
    with np.load(root()/record['artifact'], allow_pickle=False) as arrays:
        result = {**record}
        points = arrays['observations']
        chosen = None
        if track_id is not None:
            if 'observation_track' not in arrays.files:
                raise ValueError('这份旧地形尚无逐轨迹信息，请先合并最新轨迹')
            points = points[arrays['observation_track'] == record['trackIds'].index(track_id)]
            rows, columns = record['rows'], record['columns']
            yy, xx = np.mgrid[:rows, :columns]
            query = np.column_stack((record['origin'][0]+xx.ravel()*record['spacingM'],
                                     record['origin'][1]+yy.ravel()*record['spacingM']))
            chosen = supported_nodes(Frame.of(record).to_local(points[:, :2]), query, (rows, columns))[0] if len(points) else np.zeros((rows, columns), bool)
            chosen &= np.isfinite(arrays['height'])
            alignment = [t for t in record['alignment']['tracks'] if t['id'] == track_id]
            result.update(selectedTrackId=track_id, trackIds=[track_id], pointCount=len(points), observationCells=len(points),
                          supportedCells=int(chosen.sum()), coverageFraction=float(chosen.mean()),
                          alignment={**record['alignment'], 'tracks': alignment})
        for name in ('height', 'support', 'scatter'):
            values = arrays[name] if chosen is None else np.where(chosen, arrays[name], np.nan)
            result[name] = [round(float(v), 2) if np.isfinite(v) else None for v in values.ravel()]
        if chosen is not None:
            values = arrays['height'][chosen]
            result.update(minHeightM=round(float(values.min()), 1) if len(values) else None,
                          maxHeightM=round(float(values.max()), 1) if len(values) else None)
        stride = max(1, int(np.ceil(len(points)/5000)))
        shown = points[::stride].copy()
        shown[:, :2] = np.round(shown[:, :2], 6); shown[:, 2] = np.round(shown[:, 2], 2)
        result['observations'] = shown.tolist()
    result['context'] = geographic_context(record)
    return result


def track_detail(identifier):
    """The contributed observations and clock-derived diagnostics, read only."""
    with store.LOCK:
        record = next((t for t in catalogue()['tracks'] if t['id'] == identifier), None)
        if record is None:
            raise ValueError('这条轨迹不存在或已删除')
        with np.load(root()/'tracks'/(identifier+'.npz'), allow_pickle=False) as data:
            points = data['points'].copy()
            times = data['times'].copy() if 'times' in data.files else np.full(len(points), np.nan)
            breaks = data['breaks'].copy() if 'breaks' in data.files else np.array([0])
        regions = [r for r in models()['regions'] if identifier in r['trackIds']]
    frame = Frame(*np.mean(points[:, :2], axis=0))
    xy = frame.to_local(points[:, :2])
    distances = np.linalg.norm(np.diff(xy, axis=0), axis=1)
    split = np.zeros(len(points), bool); split[breaks] = True
    dt = np.diff(times)
    invalid = (distances > 300) | (np.isfinite(dt) & ((dt <= 0) | (dt > travel.travel_model.MAX_GAP_SEC)))
    split[1:] |= invalid
    along = np.r_[0., np.cumsum(np.where(split[1:], 0., distances))]
    sampler = Sampler(regions)
    # Keep each geometry part separate while limiting browser payload size.
    stride = max(1, int(np.ceil(len(points)/4000)))
    starts = np.flatnonzero(split)
    samples = []
    for a, b in zip(starts, np.r_[starts[1:], len(points)]):
        indexes = np.unique(np.r_[np.arange(a, b, stride), b-1])
        heights = sampler.sample(points[indexes, :2])
        for k, i in enumerate(indexes):
            samples.append({'lng': float(points[i, 0]), 'lat': float(points[i, 1]),
                'heightM': float(points[i, 2]) if record.get('heightReference') != 'shape' else None,
                'surfaceHeightM': round(float(heights[k]), 2) if np.isfinite(heights[k]) else None,
                'distanceM': round(float(along[i]), 1), 'breakBefore': k == 0})
    rows, summary = travel.travel_model.observations({**record, 'points': points, 'times': times, 'breaks': breaks}, frame, sampler)
    first_time = float(np.nanmin(times)) if np.isfinite(times).any() else 0.
    windows = []
    # Min/max/median statistics above always use every observation. The
    # line chart uses at most 1200 evenly spaced windows, not invented times.
    for row in rows[::max(1, int(np.ceil(len(rows)/1200)))]:
        windows.append({'elapsedSec': round(row['time']-first_time, 1), 'durationSec': round(row['seconds'], 1),
                        'speedMps': round(row['distance']/row['seconds'], 3), 'grade': row['grade'],
                        'episode': row['episode']})
    return {'track': record, 'regions': [{k: r[k] for k in ('id', 'name', 'fingerprint')} for r in regions],
            'alignment': [{'regionId': r['id'], **a} for r in regions for a in r['alignment']['tracks'] if a['id'] == identifier],
            'distanceM': round(float(along[-1]), 1), 'samples': samples, 'speedLearning': summary, 'speedWindows': windows}


def geographic_context(record):
    """Only public built-in OSM references, never another workbench's maps."""
    from . import sites
    from .reference import geojson
    lng, lat = record['frame']['origin']
    site = next((s for s in sites.builtin() if s['bbox'][0] <= lng <= s['bbox'][2] and s['bbox'][1] <= lat <= s['bbox'][3]), None)
    if site is None:
        return {'type': 'FeatureCollection', 'features': []}
    try:
        features = sites.reference(site['id'])
    except ValueError:
        return {'type': 'FeatureCollection', 'features': []}
    west, south, east, north = record['bbox']
    kept = []
    for feature in features:
        points = np.asarray(feature['lnglat'])
        if points[:, 0].max() >= west and points[:, 0].min() <= east and points[:, 1].max() >= south and points[:, 1].min() <= north:
            kept.append(feature)
    return geojson(kept[:2500])


def geotiff(identifier):
    """Float32 WGS84 raster; unknown cells remain NaN, with no vertical datum claim."""
    from PIL import Image, TiffImagePlugin
    record = region(identifier)
    frame = Frame.of(record)
    scale = record['spacingM']/frame.scales
    west, south = frame.to_lnglat(record['origin'])
    north = south+(record['rows']-1)*scale[1]
    tags = TiffImagePlugin.ImageFileDirectory_v2()
    tags.tagtype[33550] = 12; tags[33550] = (float(scale[0]), float(scale[1]), 0.)
    tags.tagtype[33922] = 12; tags[33922] = (0., 0., 0., float(west-scale[0]/2), float(north+scale[1]/2), 0.)
    tags.tagtype[34735] = 3
    tags[34735] = (1, 1, 0, 4, 1024, 0, 1, 2, 1025, 0, 1, 1, 2048, 0, 1, 4326, 2054, 0, 1, 9102)
    tags.tagtype[42113] = 2; tags[42113] = 'nan'
    tags[270] = json.dumps({'source': 'DirMap shared GPS tracks', 'model': record['fingerprint'],
                           'horizontalCRS': 'EPSG:4326', 'heightUnit': 'm', 'verticalDatum': 'relative recorded height; not surveyed'}, ensure_ascii=True)
    output = io.BytesIO()
    with np.load(root()/record['artifact'], allow_pickle=False) as data:
        Image.fromarray(np.flipud(data['height']).astype(np.float32)).save(output, format='TIFF', tiffinfo=tags, compression='tiff_adobe_deflate')
    return output.getvalue()


def relevant(state):
    if not state or not state.get('registration'):
        return []
    from .geometry import Registration
    reg = Registration.from_dict(state['registration'])
    local = reg.target if len(reg.target) else affine_points(
        reg.affine, [[0, 0], [state['width']-1, 0], [state['width']-1, state['height']-1], [0, state['height']-1]])
    points = Frame.of(state).to_lnglat(local)
    low, high = points.min(axis=0), points.max(axis=0)
    return [r for r in models()['regions'] if r.get('supportedCells', 0) and
            r['bbox'][0] <= high[0] and r['bbox'][2] >= low[0] and r['bbox'][1] <= high[1] and r['bbox'][3] >= low[1]]


def signature(state, enabled=True):
    return fingerprint(sorted(r['fingerprint'] for r in relevant(state))) if enabled else 'disabled'


class Sampler:
    def __init__(self, records):
        self.records = records
        self.models = []
        with _model_lock:
            for record in records:
                key = str(root().resolve()), record['fingerprint']
                model = _models.get(key)
                if model is None:
                    try:
                        with np.load(root()/record['artifact'], allow_pickle=False) as data:
                            model = HeightModel(record, {'height': data['height']})
                    except OSError as exc:
                        raise ValueError('这份路线所用的共享地形快照已不可用，请重新生成路线') from exc
                    _models[key] = model
                _models.move_to_end(key)
                self.models.append(model)
            while len(_models) > 8:
                _models.popitem(last=False)

    def sample(self, lnglat):
        points = np.asarray(lnglat).reshape(-1, 2)
        result = np.full(len(points), np.nan)
        for model in self.models:
            pending = np.flatnonzero(~np.isfinite(result))
            if not len(pending):
                break
            result[pending] = model.sample(points[pending])
        return result
