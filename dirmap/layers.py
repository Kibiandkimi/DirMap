"""Independent walkable surfaces joined only by explicit, checked portals.

Levels label different surfaces; they are not inferred storey heights.
Portal lengths are supplied model inputs, including zero at a shared
entrance whose approach distance is already represented on both surfaces.
"""
import hashlib
import json

import cv2
import numpy as np
from scipy.sparse import block_diag, coo_matrix
from scipy.spatial import cKDTree

from .graph import RasterGraph, apply_edits, build_graph
from .protocol import ROUTING_SCHEMA


GROUND = {'id': 'ground', 'label': '地面', 'level': 0, 'accepted': True}


def validate(surfaces, portals, edits, controls=()):
    ids = ['ground', *[surface['id'] for surface in surfaces]]
    if len(set(ids)) != len(ids) or any(identifier == '*' for identifier in ids):
        raise ValueError('层级标识重复或使用了保留名称')
    if len({portal['id'] for portal in portals}) != len(portals):
        raise ValueError('换层入口标识重复')
    for edit in edits:
        surface = edit.get('surfaceId', 'ground')
        if surface not in ids and not (surface == '*' and edit['kind'] == 'close'):
            raise ValueError('通行修正引用了不存在的层级')
        if edit.get('separateGround') and (surface == 'ground' or edit['kind'] != 'open'):
            raise ValueError('只能将其他层级的开放区域从地面分离')
    for control in controls:
        if control.get('surfaceId') is not None and control['surfaceId'] not in ids:
            raise ValueError('目标引用了不存在的到访层级')
    for portal in portals:
        if portal['fromSurface'] not in ids or portal['toSurface'] not in ids or portal['fromSurface'] == portal['toSurface']:
            raise ValueError('换层入口必须连接两个已定义的不同层级')
        if (not np.isfinite(portal.get('lengthM', 0)) or not np.isfinite(portal.get('costFactor', 1))
                or portal.get('lengthM', 0) < 0 or portal.get('costFactor', 1) < 1):
            raise ValueError('换层入口的长度或耗时系数无效')
        confirmed = {'ground', *(surface['id'] for surface in surfaces if surface.get('accepted'))}
        if portal.get('accepted') and not {portal['fromSurface'], portal['toSurface']}.issubset(confirmed):
            raise ValueError('请先核验入口连接的两个层级范围')


def _region(array):
    yy, xx = np.nonzero(array)
    if not len(xx):
        return 0, 0, 0, 0
    return int(xx.min()), int(yy.min()), int(xx.max()+1), int(yy.max()+1)


def attach(graph, definitions, raster_data, node_surfaces, portal_edges, base_count):
    """Restore per-surface spatial indexes after construction or cache load."""
    graph.surface_order = [surface['id'] for surface in definitions]
    graph.node_surfaces = np.asarray(node_surfaces, dtype=np.int32)
    graph.surface_data = {}
    graph.virtual_points = {}
    for index, (definition, data) in enumerate(zip(definitions, raster_data)):
        nodes = np.flatnonzero(graph.node_surfaces == index)
        info = {**definition, **data, 'nodes': nodes,
                'tree': cKDTree(graph.pixels[nodes]) if len(nodes) else None}
        graph.surface_data[definition['id']] = info
        graph.virtual_points[definition['id']] = {}
    graph.portal_edges = portal_edges
    graph.base_node_count = base_count
    # Ground-object recognition continues to search the original ground
    # node IDs. Layer-aware snapping uses the indexes above instead.
    graph.tree = cKDTree(graph.pixels[:base_count])
    for a, b in portal_edges:
        for node in (a, b):
            surface = graph.surface_for_node(node)
            graph.virtual_points[surface][tuple(np.round(graph.pixels[node], 8))] = node
    return graph


