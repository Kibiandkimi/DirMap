"""Legal approach candidates for cartographic point symbols and building edges.

The circle centre remains the target's exact map position. Approaches end on
walkable ground and never add an edge through an obstacle. A long wall, a
forbidden area, conflicting sides, and a large enclosing feature need review.
"""

import cv2
import numpy as np

from .graph import segment_pixels


class AccessResolver:
    def __init__(self, graph, registration, semantics):
        self.graph = graph
        self.registration = registration
        self.semantics = semantics
        self.components = {}
        self.scale = float(np.sqrt(abs(np.linalg.det(registration.affine[:, :2]))))

    def release(self):
        """Drop full-image labels between plans; the cached graph outlives them.

        Rebinding keeps a concurrent resolution's own reference valid.
        """
        self.components = {}

    def _components(self, kind):
        parts = self.components.get(kind)
        if parts is None:
            mask = self.semantics.get(kind)
            if mask is None:
                return None
            mask = (mask > 0).astype(np.uint8)
            if kind == 'water':
                # This semantic footprint only joins the quadrants of a
                # tiny fountain's cross symbol. It never modifies terrain.
                mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
            _, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
            parts = self.components[kind] = (labels, stats)
        return parts

    def _feature(self, kind, point, circle_radius):
        point = np.asarray(point, dtype=float)
        parts = self._components(kind)
        if parts is None:
            return None
        labels, stats = parts
        x, y = np.rint(point).astype(int)
        ids = []
        if labels[y, x]:
            ids.append(int(labels[y, x]))
        near = labels[max(0, y-2):y+3, max(0, x-2):x+3]
        ids += [int(i) for i in np.unique(near) if i and i not in ids]
        # White interiors of a small ring/cross can be isolated graph nodes;
        # look for an enclosing point-symbol contour, not an enclosing wall.
        radius = max(8, round(min(circle_radius, 12/max(self.scale, .1))))
        x0, y0 = max(0, x-radius), max(0, y-radius)
        x1, y1 = min(labels.shape[1], x+radius+1), min(labels.shape[0], y+radius+1)
        crop = (labels[y0:y1, x0:x1] > 0).astype(np.uint8)
        contours, _ = cv2.findContours(crop, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            if cv2.pointPolygonTest(contour, (float(point[0]-x0), float(point[1]-y0)), False) >= 0:
                px, py = contour[0, 0]
                label = int(labels[py+y0, px+x0])
                if label and label not in ids:
                    ids.append(label)
        for label in ids:
            bx, by, width, height, area = stats[label]
            largest, smallest = max(width, height)*self.scale, min(width, height)*self.scale
            if kind in ('point_ink', 'vegetation'):
                if largest > (18 if kind == 'point_ink' else 8) or max(width, height) > max(12, circle_radius):
                    continue
                local = (labels[by:by+height, bx:bx+width] == label).astype(np.uint8)
                yy, xx = np.nonzero(local)
                if len(xx) < 3:
                    continue
                covariance = np.cov(np.column_stack((xx, yy)).T)
                eigenvalues = np.linalg.eigvalsh(covariance)
                if eigenvalues[0] < .08*max(1e-6, eigenvalues[1]):
                    continue  # narrow line / fence, regardless of angle
                if kind == 'point_ink' and area/(width*height) < .22:
                    continue  # a sparse angular wall is not a solid sculpture
                limit, method = min(8., max(3., largest/2+1)), 'point_symbol_edge'
            elif kind == 'building':
                # Reject a narrow elongated grey wall as well as a linear
                # black barrier; do not pick its other side as a checkpoint.
                if smallest < 3.5 and largest > 3*max(.1, smallest):
                    continue
                if area*self.scale*self.scale < 8:
                    continue
                local = (labels[by:by+height, bx:bx+width] == label).astype(np.uint8)
                limit, method = 3., 'building_edge'
                if largest <= 26 and max(width, height) <= circle_radius*1.5 and area/(width*height) < .55:
                    contours, hierarchy = cv2.findContours(local, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
                    rings = []
                    for contour, relation in zip(contours, hierarchy[0] if hierarchy is not None else []):
                        if relation[3] < 0:
                            continue
                        _, ring_radius = cv2.minEnclosingCircle(contour)
                        if cv2.contourArea(contour) >= 6 and cv2.contourArea(contour)/max(1, np.pi*ring_radius**2) >= .75:
                            rings.append(contour)
                    if len(rings) >= 2 and any(cv2.pointPolygonTest(c, tuple((point-[bx, by]).astype(float)), False) >= 0 for c in rings):
                        limit, method = 8., 'compound_point_symbol_edge'
            else:
                local = (labels[by:by+height, bx:bx+width] == label).astype(np.uint8)
                outline, _ = cv2.findContours(local, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                if not outline:
                    continue
                contour = max(outline, key=cv2.contourArea)
                circularity = 4*np.pi*cv2.contourArea(contour)/max(1., cv2.arcLength(contour, True)**2)
                if largest <= 18 and smallest >= largest*.6 and area*self.scale*self.scale <= 240 and circularity >= .65:
                    limit, method = min(9., largest/2+1.5), 'small_water_feature_edge'
                else:
                    # A bank can be approached only immediately at its edge;
                    # an interior lake point cannot be moved to a distant road.
                    limit, method = 3., 'water_edge'
            # A symbol footprint may be filled to locate its outside edge;
            # this fill is NEVER applied to the graph's navigable cost mask.
            contour_list, _ = cv2.findContours(local, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            footprint = np.zeros_like(local)
            cv2.drawContours(footprint, contour_list, -1, 1, cv2.FILLED)
            footprint = cv2.copyMakeBorder(footprint, 2, 2, 2, 2, cv2.BORDER_CONSTANT, value=0)
            # Include the anti-aliased outline around a roof/fountain.
            footprint = cv2.dilate(footprint, np.ones((3, 3), np.uint8))
            origin = np.array([bx-2, by-2])
            at = np.rint(point-origin).astype(int)
            if not (0 <= at[0] < footprint.shape[1] and 0 <= at[1] < footprint.shape[0] and footprint[at[1], at[0]]):
                continue
            # The filled, borderless silhouette is the symbol itself; the
            # dilated footprint only bounds the search. Crossing the symbol's
            # own body is expected for a checkpoint drawn on it, while any
            # other blocked pixel still disqualifies the approach.
            symbol = np.zeros_like(local)
            cv2.drawContours(symbol, contour_list, -1, 1, cv2.FILLED)
            return {'origin': origin, 'mask': footprint, 'limitM': limit, 'method': method, 'kind': kind,
                    'symbol': symbol, 'symbolOrigin': np.array([bx, by])}
        return None

    @staticmethod
    def _in_feature(feature, points):
        xy = np.rint(np.asarray(points)-feature['origin']).astype(int)
        valid = (xy[:, 0] >= 0) & (xy[:, 1] >= 0) & (xy[:, 0] < feature['mask'].shape[1]) & (xy[:, 1] < feature['mask'].shape[0])
        inside = np.zeros(len(xy), dtype=bool)
        inside[valid] = feature['mask'][xy[valid, 1], xy[valid, 0]] > 0
        return inside

    @staticmethod
    def _over_symbol(feature, points):
        """Whether each pixel lies on the feature itself, not just its halo."""
        symbol = feature.get('symbol')
        if symbol is None:
            return AccessResolver._in_feature(feature, points)
        xy = np.rint(np.asarray(points)-feature['symbolOrigin']).astype(int)
        valid = (xy[:, 0] >= 0) & (xy[:, 1] >= 0) & (xy[:, 0] < symbol.shape[1]) & (xy[:, 1] < symbol.shape[0])
        inside = np.zeros(len(xy), dtype=bool)
        inside[valid] = symbol[xy[valid, 1], xy[valid, 0]] > 0
        return inside

    def _complete_outline(self, feature, circle_radius):
        """Recover a small symbol's dark rim, never a connected wall.

        A fountain's black outline is not in the blue mask; compression can
        likewise leave grey edge pixels around a black point symbol. Absorb
        only complete, tightly bounded obstacle components. A component
        continuing beyond the symbol's small halo is a separate/attached
        barrier, and cannot be licensed by its connection to the symbol.
        """
        if feature.get('stroke') or feature['method'] in ('building_edge', 'water_edge'):
            return feature
        symbol = feature['symbol']
        sx, sy = feature['symbolOrigin']
        height, width = self.graph.costs.shape
        padding = max(2, min(4, round(circle_radius*.10)))
        x0, y0 = max(0, sx-padding-1), max(0, sy-padding-1)
        x1 = min(width, sx+symbol.shape[1]+padding+1)
        y1 = min(height, sy+symbol.shape[0]+padding+1)
        hard = (self.graph.costs[y0:y1, x0:x1] <= 0).astype(np.uint8)
        _, labels, stats, _ = cv2.connectedComponentsWithStats(hard, 8)
        owned = np.zeros_like(hard)
        owned[sy-y0:sy-y0+symbol.shape[0], sx-x0:sx-x0+symbol.shape[1]] = symbol
        for label in np.unique(labels[owned > 0]):
            if not label:
                continue
            bx, by, bw, bh, _ = stats[label]
            # Complete component must have a free border on all four sides.
            if bx == 0 or by == 0 or bx+bw >= hard.shape[1] or by+bh >= hard.shape[0]:
                continue
            component = labels == label
            if any(mask is not None and np.any(mask[y0:y1, x0:x1][component])
                   for mask in (self.semantics.get('restricted'), getattr(self.graph, 'closed_edits', None))):
                continue
            owned[component] = 1
        contours, _ = cv2.findContours(owned, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(owned, contours, -1, 1, cv2.FILLED)
        return {**feature, 'symbol': owned, 'symbolOrigin': np.array([x0, y0]),
                'mask': cv2.dilate(owned, np.ones((3, 3), np.uint8)), 'origin': np.array([x0, y0])}

    def _ink_feature(self, point, circle_radius):
        """Approach a complete short stroke or the observed tip of a barrier.

        A sign, curved wall, or wall end can be the feature under the circle.
        Only its bounded body may associate an approach with the target;
        route edges and the subsequent local-side check remain unchanged.
        """
        parts = self._components('point_ink')
        if parts is None:
            return None
        labels, stats = parts
        height, width = labels.shape
        x, y = np.rint(point).astype(int)
        label = int(labels[y, x])
        if not label:
            return None
        bx, by, bw, bh, _ = stats[label]
        complete = (max(bw, bh) <= max(12., circle_radius*1.25) and max(bw, bh)*self.scale <= 18
                    and bx > 0 and by > 0 and bx+bw < width and by+bh < height)
        method = 'ink_stroke_edge'
        if complete:
            x0, y0 = max(0, bx-2), max(0, by-2)
            x1, y1 = min(width, bx+bw+2), min(height, by+bh+2)
            stroke = (labels[y0:y1, x0:x1] == label).astype(np.uint8)
        else:
            from .network_registration import thin
            radius = max(6, int(np.ceil(min(circle_radius, 5/max(self.scale, .1)))))
            x0, y0 = max(0, x-radius), max(0, y-radius)
            x1, y1 = min(width, x+radius+1), min(height, y+radius+1)
            local = (labels[y0:y1, x0:x1] == label).astype(np.uint8)
            skeleton = thin(local)
            kernel = np.ones((3, 3), np.uint8); kernel[1, 1] = 0
            neighbours = cv2.filter2D(skeleton, cv2.CV_16U, kernel, borderType=cv2.BORDER_CONSTANT)
            tips = (skeleton > 0) & (neighbours == 1)
            tips[:3] = tips[-3:] = False; tips[:, :3] = tips[:, -3:] = False
            ty, tx = np.nonzero(tips)
            if not len(tx):
                return None
            distance = np.hypot(tx+x0-point[0], ty+y0-point[1])
            index = int(np.argmin(distance))
            if distance[index] > min(circle_radius*.25, 1.5/max(self.scale, .1)):
                return None
            yy, xx = np.indices(local.shape)
            near_tip = (xx-tx[index])**2+(yy-ty[index])**2 <= (2/max(self.scale, .1))**2
            _, connected = cv2.connectedComponents((local & near_tip).astype(np.uint8), 8)
            own = connected[ty[index], tx[index]]
            if not own or connected[y-y0, x-x0] != own:
                return None
            stroke = (connected == own).astype(np.uint8)
            method = 'ink_tip_edge'
        # Keep the one-pixel print rim with its stroke. Other hard pixels,
        # restricted ground and manual closures are checked on every ray.
        symbol = cv2.dilate(stroke, np.ones((3, 3), np.uint8))
        grown = cv2.dilate(stroke, np.ones((5, 5), np.uint8))
        return {'origin': np.array([x0, y0]), 'mask': grown, 'limitM': 3.,
                'method': method, 'kind': 'point_ink', 'stroke': True,
                'strokeLabel': label, 'symbol': symbol, 'symbolOrigin': np.array([x0, y0])}

    @staticmethod
    def _stroke_component(feature, blocked):
        """True when every blocked pixel lies in the checkpoint's own stroke."""
        symbol = feature.get('symbol')
        if symbol is None:
            return None
        xy = np.rint(np.asarray(blocked)-feature['symbolOrigin']).astype(int)
        if (xy < 0).any() or (xy[:, 0] >= symbol.shape[1]).any() or (xy[:, 1] >= symbol.shape[0]).any():
            return None
        return True if np.all(symbol[xy[:, 1], xy[:, 0]] > 0) else None

    def review_candidates(self, control, required_component):
        """Nearby reachable ground to preview, never an automatic approach.

        Ambiguous symbols can require choosing a side by looking at a photo
        or at the site. Offer spatially distinct choices without changing
        the circle, opening obstacles, or allowing a plan before confirmation.
        """
        if required_component is None or control.get('approachPixel'):
            return []
        point = np.asarray(control['pixel'], float)
        world, valid = self.registration.forward([point])
        x, y = np.rint(point).astype(int)
        if not valid[0] or not (0 <= x < self.graph.costs.shape[1] and 0 <= y < self.graph.costs.shape[0]):
            return []
        for mask in (self.semantics.get('restricted'), self.semantics.get('water'), getattr(self.graph, 'closed_edits', None)):
            if mask is not None and mask[y, x]:
                return []
        limit_m = min(12., max(4., control.get('radiusPx', 15)*self.scale*1.4))
        radius = int(np.ceil(limit_m/self.scale))
        data = self.graph.surface_info('ground')
        owner = data['owner']
        if owner is None:
            return []
        ox, oy = np.rint(data['origin']).astype(int)
        x0, x1 = max(0, x-ox-radius), min(owner.shape[1], x-ox+radius+1)
        y0, y1 = max(0, y-oy-radius), min(owner.shape[0], y-oy+radius+1)
        crop = owner[y0:y1, x0:x1]
        yy, xx = np.nonzero((crop >= 0) & (self.graph.components[np.maximum(crop, 0)] == required_component))
        if not len(xx):
            return []
        pixels = np.column_stack((xx+x0+ox, yy+y0+oy)).astype(float)
        local, valid = self.registration.forward(pixels)
        delta = local-world[0]
        distances = np.linalg.norm(delta, axis=1)
        bearings = (np.degrees(np.arctan2(delta[:, 0], delta[:, 1]))+360) % 360
        candidates = []
        for i in np.argsort(distances):
            if not valid[i] or distances[i] > limit_m or distances[i] < .15:
                continue
            if any(min(abs(bearings[i]-c['bearingDeg']), 360-abs(bearings[i]-c['bearingDeg'])) < 60
                   or np.linalg.norm(pixels[i]-c['pixel'])*self.scale < 1.5 for c in candidates):
                continue
            try:
                node = self.graph.snap(pixels[i], max_distance_px=1)
                if self.graph.components[node] != required_component:
                    continue
            except ValueError:
                continue
            direction = ['北', '东北', '东', '东南', '南', '西南', '西', '西北'][round(float(bearings[i])/45) % 8]
            candidates.append({'pixel': pixels[i].tolist(), 'offsetM': round(float(distances[i]), 2),
                               'bearingDeg': round(float(bearings[i]), 1), 'direction': direction, 'surfaceId': 'ground'})
            if len(candidates) == 4:
                break
        return candidates

    def _road_approach(self, control, required_component):
        """Associate a printed target with nearby reachable ground.

        The association is not a walk from the circle centre. Only the
        returned road point connects to the graph. Private green therefore
        stays closed, including between two automatically adjusted targets.
        """
        olive = self.semantics.get('olive')
        if (olive is None or control.get('approachPixel') or control.get('role', 'control') != 'control'
                or control['id'] in ('start', 'finish')):
            return None
        point = np.asarray(control['pixel'], float)
        world, valid = self.registration.forward([point])
        x, y = np.rint(point).astype(int)
        height, width = self.graph.costs.shape
        if not valid[0] or not (0 <= x < width and 0 <= y < height):
            return None
        closed = getattr(self.graph, 'closed_edits', None)
        prohibited = self.semantics.get('prohibited')
        for mask in (self.semantics.get('water'), prohibited, closed):
            if mask is not None and mask[y, x]:
                return None
        # This fallback concerns circles on/near private green, not arbitrary
        # relocations of ambiguous buildings, lakes or targets behind walls.
        near = max(2, int(np.ceil(2/self.scale)))
        if not olive[max(0, y-near):y+near+1, max(0, x-near):x+near+1].any():
            return None
        radius = float(control.get('radiusPx', 15))
        limit = min(15., radius*self.scale*1.4)
        reach = int(np.ceil(min(radius*1.4, limit/self.scale)))
        x0, y0, x1, y1 = max(0, x-reach), max(0, y-reach), min(width, x+reach+1), min(height, y+reach+1)
        # Paving is preferred. White/yellow open ground on the same legal
        # network is also usable, e.g. the court side of a fenced boundary.
        ground = (self.graph.costs[y0:y1, x0:x1] > 0) & (self.graph.costs[y0:y1, x0:x1] < 1.2)
        if self.semantics.get('open_ground') is not None:
            ground &= self.semantics['open_ground'][y0:y1, x0:x1]
        # Require actual area support, not a single-pixel contour, a page
        # annotation, or compression speckles connected to a road far away.
        core = cv2.erode(ground.astype(np.uint8), np.ones((3, 3), np.uint8)) > 0
        area_support = cv2.dilate(core.astype(np.uint8), np.ones((3, 3), np.uint8)) > 0
        # A genuinely narrow printed path can be less than three pixels
        # wide. Its pale paving ink is evidence even without an area core;
        # brown contours have neither pale ground nor paving evidence.
        if self.semantics.get('paved') is not None:
            area_support |= self.semantics['paved'][y0:y1, x0:x1]
        ground &= area_support
        paving_core = (cv2.erode((self.semantics['paved'][y0:y1, x0:x1] > 0).astype(np.uint8),
                               np.ones((3, 3), np.uint8)) > 0) if self.semantics.get('paved') is not None else np.zeros(ground.shape, bool)
        _, labels, stats, _ = cv2.connectedComponentsWithStats(ground.astype(np.uint8), 8)
        keep = np.zeros(len(stats), bool)
        for i, (_, _, w, h, area) in enumerate(stats[1:], 1):
            keep[i] = area >= max(6, .5/self.scale**2) and max(w, h)*self.scale >= min(4., limit*.5)
        ground = keep[labels]
        nodes = np.asarray(self.graph.tree.query_ball_point(point, reach), int)
        if getattr(self.graph, 'node_surfaces', None) is not None:
            nodes = nodes[self.graph.node_surfaces[nodes] == self.graph.surface_order.index('ground')]
        if not len(nodes):
            return None
        pixels = np.rint(self.graph.pixels[nodes]).astype(int)
        inside = (pixels[:, 0] >= x0) & (pixels[:, 0] < x1) & (pixels[:, 1] >= y0) & (pixels[:, 1] < y1)
        nodes, pixels = nodes[inside], pixels[inside]
        take = ground[pixels[:, 1]-y0, pixels[:, 0]-x0]
        if required_component is not None:
            take &= self.graph.components[nodes] == required_component
        nodes, pixels = nodes[take], pixels[take]
        if not len(nodes):
            return None
        distance = np.linalg.norm(self.graph.local[nodes]-world[0], axis=1)
        on_paving = paving_core[pixels[:, 1]-y0, pixels[:, 0]-x0]
        # A small preference for a drawn path must not move a target to a
        # remote road instead of the open ground immediately beside it.
        order = np.lexsort((nodes, distance, distance+np.where(on_paving, 0., 2.)))
        own = next((f for kind in ('point_ink', 'vegetation', 'building')
                    if (f := self._feature(kind, point, radius)) is not None), None)
        if own is None:
            own = self._ink_feature(point, radius)
        for index in order:
            if distance[index] > limit:
                continue
            node = int(nodes[index]); end = self.graph.pixels[node]
            ray = segment_pixels(point, end)
            rx, ry = ray.T
            if any(mask is not None and mask[ry, rx].any() for mask in (self.semantics.get('water'), prohibited, closed)):
                continue
            owned = self._over_symbol(own, ray) if own is not None else np.zeros(len(ray), bool)
            from_target = np.linalg.norm(ray-point, axis=1)*self.scale
            if own is not None and own['kind'] == 'building':
                owned &= from_target <= 3.
            ink = self.semantics.get('point_ink')
            roof = self.semantics.get('building')
            # The circle can straddle its own thin printed rim. A separate
            # wall farther along the association remains a barrier.
            if ink is not None and np.any((ink[ry, rx] > 0) & ~owned & (from_target > .8)):
                continue
            if roof is not None and np.any((roof[ry, rx] > 0) & ~owned):
                continue
            try:
                if self.graph.snap(end, max_distance_px=1, surface_id='ground') != node:
                    continue
            except ValueError:
                continue
            return {'node': node, 'pixel': end.astype(float).tolist(), 'local': self.graph.local[node].tolist(),
                    'method': 'nearby_road' if on_paving[index] else 'nearby_open_ground', 'featureKind': 'restricted_boundary',
                    'offsetM': float(distance[index]), 'automatic': True, 'reviewRequired': True,
                    'targetPixel': list(control['pixel'])}
        return None

    def resolve(self, control, required_component=None):
        try:
            return self._resolve_symbol(control, required_component)
        except ValueError:
            nearby = self._road_approach(control, required_component)
            if nearby is not None:
                return nearby
            raise

    def _resolve_symbol(self, control, required_component=None):
        point = np.asarray(control.get('approachPixel') or control['pixel'], dtype=float)
        world, valid = self.registration.forward([point])
        if not valid[0]:
            raise ValueError('目标超出有效配准范围，需要补充参照')
        x, y = np.rint(point).astype(int)
        if not (0 <= x < self.graph.costs.shape[1] and 0 <= y < self.graph.costs.shape[0]):
            raise ValueError('到访位置超出原图')
        if getattr(self.graph, 'closed_edits', None) is not None and self.graph.closed_edits[y, x]:
            raise ValueError('教师已封闭该位置，不能自动改用地物外沿')
        try:
            node = self.graph.snap(point, max_distance_px=4)
            if required_component is None or self.graph.components[node] == required_component:
                return {'node': node, 'pixel': point.tolist(), 'local': world[0].tolist(),
                        'method': 'manual' if control.get('approachPixel') else 'exact', 'offsetM': 0., 'automatic': False}
        except ValueError:
            pass
        if control.get('approachPixel'):
            raise ValueError('指定到访位置不可达，请核对门洞和到访侧')
        if self.semantics.get('restricted') is not None and self.semantics['restricted'][y, x]:
            raise ValueError('圈心位于禁入区域，不能自动移到外部道路')
        radius = control.get('radiusPx', 15)
        feature = None
        for kind in ('water', 'vegetation', 'point_ink', 'building'):
            feature = self._feature(kind, point, radius)
            if feature:
                break
        if feature is None:
            feature = self._ink_feature(point, radius)
        if feature is None:
            raise ValueError('圈心处的地物或通行侧不明确，请核对实际到访位置')
        if feature['method'] == 'point_symbol_edge' and self.semantics.get('restricted') is not None:
            # A small olive patch can have JPEG holes around its black point
            # symbol. Broad angular evidence enclosing the symbol is still
            # a forbidden area; a one-pixel gap must not license an automatic
            # approach. An area on only one side does not satisfy this check.
            reach = max(4, min(14, round(max(feature['symbol'].shape)/2+3)))
            x0, y0 = max(0, x-reach), max(0, y-reach)
            x1, y1 = min(self.graph.costs.shape[1], x+reach+1), min(self.graph.costs.shape[0], y+reach+1)
            yy, xx = np.mgrid[y0:y1, x0:x1]
            dx, dy = xx-point[0], yy-point[1]
            annulus = (dx*dx+dy*dy <= reach*reach) & (dx*dx+dy*dy >= 4)
            area = (self.semantics['restricted'][y0:y1, x0:x1] > 0) & annulus
            if area.sum() > .2*max(1, annulus.sum()):
                sectors = np.floor((np.arctan2(dy[area], dx[area])+np.pi)*4/np.pi).astype(int) % 8
                if len(np.unique(sectors)) >= 7:
                    raise ValueError('目标地物被禁入区域包围，请核对点位；不能跨禁区自动接入道路')
        feature = self._complete_outline(feature, radius)
        nearby = self.graph.tree.query_ball_point(point, feature['limitM']/max(self.scale, .1)+2)
        if not feature.get('stroke'):
            nearby = [n for n in nearby if not self._in_feature(feature, self.graph.pixels[[n]])[0]]
        candidates = []
        for node in nearby:
            offset = float(np.linalg.norm(self.graph.local[node]-world[0]))
            if offset > feature['limitM']:
                continue
            end = self.graph.pixels[node]
            pixels = segment_pixels(point, end)
            # This ray associates a target with its approach; it is NEVER a
            # route edge. Only the target's bounded silhouette may be crossed.
            # Connected obstacle blobs are not object identities: a tree can
            # touch a wall and a building can touch a forbidden garden.
            over_symbol = self._over_symbol(feature, pixels)
            hard = self.graph.costs[pixels[:, 1], pixels[:, 0]] == 0
            if np.any(hard & ~over_symbol):
                continue
            restricted = self.semantics.get('restricted')
            closed = getattr(self.graph, 'closed_edits', None)
            if ((restricted is not None and np.any(restricted[pixels[:, 1], pixels[:, 0]])) or
                    (closed is not None and np.any(closed[pixels[:, 1], pixels[:, 0]]))):
                continue
            # A circle drawn in the hollow of a compound symbol legitimately
            # leaves and re-enters the symbol's own arc; the class check above
            # and the side check below already exclude a hidden obstacle, so a
            # re-entry is not by itself evidence of crossing a wall.
            candidates.append((offset, int(node)))
        if not candidates:
            restricted = self.semantics.get('restricted')
            if restricted is not None:
                reach = max(2, round(feature['limitM']/max(self.scale, .1)))
                patch = restricted[max(0, y-reach):y+reach+1, max(0, x-reach):x+reach+1]
                if patch.size and np.mean(patch) > .45:
                    raise ValueError('目标地物被禁入区域包围，请核对点位；不能跨禁区自动接入道路')
            raise ValueError('地物外沿没有无障碍的近距离到访位置')
        # Local topology, not just global connectivity: the two sides of a
        # wall can connect far away and still be ambiguous at this control.
        local_radius = max(8, int(np.ceil(feature['limitM']*2/max(self.scale, .1))),
                           max(feature['symbol'].shape)+3)
        x0, y0 = max(0, x-local_radius), max(0, y-local_radius)
        x1, y1 = min(self.graph.costs.shape[1], x+local_radius+1), min(self.graph.costs.shape[0], y+local_radius+1)
        local_free = (self.graph.costs[y0:y1, x0:x1] > 0).astype(np.uint8)
        _, local_labels = cv2.connectedComponents(local_free, connectivity=4)
        side_of = {n: int(local_labels[round(self.graph.pixels[n, 1])-y0, round(self.graph.pixels[n, 0])-x0]) for _, n in candidates}
        if feature['method'] in ('point_symbol_edge', 'small_water_feature_edge', 'compound_point_symbol_edge'):
            # The white holes inside a printed sculpture/fountain are not
            # ground on another approach side. Keep the local exterior of
            # the complete point symbol; both sides of a wall still qualify
            # as exterior and therefore remain ambiguous below.
            exterior = set(np.concatenate((local_labels[0], local_labels[-1],
                                           local_labels[:, 0], local_labels[:, -1])).tolist())
            candidates = [(d, n) for d, n in candidates if side_of[n] in exterior]
            if not candidates:
                raise ValueError('地物符号外部没有可确认的到访位置')
        groups = {}
        for distance, node in candidates:
            groups.setdefault(side_of[node], []).append((distance, node))
        if len(groups) != 1:
            raise ValueError('地物附近存在不同通行侧，需要选择实际到访侧')
        if required_component is not None:
            candidates = [(d, n) for d, n in candidates if self.graph.components[n] == required_component]
        if not candidates:
            raise ValueError('合法到访侧与起点不连通，请核对通道')
        offset, node = min(candidates)
        return {'node': node, 'pixel': self.graph.pixels[node].astype(float).tolist(),
                'local': self.graph.local[node].tolist(), 'method': feature['method'],
                'featureKind': feature['kind'], 'offsetM': offset, 'automatic': True,
                'reviewRequired': True, 'targetPixel': list(control['pixel'])}
