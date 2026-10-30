"""Heights that phones sample during a published course: a temporary terrain.

When the course's teacher allows it, phones with location on send anonymous
height samples: no team, name or group, only a random identifier per phone
and course. Each phone is one recording with its own slowly drifting error,
solved together with the shared walks around the course. The result is
temporary: routes never use it, it is removed two days after the last
sample, and it joins the shared terrain only when the teacher saves it under
their own account.
"""
from contextlib import contextmanager
import hashlib
from itertools import groupby
import json
import re
import shutil
import sqlite3
import threading
import time

import numpy as np

from . import elevation, store
from .elevation_model import reconstruct, spatial_groups
from .geometry import unmercator

RETAIN_SEC = 48*3600
MAX_BATCH = 600
MAX_PER_DEVICE = 30_000
MAX_PER_COURSE = 300_000
MAX_DEVICES = 120
MIN_REBUILD_SEC = 20
MIN_DEVICE_SAMPLES = 10
# Saving to the shared terrain needs a walk, not a phone left on a desk.
ADOPT_SAMPLES, ADOPT_SPAN_SEC = 30, 120
MAX_HORIZONTAL_M, MAX_VERTICAL_M = 25., 30.
CLOCK_TOLERANCE_SEC = 12*3600
MARGIN_M = 500.
DEVICE = re.compile(r'[A-Za-z0-9_-]{16,64}')
ARTIFACT = re.compile(r'[0-9a-f]{40}')
_schema = set()
_schema_lock = threading.Lock()
_builders = {}
_builders_lock = threading.Lock()