def build(costs, registration, source_hash, edits, surfaces, portals, restricted=None, step=4, lower_level=None):
    validate(surfaces, portals, edits)
    if not surfaces and not portals:
        return build_graph(costs, registration, source_hash, edits, step)
    definitions = [GROUND.copy(), *[{**surface, 'accepted': bool(surface.get('accepted'))} for surface in surfaces]]
    accepted = {'ground', *(surface['id'] for surface in surfaces if surface.get('accepted'))}
    base_edits, uncut_edits, cut_edits = [], [], []
    for edit in edits:
        surface = edit.get('surfaceId', 'ground')
        if surface in ('ground', '*'):
            base_edits.append(edit); uncut_edits.append(edit)
            cut_edits.append({**edit, 'kind': 'close'})
        elif surface in accepted and edit.get('separateGround'):
            base_edits.append({**edit, 'kind': 'close'})
            cut_edits.append({**edit, 'kind': 'open', 'cost': 1.})
    base_costs, closed = apply_edits(costs, base_edits)
    uncut, explicit_closed = apply_edits(costs, uncut_edits)
    cuts, _ = apply_edits(np.zeros_like(costs), cut_edits)
    restricted = np.zeros(costs.shape, bool) if restricted is None else np.asarray(restricted, bool)
    if lower_level is not None:
        known_lower = np.asarray(lower_level, bool) & (cuts > 0) & (uncut > 0) & (explicit_closed == 0) & ~restricted
        base_costs[known_lower] = uncut[known_lower]
        cuts[known_lower] = 0
    # An explicit entrance can restore its ground approach through a mask
    # created solely to separate layers. It cannot reopen a wall, a closure
    # or a forbidden pixel from the original ground model.
    for portal in portals:
        if not portal.get('accepted') or 'ground' not in (portal['fromSurface'], portal['toSurface']):
            continue
        point = np.rint(portal['pixel']).astype(int)
        if not (0 <= point[0] < costs.shape[1] and 0 <= point[1] < costs.shape[0]):
            raise ValueError('换层入口超出地图范围')
        local = np.zeros(costs.shape, np.uint8)
        cv2.circle(local, tuple(point), int(portal.get('radiusPx', 8)), 1, -1)
        allowed = (local > 0) & (cuts > 0) & (uncut > 0) & (explicit_closed == 0) & ~restricted
        base_costs[allowed] = uncut[allowed]
        cuts[allowed] = 0
    base = build_graph(base_costs, registration, source_hash, [], step)
    base.closed_edits = ((explicit_closed > 0) | (cuts > 0)).astype(np.uint8)
    parts, offsets, infos = [base], {'ground': 0}, []
    infos.append({'origin': [0, 0], 'costs': base.costs, 'owner': base.owner, 'extent': None})
    layer_parts = {'ground': base}
    count = len(base.pixels)
    for definition in surfaces:
        identifier = definition['id']
        selected = [edit for edit in edits if edit.get('surfaceId', 'ground') in (identifier, '*')] if identifier in accepted else []
        layer_costs, _ = apply_edits(np.zeros_like(costs), selected)
        extent, _ = apply_edits(np.zeros_like(costs), [{**e, 'cost': 1.} for e in selected if e['kind'] == 'open'])
        layer_costs[restricted] = 0
        bounds = _region(extent)
        x0, y0, x1, y1 = bounds
        offsets[identifier] = count
        if not np.any(layer_costs > 0):
            infos.append({'origin': [x0, y0], 'costs': layer_costs[y0:y1, x0:x1].copy(),
                          'owner': np.full((y1-y0, x1-x0), -1, np.int32), 'extent': (extent[y0:y1, x0:x1] > 0).copy()})
            layer_parts[identifier] = None
            continue
        part = build_graph(layer_costs, registration, source_hash+':'+identifier, [], step)
        owner = part.owner[y0:y1, x0:x1].copy()
        owner[owner >= 0] += count
        info = {'origin': [x0, y0], 'costs': part.costs[y0:y1, x0:x1].copy(),
                'owner': owner, 'extent': (extent[y0:y1, x0:x1] > 0).copy()}
        infos.append(info)
        # Keep only the small surface window, while its graph pixels remain
        # in the original map frame. Avoid retaining a full image per layer.
        part.costs = info['costs']
        part.owner = owner.copy()
        part.owner[part.owner >= 0] -= count
        part.surface_data = {'ground': {**GROUND, 'origin': [x0, y0], 'costs': part.costs,
                                       'owner': part.owner, 'tree': part.tree, 'nodes': None}}
        layer_parts[identifier] = part
        parts.append(part)
        count += len(part.pixels)
    pixels = [part.pixels for part in parts]
    local = [part.local for part in parts]
    sizes = [part.cell_sizes for part in parts]
    node_surfaces = []
    for index, definition in enumerate(definitions):
        part = layer_parts[definition['id']]
        if part is not None:
            node_surfaces.append(np.full(len(part.pixels), index, np.int32))
    rows, columns, lengths, weights, portal_edges = [], [], [], [], {}
    for portal in portals:
        if not portal.get('accepted'):
            continue
        point = np.asarray(portal['pixel'], float)
        world, valid = registration.forward([point])
        if not valid[0]:
            raise ValueError('换层入口超出配准有效范围')
        if restricted[tuple(np.rint(point).astype(int)[::-1])]:
            raise ValueError('换层入口不能位于禁入区域')
        ends = []
        for identifier in (portal['fromSurface'], portal['toSurface']):
            part = layer_parts[identifier]
            if part is None:
                raise ValueError('入口引用的层级尚未绘制可通行区域')
            try:
                node = part.snap(point, max_distance_px=portal.get('radiusPx', 8))
            except ValueError as error:
                raise ValueError('入口在“'+next(d['label'] for d in definitions if d['id'] == identifier)+'”中没有同层通路') from error
            vertex = count
            count += 1
            pixels.append(point[None, :]); local.append(world.copy()); sizes.append(np.ones(1, np.float32))
            node_surfaces.append(np.array([next(i for i, d in enumerate(definitions) if d['id'] == identifier)], np.int32))
            length = float(np.linalg.norm(part.local[node]-world[0]))
            cost = part.cost_at(point)
            rows += [node+offsets[identifier], vertex]
            columns += [vertex, node+offsets[identifier]]
            lengths += [length, length]; weights += [length*cost, length*cost]
            ends.append(vertex)
        a, b = ends
        length = float(portal.get('lengthM', 0))
        weight = length*float(portal.get('costFactor', 1))
        rows += [a, b]; columns += [b, a]; lengths += [length, length]; weights += [weight, weight]
        record = {**portal, 'local': world[0].tolist(), 'lengthM': length, 'travelWeightM': weight}
        portal_edges[a, b] = record
        portal_edges[b, a] = {**record, 'fromSurface': portal['toSurface'], 'toSurface': portal['fromSurface']}
    base_weights = block_diag([part.weights for part in parts], format='coo')
    base_lengths = block_diag([part.lengths for part in parts], format='coo')
    row = np.r_[base_weights.row, rows].astype(np.int32)
    col = np.r_[base_weights.col, columns].astype(np.int32)
    weighted = coo_matrix((np.r_[base_weights.data, weights], (row, col)), shape=(count, count)).tocsr()
    distance = coo_matrix((np.r_[base_lengths.data, lengths], (row, col)), shape=(count, count)).tocsr()
    digest = hashlib.sha256(json.dumps({'source': source_hash, 'registration': registration.as_dict(), 'edits': edits,
        'surfaces': surfaces, 'portals': portals, 'schema': ROUTING_SCHEMA}, sort_keys=True).encode()+costs.tobytes()+restricted.tobytes()
        +(np.asarray(lower_level, bool).tobytes() if lower_level is not None else b'')).hexdigest()[:20]
    result = RasterGraph(weighted, distance, np.concatenate(pixels), np.concatenate(local), base.costs, digest, step, True)
    result.owner, result.closed_edits, result.cell_sizes = base.owner, base.closed_edits, np.concatenate(sizes)
    return attach(result, definitions, infos, np.concatenate(node_surfaces), portal_edges, len(base.pixels))


