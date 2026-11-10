"""Bounded, read-only OSM map download when Overpass is unavailable.

The map API returns nodes and complete ways, but may leave relation members
outside the viewport unavailable. Join tiles before resolving geometry and
discard incomplete ways/relations rather than drawing missing segments.
"""
import math
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

from .geometry import Frame
from .reference import classify

MAP_API = 'https://api.openstreetmap.org/api/0.6/map'
MAX_TOTAL_BYTES = 40*1024*1024
MAX_TILE_DEPTH = 2


def validate_bbox(bbox, max_side=3000):
    try:
        west, south, east, north = map(float, bbox)
    except (TypeError, ValueError):
        raise ValueError('场地范围无效') from None
    if (not all(math.isfinite(v) for v in (west, south, east, north))
            or not -180 <= west < east <= 180 or not -85 <= south < north <= 85):
        raise ValueError('场地范围无效')
    sides = Frame((west+east)/2, (south+north)/2).scales*[east-west, north-south]
    # Saved place bounds are rounded to seven decimal places.
    if min(sides) < 149.9 or max(sides) > max_side+.1:
        raise ValueError('场地范围的边长应在 150 m 到 3 km 之间')
    return west, south, east, north


def download_map(bbox, progress=lambda percent, message: None):
    bbox = validate_bbox(bbox)
    nodes, ways, relations = {}, {}, {}
    consumed, requests = 0, 0

    def fetch(box, depth=0):
        nonlocal consumed, requests
        requests += 1
        progress(45+min(requests, 20), '下载 OpenStreetMap 备用参照'+(f'（分块 {requests-1}）' if depth else ''))
        url = MAP_API+'?bbox='+','.join(f'{value:.7f}' for value in box)
        request = urllib.request.Request(url, headers={
            'User-Agent': 'DirMap/0.1 orienteering class reference', 'Accept': 'application/xml'})
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                body = response.read(MAX_TOTAL_BYTES-consumed+1)
        except urllib.error.HTTPError as exc:
            message = exc.read(4096).decode(errors='replace')
            if exc.code == 400 and 'too many nodes' in message.lower() and depth < MAX_TILE_DEPTH:
                west, south, east, north = box
                x, y = (west+east)/2, (south+north)/2
                for child in ((west, south, x, y), (x, south, east, y), (west, y, x, north), (x, y, east, north)):
                    fetch(child, depth+1)
                return
            raise ValueError(f'备用参照服务返回 {exc.code}：{message[:160]}') from None
        except OSError as exc:
            raise ValueError(f'备用参照服务暂时不可用：{exc}') from None
        consumed += len(body)
        if consumed > MAX_TOTAL_BYTES:
            raise ValueError('场地内的参照数据过多，请缩小场地范围后重试')
        try:
            root = ET.fromstring(body)
            if root.tag != 'osm':
                raise ValueError('不是 OSM 地图')
            for item in root:
                if item.tag == 'node':
                    nodes[int(item.attrib['id'])] = {'lat': float(item.attrib['lat']), 'lon': float(item.attrib['lon'])}
                elif item.tag in ('way', 'relation'):
                    tags = {tag.attrib['k']: tag.attrib['v'] for tag in item.findall('tag')}
                    if item.tag == 'way':
                        ways[int(item.attrib['id'])] = {'nodes': [int(node.attrib['ref']) for node in item.findall('nd')], 'tags': tags}
                    else:
                        relations[int(item.attrib['id'])] = {'members': [dict(m.attrib) for m in item.findall('member')], 'tags': tags}
        except (ET.ParseError, KeyError, ValueError) as exc:
            raise ValueError(f'备用参照数据不完整：{exc}') from None

    fetch(bbox)
    geometries = {key: [nodes[node] for node in way['nodes']] for key, way in ways.items()
                  if len(way['nodes']) >= 2 and all(node in nodes for node in way['nodes'])}
    elements = [{'type': 'way', 'id': key, 'tags': ways[key]['tags'], 'geometry': geometry}
                for key, geometry in sorted(geometries.items()) if classify(ways[key]['tags'])]
    for key, relation in sorted(relations.items()):
        if not classify(relation['tags']):
            continue
        # Label nodes are not polygon geometry; a missing outer/inner way
        # or a nested relation is. Neither may be bridged by a straight line.
        members = [m for m in relation['members'] if m.get('role', '') in ('outer', 'inner', '')]
        if not members or any(m.get('type') != 'way' or int(m.get('ref', 0)) not in geometries for m in members):
            continue
        elements.append({'type': 'relation', 'id': key, 'tags': relation['tags'], 'members': [
            {'type': 'way', 'ref': int(m['ref']), 'role': m.get('role', ''), 'geometry': geometries[int(m['ref'])]} for m in members]})
    return {'elements': elements}, MAP_API
