"""Atomic manifests, immutable derived files, and cross-process edit locking."""

from contextlib import contextmanager
import contextvars
import hashlib
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get('DIRMAP_DATA_DIR', str(ROOT/'data')))
# '' is the original single-teacher workbench at the data root; each account
# works in its own directory under workspaces/. Requests and queued jobs set
# the workbench they act on, so map IDs such as szu-1 never cross accounts.
LEGACY = ''
CURRENT = contextvars.ContextVar('dirmap_workspace', default=LEGACY)


def valid_workspace(workspace):
    return workspace == LEGACY or bool(re.fullmatch(r'[0-9a-f]{16}', workspace or ''))


def workspace_root(workspace=None):
    workspace = CURRENT.get() if workspace is None else workspace
    if workspace == LEGACY:
        return DATA
    if not valid_workspace(workspace):
        raise ValueError('工作区标识无效')
    return DATA/'workspaces'/workspace


def current_workspace():
    return CURRENT.get()


@contextmanager
def using(workspace):
    if not valid_workspace(workspace):
        raise ValueError('工作区标识无效')
    token = CURRENT.set(workspace)
    try:
        yield
    finally:
        CURRENT.reset(token)


def workspaces():
    """Every workbench on this server, the original one first."""
    found = [LEGACY]
    if (DATA/'workspaces').is_dir():
        found += sorted(path.name for path in (DATA/'workspaces').iterdir() if path.is_dir() and valid_workspace(path.name))
    return found

class DataLock:
    """Reentrant in a thread; serializes writers in other server processes.

    flock is held by a live file descriptor, not inferred from a stale PID or
    a timestamp. A crashed process releases it through the operating system.
    """
    def __init__(self):
        self.mutex = threading.RLock()
        self.local = threading.local()

    def __enter__(self):
        self.mutex.acquire()
        try:
            depth = getattr(self.local, 'depth', 0)
            if depth == 0:
                DATA.mkdir(parents=True, exist_ok=True)
                self.local.stream = (DATA/'.write.lock').open('a+b')
                try:
                    fcntl.flock(self.local.stream, fcntl.LOCK_EX)
                except BaseException:
                    self.local.stream.close()
                    raise
            self.local.depth = depth+1
            return self
        except BaseException:
            self.mutex.release()
            raise

    def __exit__(self, *exc):
        self.local.depth -= 1
        if self.local.depth == 0:
            self.local.stream.close()
        self.mutex.release()


LOCK = DataLock()


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name+f'.{uuid.uuid4().hex}.tmp')
    try:
        with temporary.open('w', encoding='utf-8') as stream:
            stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))+'\n')
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path, default=None):
    path = Path(path)
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(256*1024), b''):
            digest.update(block)
    return digest.hexdigest()


def asset_dir(asset_id):
    if not valid_project(asset_id):
        raise ValueError('地图标识无效')
    path = workspace_root()/'projects'/asset_id
    path.mkdir(parents=True, exist_ok=True)
    return path


# The three original sheets in the repository are of the Yuehai campus;
# uploads made before places existed were all of that campus as well.
DEFAULT_SITE = 'szu-yuehai'
_HASHES = {}


def builtin_maps():
    originals = []
    for index, path in enumerate(sorted(ROOT.glob('*.jpg'))):
        stat = path.stat()
        key = (str(path), stat.st_size, stat.st_mtime_ns)
        if key not in _HASHES:
            _HASHES[key] = file_hash(path)
        originals.append({'id': f'szu-{index+1}', 'path': str(path), 'name': f'深大定向图 {index+1}',
                          'filename': path.name, 'role': 'map', 'sha256': _HASHES[key], 'siteId': DEFAULT_SITE,
                          'builtin': True})
    return originals


def site_of(asset):
    return asset.get('siteId') or DEFAULT_SITE


def uploads_path():
    return workspace_root()/'uploads.json'


def catalog():
    originals = builtin_maps()
    # Uploaded originals always live inside the selected workbench. Older
    # manifests stored absolute paths: after a backup/clone is relocated,
    # those may still exist but belong to a different (possibly live) copy.
    # Resolve by asset ID instead of following the old machine's path.
    originals.extend({**asset, 'siteId': asset.get('siteId') or DEFAULT_SITE, 'path': str(asset_dir(asset['id'])/'source.img')}
                     for asset in read_json(uploads_path(), []))
    return originals


def get_asset(asset_id):
    for asset in catalog():
        if asset['id'] == asset_id:
            return asset
    raise ValueError('地图不存在')


def course_defaults(source_hash):
    # Defaults describe this source sheet and explicit teacher rules. Other
    # uploaded maps remain freely configurable and inherit no SZU point IDs.
    return read_json(ROOT/'data/course-presets.json', {}).get(source_hash)


def valid_project(asset_id):
    return bool(asset_id) and all(c in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in asset_id)


def get_state(asset_id):
    if not valid_project(asset_id):
        raise ValueError('地图标识无效')
    # Looking up a map must not create a project directory for it.
    return read_json(workspace_root()/'projects'/asset_id/'state.json')


