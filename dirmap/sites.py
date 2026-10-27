"""Teaching places: each has its own OSM reference and local metre frame.

A map belongs to one place. The place's public OSM features anchor automatic
registration and draw the standard-map reference, and its frame origin keeps
local east/north metres accurate around it. Built-in places ship with the
application. An account can add another place by downloading the public OSM
features of a bounded area into its own workbench.
"""
import hashlib
import json
from pathlib import Path
import shutil
import time
import urllib.parse
import urllib.request
import uuid

from . import store
from .geometry import Frame
from .reference import load_reference

BUILTIN = Path(__file__).with_name('sites.json')
MIN_SIDE_M, MAX_SIDE_M = 150, 3000
MAX_CUSTOM = 8
MAX_DOWNLOAD_BYTES = 40*1024*1024
OVERPASS = ('https://overpass-api.de/api/interpreter', 'https://overpass.kumi.systems/api/interpreter',
            'https://overpass.private.coffee/api/interpreter')
# Tags read by registration, the layer suggestions and the map display.
KEEP_TAGS = frozenset(('building', 'natural', 'water', 'waterway', 'highway', 'leisure', 'landuse', 'name', 'bridge',
                       'tunnel', 'layer', 'level', 'covered', 'access', 'foot', 'area', 'width', 'smoothness', 'sport'))
PUBLIC_KEYS = ('id', 'name', 'shortName', 'origin', 'bbox', 'builtin', 'status', 'featureCount', 'retrievedAt', 'error')


def builtin():
    return [{**site, 'builtin': True, 'status': 'ready'} for site in json.loads(BUILTIN.read_text(encoding='utf-8'))['sites']]


def custom():
    return store.read_json(store.workspace_root()/'sites.json', [])


def available():
    return builtin()+custom()


def get(site_id):
    for site in available():
        if site['id'] == site_id:
            return site
    raise ValueError('场地不存在，请先添加场地')


def public(site):
    return {key: site[key] for key in PUBLIC_KEYS if key in site}


def frame(site):
    return Frame(float(site['origin'][0]), float(site['origin'][1]))


def reference_paths(site):
    if not site.get('builtin'):
        return [store.workspace_root()/'sites'/site['id']/'reference.json']
    paths = []
    for name in site['reference']:
        # A deployment keeps the campus reference in its data directory.
        local = store.DATA/name.removeprefix('data/') if name.startswith('data/') else None
        paths.append(local if local is not None and local.exists() else store.ROOT/name)
    return paths


def reference_hash(site):
    digest = hashlib.sha256()
    for path in reference_paths(site):
        digest.update(path.name.encode()+b'\0'+(store.file_hash(path).encode() if path.exists() else b'missing')+b'\0')
    return digest.hexdigest()


def reference(site_id, at_frame=None):
    """Reference features in local metres of `at_frame` (the place's own)."""
    site = get(site_id)
    paths = [path for path in reference_paths(site) if path.exists()]
    if site.get('status') != 'ready' or not paths:
        raise ValueError('场地参照尚未下载完成，请先下载参照或录入同名点')
    return load_reference(paths, at_frame or frame(site))


def extent_m(bbox):
    west, south, east, north = bbox
    width, height = Frame((west+east)/2, (south+north)/2).scales*[east-west, north-south]
    return float(width), float(height)


def add(name, bbox):
    name = ' '.join(name.split())
    west, south, east, north = (float(value) for value in bbox)
    if not name or len(name) > 40:
        raise ValueError('请填写 1–40 字的场地名称')
    if not (-180 <= west < east <= 180 and -85 <= south < north <= 85):
        raise ValueError('场地范围无效')
    width, height = extent_m((west, south, east, north))
    if max(width, height) > MAX_SIDE_M or min(width, height) < MIN_SIDE_M:
        raise ValueError(f'场地范围的边长应在 {MIN_SIDE_M} m 到 {MAX_SIDE_M/1000:g} km 之间（当前约 {width:.0f} × {height:.0f} m），请缩放地图后重试')
    with store.LOCK:
        sites = custom()
        if len(sites) >= MAX_CUSTOM:
            raise ValueError(f'每个工作台最多添加 {MAX_CUSTOM} 个场地')
        if any(site['name'] == name for site in builtin()+sites):
            raise ValueError('已有同名场地')
        site = {'id': 'site-'+uuid.uuid4().hex[:10], 'name': name, 'shortName': name[:12],
                'origin': [round((west+east)/2, 7), round((south+north)/2, 7)],
                'bbox': [round(west, 7), round(south, 7), round(east, 7), round(north, 7)],
                'builtin': False, 'status': 'pending', 'createdAt': time.time()}
        store.atomic_json(store.workspace_root()/'sites.json', [*sites, site])
    return site


def update(site_id, **changes):
    with store.LOCK:
        sites = custom()
        for site in sites:
            if site['id'] == site_id:
                site.update(changes)
                store.atomic_json(store.workspace_root()/'sites.json', sites)
                return site
    raise ValueError('场地不存在')