def reference_candidates(state, features):
    """Reference geometry is a review suggestion, never a traversability edit."""
    from .geometry import Registration
    reg = Registration.from_dict(state['registration'])
    scale = float(np.sqrt(abs(np.linalg.det(reg.affine[:, :2]))))
    result = []
    for feature in features:
        tags = feature.get('tags', {})
        if feature.get('kind') != 'road' or tags.get('highway') not in ('footway', 'path', 'steps', 'pedestrian', 'cycleway'):
            continue
        if tags.get('area') == 'yes' or tags.get('foot') == 'no' or tags.get('access') in ('no', 'private'):
            continue
        if not any(tags.get(key) not in (None, 'no', '0') for key in ('bridge', 'tunnel', 'layer', 'covered')):
            continue
        points = reg.inverse(np.asarray(feature['points'], float))
        if (len(points) < 2 or not np.isfinite(points).all() or (points < 0).any()
                or (points[:, 0] >= state['width']).any() or (points[:, 1] >= state['height']).any()):
            continue
        try:
            level = int(tags.get('layer', -1 if tags.get('tunnel') == 'yes' else 1))
        except ValueError:
            level = -1 if tags.get('tunnel') == 'yes' else 1
        try:
            width = float(tags.get('width', 2))
        except ValueError:
            width = 2
        if not np.isfinite(width) or width <= 0:
            width = 2
        label = tags.get('name') or ('地下通道' if level < 0 else '人行桥' if tags.get('bridge') == 'yes' else '有顶通道')
        result.append({'id': feature['id'], 'label': label, 'level': max(-20, min(20, level)),
                       'pixels': points.tolist(), 'suggestedRadiusPx': max(1, min(100, round(width/max(.01, scale)/2))),
                       'usable': tags.get('smoothness') != 'impassable', 'tags': tags,
                       'scope': 'OSM 参照位置；须对照原图和现场核验宽度、通行区域与出入口，未自动加入路网。'})
    return result