def save_state(asset_id, value, operation_id=None, result=None):
    """Commit the state and a replay receipt with one atomic manifest switch.

    A receipt alone is only staged; lastOperationId proves the state was
    committed. Ordinary edits retain that marker. The single live queue
    worker always acknowledges/reconciles this operation before the next.
    """
    with LOCK:
        current = get_state(asset_id)
        previous_operation = (current or {}).get('lastOperationId')
        if operation_id:
            value['lastOperationId'] = operation_id
            atomic_json(DATA/'jobs'/'results'/f'{operation_id}.json', result if result is not None else value)
        elif previous_operation:
            value['lastOperationId'] = previous_operation
        atomic_json(asset_dir(asset_id)/'state.json', value)


def operation_result(asset_id, operation_id):
    with LOCK:
        state = get_state(asset_id)
        if (state or {}).get('lastOperationId') == operation_id:
            return read_json(DATA/'jobs'/'results'/f'{operation_id}.json')
    return None


ARTIFACT_NAMES = {'terrain.npz', 'terrain.png', 'terrain-warp.png', 'blocked.png', 'overlay.png', 'working.jpg'}


def artifact_path(state, name):
    if name not in ARTIFACT_NAMES:
        raise ValueError('地图派生文件无效')
    if 'artifacts' in state and name not in state['artifacts']:
        raise FileNotFoundError(name)
    relative = Path(state.get('artifacts', {}).get(name, name))
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('地图派生文件路径无效')
    return asset_dir(state['assetId'])/relative


def stage_artifacts(state, directory, replace_all=False):
    """Stage a complete immutable generation; save_state publishes its paths.

    Existing manifests keep referring to their old files throughout the
    operation, including if the process dies between any two file writes.
    """
    root = asset_dir(state['assetId'])
    relative = Path('derived')/uuid.uuid4().hex
    destination = root/relative
    destination.mkdir(parents=True)
    manifest = {} if replace_all else dict(state.get('artifacts', {name: name for name in ARTIFACT_NAMES if (root/name).exists()}))
    for source in Path(directory).iterdir():
        if source.name not in ARTIFACT_NAMES:
            raise ValueError('处理结果包含未知文件')
        source.replace(destination/source.name)
        with (destination/source.name).open('rb') as stream:
            os.fsync(stream.fileno())
        manifest[source.name] = str(relative/source.name)
    sync_directory(destination)
    sync_directory(destination.parent)
    sync_directory(root)
    state['artifacts'] = manifest
    return state


def _newest(directory, pattern):
    return sorted(directory.glob(pattern), key=lambda path: path.stat().st_mtime, reverse=True)


def prune_history(keep_per_asset=20, keep_sec=30*86400, receipt_age_sec=86400, now=None):
    """Bound saved plans, comparisons and replay receipts.

    Published courses embed their plan and overlay, so they never read these
    files. Each map keeps its newest plans and comparisons regardless of age,
    plus every comparison that a kept plan refers to. A replay receipt
    matters only while it is some map's lastOperationId.
    """
    now = time.time() if now is None else now
    removed = []
    with LOCK:
        receipts = set()
        directories = sorted(path for workspace in workspaces()
                             for path in (workspace_root(workspace)/'projects').glob('*') if path.is_dir())
        for directory in directories:
            receipts.add((read_json(directory/'state.json') or {}).get('lastOperationId'))
            plans = _newest(directory, 'plan-*.json')
            stale = {path for path in plans[keep_per_asset:] if now-path.stat().st_mtime > keep_sec}
            used = {((read_json(path) or {}).get('teamRecommendation') or {}).get('comparisonId')
                    for path in plans if path not in stale}
            stale.update(path for path in _newest(directory, 'comparison-*.json')[keep_per_asset:]
                         if path.stem.removeprefix('comparison-') not in used and now-path.stat().st_mtime > keep_sec)
            for path in sorted(stale):
                path.unlink()
                removed.append(path)
        for path in (DATA/'jobs'/'results').glob('*.json'):
            if path.stem not in receipts and now-path.stat().st_mtime > receipt_age_sec:
                path.unlink()
                removed.append(path)
    return removed


def prune_artifacts(asset_id, minimum_age_sec=86400):
    """Bound old derived generations without touching sources or releases.

    Keep all currently referenced directories plus the two newest ones, and
    never delete a recent generation that an in-flight response could use.
    The caller is the exclusive queue worker, between operations.
    """
    with LOCK:
        state = get_state(asset_id)
        if not state:
            return
        root = asset_dir(asset_id)
        names = state.get('artifacts', {name: name for name in ARTIFACT_NAMES})
        live = {artifact_path(state, name).parent for name in names}
        generations = sorted((root/'derived').glob('*'), key=lambda p: p.stat().st_mtime, reverse=True)
        live.update(generations[:2])
        for path in generations:
            if (path.is_dir() and path not in live and len(path.name) == 32
                    and all(c in '0123456789abcdef' for c in path.name)
                    and time.time()-path.stat().st_mtime > minimum_age_sec):
                shutil.rmtree(path)
