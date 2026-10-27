"""A place's OSM reference. It is geodetic evidence, not a complete route graph."""

import json
from pathlib import Path

import cv2
import numpy as np

from .geometry import Frame


def polygon_properties(points):
    contour = np.asarray(points, dtype=np.float32).reshape(-1, 1, 2)
    moments = cv2.moments(contour)
    area = abs(moments['m00'])
    if area < 1:
        return None
    center = [moments['m10']/moments['m00'], moments['m01']/moments['m00']]
    _, sides, angle = cv2.minAreaRect(contour)
    ratio = max(sides) / max(1e-6, min(sides))
    return {'center': center, 'area': area, 'aspect': ratio, 'points': np.asarray(points).tolist()}


# Parkland and stream centre lines only draw a park recognisable on screen.
# Registration matches buildings, water and pitches; routing never uses any
# reference polygon as a traversability mask.
GREEN = {'leisure': ('park', 'garden'), 'landuse': ('grass', 'forest', 'meadow', 'recreation_ground', 'village_green'),
         'natural': ('wood', 'scrub', 'grassland')}
LINES = ('road', 'waterway')


def classify(tags):
    if tags.get('building'):
        return 'building'
    if tags.get('natural') == 'water' or tags.get('leisure') == 'swimming_pool':
        return 'water'
    if tags.get('leisure') == 'pitch':
        return 'pitch'
    if tags.get('highway'):
        return 'road'
    if tags.get('waterway') in ('river', 'stream', 'canal', 'drain', 'ditch'):
        return 'waterway'
    if any(tags.get(key) in values for key, values in GREEN.items()):
        return 'green'
    return None


def load_reference(paths, frame=None):
    """Features of one place, from one or more Overpass-format files."""
    frame = frame or Frame()
    elements = []
    for path in [paths] if isinstance(paths, (str, Path)) else paths:
        elements += json.loads(Path(path).read_text(encoding='utf-8'))['elements']
    features = []
    for element in elements:
        tags = element.get('tags', {})
        coordinates = [[p['lon'], p['lat']] for p in element.get('geometry', []) if p]
        if element.get('type') == 'relation':
            # Join open OSM outer-member ways by endpoint. Inner islands are
            # irrelevant to a matching outline; route generation never uses
            # the filled reference polygon as a traversability mask.
            segments = [[[p['lon'], p['lat']] for p in m.get('geometry', []) if p]
                        for m in element.get('members', []) if m.get('role') == 'outer' and m.get('geometry')]
            coordinates = segments.pop(0) if segments else []
            while segments:
                matched = False
                for i, segment in enumerate(segments):
                    if coordinates[-1] == segment[0]:
                        coordinates += segment[1:]
                    elif coordinates[-1] == segment[-1]:
                        coordinates += list(reversed(segment))[1:]
                    elif coordinates[0] == segment[-1]:
                        coordinates = segment[:-1]+coordinates
                    elif coordinates[0] == segment[0]:
                        coordinates = list(reversed(segment))[:-1]+coordinates
                    else:
                        continue
                    segments.pop(i); matched = True; break
                if not matched:
                    break
            if segments or not coordinates or coordinates[0] != coordinates[-1]:
                continue
        if len(coordinates) < 2:
            continue
        kind = classify(tags)
        if kind is None:
            continue
        local = frame.to_local(coordinates)
        properties = polygon_properties(local) if kind not in LINES else None
        if kind not in LINES and (not properties or properties['area'] < 40):
            continue
        features.append({'id': str(element['id']), 'kind': kind, 'tags': tags,
                         'lnglat': coordinates, 'points': local.tolist(), **(properties or {})})
    return features


def geojson(features):
    return {'type': 'FeatureCollection', 'features': [
        {'type': 'Feature', 'id': item['id'],
         'properties': {'kind': item['kind'], **item['tags']},
         'geometry': {'type': 'LineString' if item['kind'] in LINES else 'Polygon',
                      'coordinates': item['lnglat'] if item['kind'] in LINES else [item['lnglat']]}}
        for item in features]}
