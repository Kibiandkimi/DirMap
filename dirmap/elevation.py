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

from . import store
from .elevation_model import HeightModel, SCHEMA, fingerprint, reconstruct, spatial_groups
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
    return store.read_json(root()/'models.json', {'sourceVersion': 0, 'schema': SCHEMA, 'regions': []})


def public_status():
    with store.LOCK:
        source, built = catalogue(), models()
    return {'version': source['version'], 'modelVersion': built.get('sourceVersion', 0),
            'needsRebuild': source['version'] != built.get('sourceVersion') or built.get('schema') != SCHEMA,
            'builtAt': built.get('builtAt'), 'tracks': source['tracks'], 'regions': built['regions'],
            'pointCount': sum(t['pointCount'] for t in source['tracks'] if t['enabled']),
            'shared': True, 'datum': 'relative-recorded-height'}


def parse_time(text):
    try:
        value = datetime.fromisoformat((text or '').strip().replace('Z', '+00:00'))
    except ValueError:
        return np.nan
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).timestamp()


def parse_kml(content, filename):
    """KML LineString and gx:Track, ignoring descriptions and network links."""
    if not filename.lower().endswith('.kml') or len(content) > MAX_FILE_BYTES:
        raise ValueError('请选择不超过 20 MiB 的 KML 轨迹文件')
    if re.search(br'<!\s*(?:DOCTYPE|ENTITY)\b', content, re.I):
        raise ValueError('KML 不能包含 DTD 或实体定义')
    try:
        document = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError('KML 文件无法解析') from exc
    def tag(element):
        return element.tag.rsplit('}', 1)[-1]
    # Some apps write the same walk as a timed gx:Track and again as a
    # LineString; counting both would double its weight.
    geometries = ([e for e in document.iter() if tag(e) == 'Track'] or [e for e in document.iter() if tag(e) == 'LineString']
                  or [e for e in document.iter() if tag(e) == 'Point'])
    rows, times, breaks, rejected = [], [], [], 0
    modes = set()
    for geometry in geometries:
        modes.update((e.text or '').strip() for e in geometry if tag(e) == 'altitudeMode')
        if modes & {'relativeToGround', 'relativeToSeaFloor'}:
            raise ValueError('相对地面或海底的高度不能用于重建地形，请导出记录海拔的轨迹')
        start = len(rows)
        elements = [e for e in geometry.iter() if tag(e) in ('coordinates', 'coord')]
        # gx:Track pairs its n <when> with its n <gx:coord>; the times only
        # help separate a phone's slow height drift from the terrain.
        whens = [e for e in geometry if tag(e) == 'when'] if tag(geometry) == 'Track' else []
        stamps = [parse_time(e.text) for e in whens] if len(whens) == len(elements) else [np.nan]*len(elements)
        for element, stamp in zip(elements, stamps):
            kind = tag(element)
            values = (element.text or '').split()
            tokens = [word.split(',') for word in values] if kind == 'coordinates' else [values]
            for token in tokens:
                try:
                    if len(token) < 3:
                        raise ValueError()
                    point = np.asarray(token[:3], float)
                    if not np.isfinite(point).all() or abs(point[0]) > 180 or abs(point[1]) > 85 or not -500 <= point[2] <= 9000:
                        raise ValueError()
                except ValueError:
                    rejected += 1
                    if rows and (not breaks or breaks[-1] != len(rows)):
                        breaks.append(len(rows))
                    continue
                rows.append(point); times.append(stamp if kind == 'coord' else np.nan)
                if len(rows) > MAX_TRACK_POINTS:
                    raise ValueError('单份轨迹超过 25 万个观测点，请分段导出')
        if len(rows) > start:
            breaks.append(start)
    if len(rows) < 3:
        raise ValueError('轨迹至少需要 3 个带有效高程的坐标；仅有经纬度不能重建地形')
    points = np.asarray(rows)
    if np.all(points[:, 2] == 0) and 'absolute' not in modes:
        raise ValueError('轨迹高程全部为 0，可能未记录海拔；请导出包含实际高程的数据')
    # Export timestamps, filenames and whitespace do not create duplicate surveys.
    points[:, :2] = np.round(points[:, :2], 6)
    points[:, 2] = np.round(points[:, 2], 1)
    breaks = np.asarray(sorted(set([0, *[i for i in breaks if i < len(points)]])), np.int32)
    digest = hashlib.sha256(points.astype('<f8').tobytes()+breaks.astype('<i4').tobytes()).hexdigest()
    name = next(((e.text or '').strip() for e in document.iter() if tag(e) == 'name' and (e.text or '').strip()), Path(filename).name)
    times = np.asarray(times, float)
    summary = {'id': digest, 'name': name[:120], 'fileName': Path(filename).name[:180],
               'pointCount': len(points), 'rejectedPoints': rejected,
               'recordedAt': float(np.nanmin(times)) if np.isfinite(times).any() else None,
               'bbox': [*points[:, :2].min(axis=0).tolist(), *points[:, :2].max(axis=0).tolist()],
               'minHeightM': float(points[:, 2].min()), 'maxHeightM': float(points[:, 2].max()),
               'sourceSha256': hashlib.sha256(content).hexdigest(), 'altitudeModes': sorted(modes),
               'enabled': True}
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
        raise ValueError('每批最多 32 份 KML，总大小不超过 64 MiB')
    parsed, errors = [], []
    for name, content in files:
        try:
            parsed.append(parse_kml(content, name))
        except ValueError as exc:
            errors.append({'fileName': Path(name).name, 'message': str(exc)})
    if not parsed:
        raise ValueError('；'.join(item['fileName']+'：'+item['message'] for item in errors))
    with store.LOCK:
        source = catalogue()
        existing = {t['id']: t for t in source['tracks']}
        added, duplicates = [], []
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
                continue
            record = {**summary, 'ownerId': owner['id'], 'ownerName': owner['username'], 'createdAt': time.time()}
            write_npz(root()/'tracks'/(record['id']+'.npz'), arrays)
            source['tracks'].append(record); existing[record['id']] = record; added.append(record['id'])
        if added:
            source['version'] += 1
            store.atomic_json(root()/'tracks.json', source)
        return {'version': source['version'], 'added': added, 'duplicates': duplicates, 'errors': errors}


