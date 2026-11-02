"""Printed contours supply equal-height constraints, with optional known levels."""
import hashlib
import math

import cv2
import numpy as np

from .geometry import Frame, Registration
from .network_registration import thin, sample_line, map_footprint

AUTO_SCHEMA = 'printed-equal-height-v1'


def trace_lines(skeleton, minimum_length=0.):
    ys, xs = np.nonzero(skeleton)
    if not len(xs):
        return []
    index = np.full(skeleton.shape, -1, np.int32)
    index[ys, xs] = np.arange(len(xs))
    neighbours = [[] for _ in xs]
    for dx, dy in ((1, 0), (0, 1), (1, 1), (-1, 1)):
        valid = (xs+dx >= 0) & (xs+dx < skeleton.shape[1]) & (ys+dy < skeleton.shape[0])
        own = np.flatnonzero(valid)
        other = index[ys[own]+dy, xs[own]+dx]
        take = other >= 0
        if dx and dy:
            # Diagonals through a filled 2x2 corner make fake branch nodes.
            take &= (index[ys[own], xs[own]+dx] < 0) & (index[ys[own]+dy, xs[own]] < 0)
        for a, b in zip(own[take].tolist(), other[take].tolist()):
            neighbours[a].append(b); neighbours[b].append(a)
    visited, result = set(), []
    count = len(xs)
    key = lambda a, b: min(a, b)*count+max(a, b)
    starts = [i for i, ns in enumerate(neighbours) if len(ns) != 2]
    starts.extend(i for i, ns in enumerate(neighbours) if len(ns) == 2)
    for start in starts:
        for following in neighbours[start]:
            if key(start, following) in visited:
                continue
            path = [start]; previous, current = start, following
            visited.add(key(previous, current))
            while True:
                path.append(current)
                if current == start or len(neighbours[current]) != 2:
                    break
                next_node = next(i for i in neighbours[current] if i != previous)
                if key(current, next_node) in visited:
                    break
                visited.add(key(current, next_node))
                previous, current = current, next_node
            if (len(path)-1)*math.sqrt(2) < minimum_length:
                continue
            result.append(np.column_stack((xs[path], ys[path])).astype(float))
    return result