def remove(site_id):
    if any(site['id'] == site_id for site in builtin()):
        raise ValueError('内置场地不能删除')
    with store.LOCK:
        if any(store.site_of(asset) == site_id for asset in store.catalog()):
            raise ValueError('仍有地图属于这个场地，请先删除这些地图')
        sites = custom()
        kept = [site for site in sites if site['id'] != site_id]
        if len(kept) == len(sites):
            raise ValueError('场地不存在')
        store.atomic_json(store.workspace_root()/'sites.json', kept)
        shutil.rmtree(store.workspace_root()/'sites'/site_id, ignore_errors=True)


def overpass_query(bbox):
    west, south, east, north = bbox
    box = f'({south:.7f},{west:.7f},{north:.7f},{east:.7f})'
    parts = ('way["building"]', 'relation["building"]', 'way["natural"~"^(water|wood|scrub|grassland)$"]',
             'relation["natural"="water"]', 'way["waterway"]', 'way["highway"]',
             'way["leisure"~"^(pitch|track|stadium|swimming_pool|park|garden|playground)$"]', 'relation["leisure"="park"]',
             'way["landuse"~"^(grass|forest|meadow|recreation_ground|village_green)$"]')
    return '[out:json][timeout:90];('+''.join(part+box+';' for part in parts)+');out geom;'


def download(query, progress=lambda p, m: None, rounds=2):
    """Query public Overpass mirrors in turn; a busy mirror is common."""
    failures = []
    for attempt in range(rounds):
        for index, host in enumerate(OVERPASS):
            netloc = urllib.parse.urlsplit(host).netloc
            progress(10+(attempt*len(OVERPASS)+index)*10, f'下载 OpenStreetMap 参照（{netloc}）')
            try:
                request = urllib.request.Request(host, data=urllib.parse.urlencode({'data': query}).encode(),
                                                 headers={'User-Agent': 'DirMap/0.1 orienteering class reference'})
                with urllib.request.urlopen(request, timeout=120) as response:
                    body = response.read(MAX_DOWNLOAD_BYTES+1)
                if len(body) > MAX_DOWNLOAD_BYTES:
                    raise OverflowError
                data = json.loads(body)
                remark = str(data.get('remark') or '')
                # Overpass reports a timeout as a remark beside partial data.
                if 'elements' not in data or 'error' in remark.lower():
                    raise ValueError(remark or 'no elements')
                return data, host
            except OverflowError:
                raise ValueError('场地内的地物过多，请缩小场地范围后重试') from None
            except (OSError, ValueError) as exc:
                failures.append(f'{netloc}: {exc}')
        if attempt+1 < rounds:
            time.sleep(5)
    raise ValueError('OpenStreetMap 参照服务暂时不可用，请稍后重试（'+'；'.join(failures[-3:])[:300]+'）')


def compact(elements):
    """Only the tags and geometry DirMap reads; ODbL attribution is kept."""
    result = []
    for element in elements:
        tags = {key: value for key, value in element.get('tags', {}).items() if key in KEEP_TAGS}
        if element.get('type') == 'way' and element.get('geometry'):
            result.append({'type': 'way', 'id': element['id'], 'tags': tags,
                           'geometry': [{'lat': p['lat'], 'lon': p['lon']} for p in element['geometry'] if p]})
        elif element.get('type') == 'relation':
            members = [{'type': 'way', 'ref': member.get('ref'), 'role': member.get('role', ''),
                        'geometry': [{'lat': p['lat'], 'lon': p['lon']} for p in member['geometry'] if p]}
                       for member in element.get('members', []) if member.get('type') == 'way' and member.get('geometry')]
            if members:
                result.append({'type': 'relation', 'id': element['id'], 'tags': tags, 'members': members})
    return result


def save_reference(path, bbox, elements, source):
    store.atomic_json(path, {'elements': elements, 'source': {
        'url': source, 'bbox': list(bbox), 'retrievedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'crs': 'WGS84', 'license': 'ODbL 1.0', 'attribution': '© OpenStreetMap contributors'}})


def fetch_reference(site_id, progress=lambda p, m: None):
    site = get(site_id)
    if site.get('builtin'):
        raise ValueError('内置场地的参照随程序发布，无需下载')
    target = store.workspace_root()/'sites'/site_id/'reference.json'
    update(site_id, status='fetching', error=None)
    try:
        data, host = download(overpass_query(site['bbox']), progress)
        elements = compact(data['elements'])
        progress(80, '保存场地参照')
        save_reference(target, site['bbox'], elements, host)
        usable = len(load_reference(target, frame(site)))
    except Exception as exc:
        # A failed refresh keeps a previously downloaded reference usable.
        update(site_id, status='ready' if target.exists() else 'failed', error=str(exc)[:300])
        raise
    return public(update(site_id, status='ready', featureCount=usable, retrievedAt=time.time(), error=None))