@contextmanager
def database():
    # Where phones walked: readable by the server's user only.
    path = store.private_database(store.DATA/'live-elevation.sqlite3')
    db = sqlite3.connect(path, timeout=10, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        with _schema_lock:
            if str(path) not in _schema:
                db.execute('PRAGMA journal_mode=WAL')
                db.executescript('''
                    CREATE TABLE IF NOT EXISTS courses (
                        release TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0,
                        revision INTEGER NOT NULL DEFAULT 0, sample_at REAL,
                        built_revision INTEGER, built_at REAL, models TEXT,
                        adopted_at REAL, adopted_revision INTEGER);
                    CREATE TABLE IF NOT EXISTS samples (
                        release TEXT NOT NULL, device TEXT NOT NULL, t REAL NOT NULL,
                        lng REAL NOT NULL, lat REAL NOT NULL, h REAL NOT NULL,
                        hacc REAL NOT NULL, vacc REAL, received REAL NOT NULL,
                        PRIMARY KEY(release, device, t)) WITHOUT ROWID;''')
                _schema.add(str(path))
        yield db
    finally:
        db.close()


def live_dir(release_id):
    return store.DATA/'releases'/release_id/'live'


def artifact_path(release_id, name):
    if not ARTIFACT.fullmatch(name or ''):
        raise ValueError('临时地形标识无效')
    return live_dir(release_id)/(name+'.npz')


def area(release):
    """The course's map extent in degrees, widened so edges stay covered."""
    west, south, east, north = release['extent']
    (lng0, lat0), (lng1, lat1) = unmercator([[west, south], [east, north]])
    pad_lng = MARGIN_M/(111320*np.cos(np.radians((lat0+lat1)/2)))
    pad_lat = MARGIN_M/110540
    return float(lng0-pad_lng), float(lat0-pad_lat), float(lng1+pad_lng), float(lat1+pad_lat)


def set_enabled(release_id, enabled):
    with database() as db:
        db.execute('INSERT OR IGNORE INTO courses(release) VALUES (?)', (release_id,))
        db.execute('UPDATE courses SET enabled=? WHERE release=?', (int(bool(enabled)), release_id))


def enabled(release_id):
    with database() as db:
        row = db.execute('SELECT enabled FROM courses WHERE release=?', (release_id,)).fetchone()
    return bool(row and row['enabled'])


def record(release, device, samples, now=None):
    """Store one phone's batch; a disabled course tells the phone to stop."""
    now = time.time() if now is None else now
    if not DEVICE.fullmatch(device or ''):
        raise ValueError('设备标识无效')
    if len(samples) > MAX_BATCH:
        raise ValueError(f'每次最多上传 {MAX_BATCH} 个高度样本')
    west, south, east, north = area(release)
    rows = []
    for sample in samples:
        moment = float(sample['t'])/1000
        vertical = sample.get('vacc')
        values = [moment, sample['lng'], sample['lat'], sample['h'], sample['hacc'], *([] if vertical is None else [vertical])]
        if not np.isfinite(values).all() or abs(moment-now) > CLOCK_TOLERANCE_SEC or moment > now+120:
            continue
        if not (west <= sample['lng'] <= east and south <= sample['lat'] <= north and -100 <= sample['h'] <= 3000):
            continue
        if not 0 <= sample['hacc'] <= MAX_HORIZONTAL_M or (vertical is not None and not 0 <= vertical <= MAX_VERTICAL_M):
            continue
        rows.append((release['id'], device, round(moment, 3), float(sample['lng']), float(sample['lat']), float(sample['h']),
                     float(sample['hacc']), None if vertical is None else float(vertical), now))
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            course = db.execute('SELECT enabled FROM courses WHERE release=?', (release['id'],)).fetchone()
            if not course or not course['enabled']:
                db.execute('ROLLBACK')
                return {'enabled': False, 'accepted': 0}
            own = db.execute('SELECT COUNT(*) FROM samples WHERE release=? AND device=?', (release['id'], device)).fetchone()[0]
            total = db.execute('SELECT COUNT(*) FROM samples WHERE release=?', (release['id'],)).fetchone()[0]
            if not own and db.execute('SELECT COUNT(DISTINCT device) FROM samples WHERE release=?', (release['id'],)).fetchone()[0] >= MAX_DEVICES:
                db.execute('ROLLBACK')
                return {'enabled': True, 'accepted': 0, 'full': True}
            rows = rows[:max(0, min(MAX_PER_DEVICE-own, MAX_PER_COURSE-total))]
            before = db.total_changes
            db.executemany('INSERT OR IGNORE INTO samples VALUES (?,?,?,?,?,?,?,?,?)', rows)
            accepted = db.total_changes-before
            if accepted:
                db.execute('UPDATE courses SET revision=revision+1, sample_at=? WHERE release=?', (now, release['id']))
            db.execute('COMMIT')
        except BaseException:
            db.execute('ROLLBACK')
            raise
    return {'enabled': True, 'accepted': accepted, 'full': own+accepted >= MAX_PER_DEVICE}


def status(release_id):
    with database() as db:
        course = db.execute('SELECT * FROM courses WHERE release=?', (release_id,)).fetchone()
        count, devices = db.execute('SELECT COUNT(*), COUNT(DISTINCT device) FROM samples WHERE release=?', (release_id,)).fetchone()
    if course is None:
        return {'enabled': False, 'samples': 0, 'devices': 0, 'lastSampleAt': None, 'builtAt': None, 'models': [],
                'stale': False, 'adoptedAt': None, 'adoptedCurrent': False, 'expiresAt': None}
    return {'enabled': bool(course['enabled']), 'samples': count, 'devices': devices, 'lastSampleAt': course['sample_at'],
            'builtAt': course['built_at'], 'models': json.loads(course['models'] or '[]'),
            'stale': bool(count) and course['built_revision'] != course['revision'],
            'adoptedAt': course['adopted_at'], 'adoptedCurrent': course['adopted_revision'] == course['revision'],
            'expiresAt': course['sample_at']+RETAIN_SEC if course['sample_at'] else None}


def summary(release_id):
    value = status(release_id)
    value.pop('models')
    return value


def current(release, now=None):
    """The temporary terrain, rebuilt here at most every few seconds.

    Reading drives the update: there is no background work while nobody looks.
    A reader arriving during a rebuild gets the previous surface.
    """
    now = time.time() if now is None else now
    value = status(release['id'])
    if value['stale'] and (value['builtAt'] is None or now-value['builtAt'] >= MIN_REBUILD_SEC):
        with _builders_lock:
            lock = _builders.setdefault(release['id'], threading.Lock())
        if lock.acquire(blocking=False):
            try:
                rebuild(release, now)
            finally:
                lock.release()
            value = status(release['id'])
        else:
            value['updating'] = True
    return value


def recordings(release_id, minimum=MIN_DEVICE_SAMPLES):
    """Each phone's samples in time order: (device, [lng, lat, h, seconds])."""
    with database() as db:
        rows = db.execute('SELECT device, t, lng, lat, h FROM samples WHERE release=? ORDER BY device, t', (release_id,)).fetchall()
    result = []
    for device, group in groupby(rows, key=lambda row: row['device']):
        values = np.array([(r['lng'], r['lat'], r['h'], r['t']) for r in group], float)
        if len(values) >= minimum:
            result.append((device, values))
    return result


def shared_tracks(bounds):
    west, south, east, north = bounds
    tracks = []
    with store.LOCK:
        for record in elevation.catalogue()['tracks']:
            low_lng, low_lat, high_lng, high_lat = record['bbox']
            if not record['enabled'] or high_lng < west or low_lng > east or high_lat < south or low_lat > north:
                continue
            with np.load(elevation.root()/'tracks'/(record['id']+'.npz'), allow_pickle=False) as data:
                tracks.append({'id': record['id'], 'points': data['points'],
                               'times': data['times'] if 'times' in data.files else None})
    return tracks


def rebuild(release, now=None):
    now = time.time() if now is None else now
    release_id = release['id']
    with database() as db:
        revision = db.execute('SELECT revision FROM courses WHERE release=?', (release_id,)).fetchone()['revision']
    live = [{'id': 'live-'+hashlib.sha256((release_id+device).encode()).hexdigest()[:16],
             'points': values[:, :3], 'times': values[:, 3]} for device, values in recordings(release_id)]
    models = []
    if live:
        directory = live_dir(release_id)
        directory.mkdir(parents=True, exist_ok=True)
        # The shared walks around the course are the starting surface.
        for group in spatial_groups(live+shared_tracks(area(release))):
            phones = [t for t in group if t['id'].startswith('live-')]
            if not phones:
                continue
            metadata, arrays = reconstruct(group)
            digest = hashlib.sha256()
            for key in sorted(arrays):
                digest.update(key.encode()); digest.update(np.ascontiguousarray(arrays[key]).tobytes())
            name = digest.hexdigest()[:40]
            if not (directory/(name+'.npz')).exists():
                elevation.write_npz(directory/(name+'.npz'), arrays)
            models.append({'id': name[:16], 'name': '本课实时地形', 'artifact': name, 'frame': metadata['frame'],
                           'origin': metadata['origin'], 'spacingM': metadata['spacingM'], 'columns': metadata['columns'],
                           'rows': metadata['rows'], 'bbox': metadata['bbox'], 'supportedCells': metadata['supportedCells'],
                           'minHeightM': metadata['minHeightM'], 'maxHeightM': metadata['maxHeightM'],
                           'livePhones': len(phones), 'liveSamples': int(sum(len(t['points']) for t in phones)),
                           'sharedTracks': len(group)-len(phones),
                           'alignment': {key: value for key, value in metadata['alignment'].items() if key != 'tracks'}})
    with database() as db:
        db.execute('UPDATE courses SET models=?, built_revision=?, built_at=? WHERE release=?',
                   (json.dumps(models, ensure_ascii=False), revision, now, release_id))
    # A phone may still be loading the previous image: keep one older file.
    keep = {m['artifact'] for m in models}
    if live_dir(release_id).is_dir():
        files = sorted(live_dir(release_id).glob('*.npz'), key=lambda p: p.stat().st_mtime, reverse=True)
        keep.update(path.stem for path in files[:len(keep)+1])
        for path in files:
            if path.stem not in keep:
                path.unlink(missing_ok=True)
    return models


def adopt(release, owner, now=None):
    """Save each phone's walk as a shared track owned by the course's teacher."""
    now = time.time() if now is None else now
    parsed = []
    for device, values in recordings(release['id'], ADOPT_SAMPLES):
        if values[-1, 3]-values[0, 3] < ADOPT_SPAN_SEC:
            continue
        number = len(parsed)+1
        parsed.append(elevation.track_record(values[:, :3], [0], values[:, 3], f"{release['title']} · 实时采样 {number}",
                                             f"live-{release['id'][:8]}-{number}.json",
                                             hashlib.sha256(values.tobytes()).hexdigest(), ['course-live-geolocation']))
    if not parsed:
        raise ValueError(f'还没有可以保存的实时样本：每部手机至少需要 {ADOPT_SAMPLES} 个、持续 {ADOPT_SPAN_SEC // 60} 分钟')
    receipt = elevation.store_tracks(parsed, owner)
    with database() as db:
        db.execute('UPDATE courses SET adopted_at=?, adopted_revision=revision WHERE release=?', (now, release['id']))
    return receipt


def clear(release_id):
    with database() as db:
        db.execute('DELETE FROM samples WHERE release=?', (release_id,))
        db.execute('UPDATE courses SET models=NULL, built_revision=NULL, built_at=NULL, sample_at=NULL, '
                   'revision=revision+1, adopted_at=NULL, adopted_revision=NULL WHERE release=?', (release_id,))
    shutil.rmtree(live_dir(release_id), ignore_errors=True)


def prune(now=None):
    """Samples and temporary surfaces go two days after a course's last sample."""
    now = time.time() if now is None else now
    with database() as db:
        expired = [row['release'] for row in db.execute(
            'SELECT release FROM courses WHERE sample_at IS NOT NULL AND sample_at < ?', (now-RETAIN_SEC,))]
    for release_id in expired:
        clear(release_id)
    return expired