def change_track(identifier, owner, expected_version, *, name=None, enabled=None, delete=False):
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


def rebuild(expected_version, progress=lambda p, m: None, operation_id=None):
    with store.LOCK:
        source, previous = catalogue(), models()
        if previous.get('operationId') == operation_id and operation_id:
            return public_status()
        if source['version'] != expected_version:
            raise Conflict('重建期间又有轨迹更新，请使用最新数据重新重建')
        if previous.get('sourceVersion') == expected_version and previous.get('schema') == SCHEMA:
            return public_status()
        tracks = []
        for record in source['tracks']:
            if record['enabled']:
                with np.load(root()/'tracks'/(record['id']+'.npz'), allow_pickle=False) as data:
                    tracks.append({**record, 'points': data['points'], 'times': data['times'] if 'times' in data.files else None})
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
    with store.LOCK:
        if catalogue()['version'] != expected_version:
            raise Conflict('重建期间又有轨迹更新，请使用最新数据重新重建')
        history, counts = [], {}
        current_files = {r['artifact'] for r in regions}
        for record in [*previous['regions'], *previous.get('history', [])]:
            if record['artifact'] not in current_files and counts.get(record['id'], 0) < 2:
                history.append(record); current_files.add(record['artifact']); counts[record['id']] = counts.get(record['id'], 0)+1
        store.atomic_json(root()/'models.json', {'schema': SCHEMA, 'sourceVersion': expected_version,
                          'regions': regions, 'history': history, 'builtAt': time.time(), 'operationId': operation_id})
        try:
            prune_models()
        except (OSError, ValueError):
            pass  # housekeeping must not turn a committed reconstruction into a failed import
    progress(100, '共享地形已更新')
    return public_status()


def prune_models():
    """Published courses pin height inputs, just as they pin their map image."""
    with store.LOCK:
        built = models()
        keep = {r['artifact'] for r in [*built['regions'], *built.get('history', [])]}
        for path in (store.DATA/'releases').glob('*/release.json'):
            release = store.read_json(path, {})
            keep.update(r['artifact'] for r in (release.get('plan', {}).get('elevationSnapshot') or {}).get('models', []))
        for path in (root()/'models').glob('*.npz'):
            if 'models/'+path.name not in keep and time.time()-path.stat().st_mtime > 86400:
                path.unlink(missing_ok=True)


def region(identifier):
    record = next((r for r in models()['regions'] if r['id'] == identifier), None)
    if record is None:
        raise ValueError('该地形区域不存在或已合并，请刷新区域列表')
    return record


def model_grid(identifier):
    record = region(identifier)
    with np.load(root()/record['artifact'], allow_pickle=False) as arrays:
        result = {**record}
        for name in ('height', 'support', 'scatter'):
            result[name] = [round(float(v), 2) if np.isfinite(v) else None for v in arrays[name].ravel()]
        points = arrays['observations']
        stride = max(1, int(np.ceil(len(points)/5000)))
        shown = points[::stride].copy()
        shown[:, :2] = np.round(shown[:, :2], 6); shown[:, 2] = np.round(shown[:, 2], 2)
        result['observations'] = shown.tolist()
    result['context'] = geographic_context(record)
    return result


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
