"""Versioned public starter maps, never taken from an account's workbench."""
import base64
import copy
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
import zipfile

from . import store
from .protocol import TERRAIN_SCHEMA

BUNDLE = Path(__file__).with_name('public-samples.json')
MAX_BUNDLE_BYTES = 96 * 1024**2
MAX_EXPANDED_BYTES = 160 * 1024**2
_fingerprint = None


def state_hash(state):
    value = {key: item for key, item in state.items() if key != 'seededStateHash'}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def untouched(state):
    return (state.get('seededFrom') == 'public-samples'
            and state.get('seededStateHash') == state_hash(state))


def write_bundle(data, target):
    """Package the fresh results of tools/build_samples.py for deployment."""
    files = {}
    for asset in store.builtin_maps():
        directory = Path(data)/'projects'/asset['id']
        state = store.read_json(directory/'state.json')
        if not state or not state.get('registration') or state.get('sourceHash') != asset['sha256'] or state.get('terrainSchema') != TERRAIN_SCHEMA:
            raise ValueError('Public starter maps must be processed from the current original images')
        if (state.get('reviewed') or state.get('review') or state.get('edits') or state.get('surfaces') or state.get('portals')
                or any(c.get('manualFields') or c['id'].startswith('manual-') or c.get('labelSource') == 'manual'
                       or c.get('state') in ('accepted', 'rejected') for c in state['controls'])
                or str((state.get('quality') or {}).get('method', '')).startswith('manual')):
            raise ValueError('Public starter maps cannot contain teacher edits or reviews')
        state = copy.deepcopy(state)
        artifacts = state.get('artifacts') or {name: name for name in store.ARTIFACT_NAMES if (directory/name).is_file()}
        if not {'terrain.npz', 'overlay.png'}.issubset(artifacts):
            raise ValueError('Public starter map artifacts are incomplete')
        state['artifacts'] = {}
        for name, relative in sorted(artifacts.items()):
            if name not in store.ARTIFACT_NAMES or (directory/relative).resolve().is_relative_to(directory.resolve()) is False:
                raise ValueError('Unexpected public starter artifact')
            files[f'projects/{asset["id"]}/{name}'] = (directory/relative).read_bytes()
            state['artifacts'][name] = name
        for key in ('lastOperationId', 'review', 'updatedAt', 'createdAt', 'processingElapsedSec', 'terrainProcessingElapsedSec'):
            state.pop(key, None)
        state.update(name=asset['name'], version=1, reviewed=False, graphHash=None)
        files[f'projects/{asset["id"]}/state.json'] = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    if sum(map(len, files.values())) > MAX_EXPANDED_BYTES:
        raise ValueError('Public starter maps exceed their storage budget')
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, content in sorted(files.items()):
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)
    document = {'schema': 1, 'terrainSchema': TERRAIN_SCHEMA,
                'files': {name: hashlib.sha256(content).hexdigest() for name, content in sorted(files.items())},
                'zipBase64': base64.b64encode(output.getvalue()).decode('ascii')}
    encoded = json.dumps(document, ensure_ascii=False, separators=(',', ':')).encode()
    if len(encoded) > MAX_BUNDLE_BYTES:
        raise ValueError('Public starter bundle exceeds its release budget')
    target = Path(target); target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix('.tmp')
    try:
        temporary.write_bytes(encoded)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return {'maps': len(store.builtin_maps()), 'files': len(files), 'bytes': len(encoded),
            'expandedBytes': sum(map(len, files.values())), 'sha256': hashlib.sha256(encoded).hexdigest()}


def _key():
    global _fingerprint
    stat = BUNDLE.stat()
    if stat.st_size > MAX_BUNDLE_BYTES:
        raise ValueError('Public starter bundle exceeds its storage budget')
    signature = (str(BUNDLE.resolve()), stat.st_size, stat.st_mtime_ns)
    if _fingerprint is None or _fingerprint[0] != signature:
        _fingerprint = signature, store.file_hash(BUNDLE)
    return _fingerprint[1]


def _extract(target):
    document = store.read_json(BUNDLE)
    if document.get('schema') != 1 or document.get('terrainSchema') != TERRAIN_SCHEMA:
        raise ValueError('Public starter maps need to be rebuilt for this application')
    assets = {asset['id']: asset for asset in store.builtin_maps()}
    expected = document['files']
    data = base64.b64decode(document['zipBase64'], validate=True)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        if len(set(names)) != len(names) or set(names) != set(expected) or sum(entry.file_size for entry in entries) > MAX_EXPANDED_BYTES:
            raise ValueError('Invalid public starter file inventory')
        for entry in entries:
            parts = PurePosixPath(entry.filename).parts
            if (len(parts) != 3 or parts[0] != 'projects' or parts[1] not in assets
                    or parts[2] not in {'state.json', *store.ARTIFACT_NAMES} or entry.is_dir()):
                raise ValueError('Invalid public starter path')
            path = target/entry.filename
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            digest = hashlib.sha256()
            with archive.open(entry) as source, path.open('xb') as destination:
                while chunk := source.read(256*1024):
                    digest.update(chunk); destination.write(chunk)
            path.chmod(0o600)
            if digest.hexdigest() != expected[entry.filename]:
                raise ValueError('Public starter checksum mismatch')
    for identifier, asset in assets.items():
        state = store.read_json(target/'projects'/identifier/'state.json')
        if (not state or state.get('sourceHash') != asset['sha256'] or state.get('terrainSchema') != TERRAIN_SCHEMA
                or state.get('assetId') != identifier or state.get('reviewed')):
            raise ValueError('Public starter state does not match its source')
        if not {'terrain.npz', 'overlay.png'}.issubset(state.get('artifacts', {})):
            raise ValueError('Public starter state lacks required artifacts')
        if any(name not in store.ARTIFACT_NAMES or name != value or f'projects/{identifier}/{name}' not in expected
               for name, value in state['artifacts'].items()):
            raise ValueError('Public starter state has an invalid artifact reference')


def source_root():
    """Materialize once. Callers also hold store.LOCK while hard-linking files."""
    if not BUNDLE.is_file():
        return None
    with store.LOCK:
        key = _key()
        cache = store.DATA/'shared-samples'; cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = cache/key
        ready = {'sha256': key, 'terrainSchema': TERRAIN_SCHEMA,
                 'sources': {asset['id']: asset['sha256'] for asset in store.builtin_maps()}}
        if store.read_json(target/'ready.json') != ready:
            with tempfile.TemporaryDirectory(prefix='.building-', dir=cache) as temporary:
                staging = Path(temporary)/'snapshot'; staging.mkdir(mode=0o700)
                _extract(staging)
                store.atomic_json(staging/'ready.json', ready)
                if target.exists():
                    shutil.rmtree(target)
                staging.replace(target)
        # Accounts retain their own hard links, even when an old shared
        # snapshot is removed. Keep two decoded generations on a small disk.
        older = sorted((p for p in cache.iterdir() if p.is_dir() and p != target and re.fullmatch('[0-9a-f]{64}', p.name)),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        for obsolete in older[1:]:
            shutil.rmtree(obsolete)
        return target