def extract(image, controls=()):
    height, width = image.shape[:2]
    radii = [c['radiusPx'] for c in controls if c.get('role', 'control') == 'control']
    radius = float(np.median(radii)) if radii else max(width, height)/90
    h, s, v = cv2.split(cv2.cvtColor(image, cv2.COLOR_BGR2HSV))
    brown = ((h >= 4) & (h <= 25) & (s >= 75) & (v >= 75) & (v <= 238)).astype(np.uint8)
    # Closing repairs small print gaps; it cannot bridge a course circle or
    # assume that two interrupted contours have the same elevation.
    brown = cv2.morphologyEx(brown, cv2.MORPH_CLOSE, np.ones((2, 2), np.uint8))
    thickness = cv2.distanceTransform(brown, cv2.DIST_L2, cv2.DIST_MASK_PRECISE)
    skeleton = thin(brown)
    minimum_length = max(40., radius*2.8)
    # Text and fill texture can leave tens of thousands of tiny skeletons.
    # Even an all-diagonal path has length at most sqrt(2) per pixel. Remove
    # components that cannot possibly reach the existing length threshold
    # before building the Python graph; this does not remove a valid curve.
    _, components, stats, _ = cv2.connectedComponentsWithStats(skeleton, 8)
    keep = stats[:, cv2.CC_STAT_AREA]*math.sqrt(2) >= minimum_length
    keep[0] = False
    skeleton = keep[components].astype(np.uint8)
    del components, stats
    footprint = map_footprint(image)
    curves = []
    for path in trace_lines(skeleton, minimum_length):
        length = float(np.linalg.norm(np.diff(path, axis=0), axis=1).sum())
        if length < minimum_length or np.ptp(path, axis=0).max() < radius*1.1:
            continue
        if footprint is not None:
            samples = path[::max(1, len(path)//40)]
            if np.mean([cv2.pointPolygonTest(footprint.astype(np.float32), tuple(p), True) >= -radius*.5 for p in samples]) < .9:
                continue
        widths = thickness[path[:, 1].astype(int), path[:, 0].astype(int)]*2
        if np.percentile(widths, 90) > max(3.5, radius*.25):
            continue  # brown area fills, not contour strokes
        closed = bool(np.array_equal(path[0], path[-1]))
        pixels = cv2.approxPolyDP(path.astype(np.float32), max(.7, radius*.025), closed).reshape(-1, 2)
        if closed and not np.array_equal(pixels[0], pixels[-1]):
            pixels = np.vstack((pixels, pixels[0]))
        if len(pixels) < 3:
            continue
        if closed:
            rect = cv2.minAreaRect(pixels)
            if cv2.contourArea(pixels)/max(1, rect[1][0]*rect[1][1]) > .93:
                continue  # court markings and page frames
        identifier = 'contour-'+hashlib.sha256(np.round(pixels, 1).astype('<f4').tobytes()).hexdigest()[:12]
        curves.append({'id': identifier, 'pixels': pixels.tolist(), 'closed': closed,
                       'lengthPx': round(length, 1), 'heightM': None, 'confidence': .7})
    curves.sort(key=lambda curve: -curve['lengthPx'])
    return curves[:300]


def geographic_curve(curve, state, reg=None):
    reg = reg or Registration.from_dict(state['registration'])
    scale = math.sqrt(abs(np.linalg.det(reg.affine[:, :2])))
    pixels = sample_line(curve['pixels'], max(1., 5/scale))
    local, valid = reg.forward(pixels)
    return Frame.of(state).to_lnglat(local), valid


def report(state):
    from . import elevation, service
    curves = []
    automatic = state.get('automaticContours')
    record = next((t for t in elevation.catalogue()['tracks'] if automatic and t['id'] == automatic.get('trackId')), None)
    applied = set((record or {}).get('contourIds', [])) if (record or {}).get('enabled') else set()
    reg = Registration.from_dict(state['registration']) if state.get('registration') else None
    sampler = elevation.Sampler(elevation.relevant(state)) if state.get('registration') else None
    for curve in state.get('contours', []):
        item = dict(curve, heightM=state.get('contourHeights', {}).get(curve['id']),
                    calibrated=False, suggestedHeightM=None, suggestionSpreadM=None,
                    referenceCoverage=0., referenceSamples=0, automaticallyUsed=curve['id'] in applied)
        if state.get('registration'):
            points, valid = geographic_curve(curve, state, reg)
            item['calibrated'] = bool(valid.all())
            if valid.all() and sampler:
                heights = sampler.sample(points)
                known = heights[np.isfinite(heights)]
                item.update(referenceCoverage=round(len(known)/max(1, len(heights)), 3), referenceSamples=len(known))
                if len(known) >= max(3, len(heights)*.7):
                    item.update(suggestedHeightM=round(float(np.median(known)), 1),
                                suggestionSpreadM=round(float(np.percentile(known, 90)-np.percentile(known, 10)), 1))
        curves.append(item)
    return {'assetId': state['assetId'], 'version': state['version'], 'revision': state.get('contourRevision', 0),
            'contours': curves, 'intervalM': state.get('contourIntervalM'),
            'heightReference': state.get('contourHeightReference', 'reference' if state.get('contourHeights') else 'relative'),
            'contributionStale': bool(state.get('contourRegistrationStamp') and state['contourRegistrationStamp'] != service.registration_stamp(state)),
            'automatic': {**automatic, 'enabled': bool((record or {}).get('enabled'))} if automatic else None,
            'heightSource': 'automatic-equal-height-with-optional-levels'}


def sync_automatic(state, owner):
    """Idempotently replace this sheet's automatic constraints after recognition.

    Only the registered portions participate. No guessed altitude, interval,
    or uphill direction is stored. The solver estimates a separate level for
    every continuous line, and waits for overlap if no height datum exists.
    """
    from . import elevation, service, store
    if not state.get('registration') or service.registration_stale(state):
        return None
    reg = Registration.from_dict(state['registration'])
    points, breaks, used = [], [], []
    marked = state.get('contourHeights', {})
    for curve in sorted(state.get('contours', []), key=lambda c: c['id']):
        if curve['id'] in marked:
            continue
        coordinates, valid = geographic_curve(curve, state, reg)
        edges = np.flatnonzero(np.diff(np.r_[False, valid, False]))
        pieces = [coordinates[a:b] for a, b in zip(edges[::2], edges[1::2]) if b-a >= 3]
        if pieces:
            used.append(curve['id'])
        for piece in pieces:
            breaks.append(len(points))
            points.extend(np.column_stack((piece, np.zeros(len(piece)))).tolist())
    source_key = f'{store.current_workspace()}:{state["assetId"]}'
    with store.LOCK:
        receipt, identifier = {'version': elevation.catalogue()['version'], 'added': [], 'duplicates': []}, None
        if points:
            summary, arrays = elevation.track_record(np.asarray(points), breaks, np.full(len(points), np.nan),
                state['name']+' · 自动等高线', 'map-contours', state['sourceHash'], ['equal-height'])
            summary.update(id=hashlib.sha256(('auto-contours-v1:'+summary['id']).encode()).hexdigest(),
                           kind='contours', heightReference='shape', contourCount=len(used),
                           automatic=True, sourceKey=source_key, sourceKeys=[source_key], contourIds=used)
            identifier = summary['id']
            receipt = elevation.store_tracks([(summary, arrays)], owner)
        source = elevation.catalogue()
        changed, obsolete = False, []
        for record in source['tracks']:
            if not record.get('automatic'):
                continue
            keys = set(record.get('sourceKeys', [record.get('sourceKey')])) - {None}
            updated = keys | {source_key} if record['id'] == identifier else keys - {source_key}
            if updated != keys:
                record['sourceKeys'] = sorted(updated); changed = True
            if not updated:
                obsolete.append(record['id']); changed = True
        if changed:
            source['tracks'] = [t for t in source['tracks'] if t['id'] not in obsolete]
            source['version'] += 1
            store.atomic_json(elevation.root()/'tracks.json', source)
            for old_id in obsolete:
                (elevation.root()/'tracks'/(old_id+'.npz')).unlink(missing_ok=True)
            receipt['version'] = source['version']
        state['automaticContours'] = {'schema': AUTO_SCHEMA, 'trackId': identifier, 'used': len(used),
            'pending': max(0, len(state.get('contours', []))-len(used)-len(marked)),
            'registrationStamp': service.registration_stamp(state)}
    return receipt


def remove_automatic_source(asset_id):
    """Deleting a map removes only its automatic, source-derived contribution."""
    from . import elevation, store
    key = f'{store.current_workspace()}:{asset_id}'
    with store.LOCK:
        source = elevation.catalogue(); removed, changed = [], False
        for record in source['tracks']:
            if not record.get('automatic'):
                continue
            keys = set(record.get('sourceKeys', [record.get('sourceKey')])) - {None}
            if key not in keys and keys:
                continue
            keys.discard(key); changed = True
            record['sourceKeys'] = sorted(keys)
            if not keys:
                removed.append(record['id'])
        if changed:
            source['tracks'] = [t for t in source['tracks'] if t['id'] not in removed]
            source['version'] += 1
            store.atomic_json(elevation.root()/'tracks.json', source)
            for identifier in removed:
                (elevation.root()/'tracks'/(identifier+'.npz')).unlink(missing_ok=True)
    return changed


def contribute(state, selected, owner, height_reference='reference'):
    from . import elevation
    if not state.get('registration'):
        raise ValueError('请先配准地图，再贡献等高线')
    if height_reference not in ('reference', 'relative'):
        raise ValueError('等高线高度基准无效')
    known = {curve['id']: curve for curve in state.get('contours', [])}
    reg = Registration.from_dict(state['registration'])
    if len({item['id'] for item in selected}) != len(selected):
        raise ValueError('同一条等高线不能指定多个高度')
    observations, breaks = [], []
    for item in sorted(selected, key=lambda item: item['id']):
        curve = known.get(item['id'])
        if curve is None:
            raise ValueError('等高线已更新，请重新核对')
        points, valid = geographic_curve(curve, state, reg)
        if not valid.all():
            raise ValueError('所选等高线超出配准范围，请补充同名点')
        breaks.append(len(observations))
        observations.extend(np.column_stack((points, np.full(len(points), item['heightM']))).tolist())
    if not observations or len(observations) > 50_000:
        raise ValueError('请选择有效等高线；每次最多 50000 个高程采样点')
    points = np.asarray(observations)
    if height_reference == 'relative':
        # The chosen zero carries no measurement information. Equivalent
        # level differences must not be counted again under another zero.
        points[:, 2] -= points[:, 2].min()
    summary, arrays = elevation.track_record(points, breaks, np.full(len(points), np.nan),
        state['name']+' · 已核对等高线', 'map-contours', state['sourceHash'], ['teacher-confirmed'])
    prefix = 'contours-relative-v1:' if height_reference == 'relative' else 'contours-v1:'
    summary.update(id=hashlib.sha256((prefix+summary['id']).encode()).hexdigest(), kind='contours',
                   heightReference=height_reference, contourCount=len(selected))
    from . import store
    # This is the teacher's current selection for this sheet. Updating it
    # supersedes only this contributor's earlier contour set for the same
    # image, so revisions cannot contradict each other or gain extra weight.
    with store.LOCK:
        existing = next((t for t in elevation.catalogue()['tracks'] if t['id'] == summary['id']), None)
        if existing and not existing['enabled'] and (not owner or existing['ownerId'] != owner.get('id')):
            raise elevation.NotOwner('这组等高线由另一位贡献者管理，且已被停用')
        receipt = elevation.store_tracks([(summary, arrays)], owner)
        source = elevation.catalogue()
        changed = False
        for record in source['tracks']:
            if record['id'] == summary['id'] and not record['enabled'] and record['ownerId'] == owner['id']:
                record['enabled'] = True; changed = True
            if (record['id'] != summary['id'] and record.get('kind') == 'contours' and not record.get('automatic') and record['enabled']
                    and record['ownerId'] == owner['id'] and record['sourceSha256'] == state['sourceHash']):
                record['enabled'] = False; changed = True
        if changed:
            source['version'] += 1
            store.atomic_json(elevation.root()/'tracks.json', source)
            receipt['version'] = source['version']
    return receipt
