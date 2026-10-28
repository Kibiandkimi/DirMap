"""Recognize course symbols without erasing cartographic forbidden areas."""

import cv2
import numpy as np


def course_symbol(purple, control):
    center = np.array(control['pixel'], dtype=float)
    radius = float(control['radiusPx'])
    height, width = purple.shape
    pad = int(np.ceil(radius*2.1))
    origin = np.maximum(0, np.floor(center-pad).astype(int))
    end = np.minimum([width, height], np.ceil(center+pad).astype(int))
    crop = purple[origin[1]:end[1], origin[0]:end[0]]
    point = center-origin
    angles = np.linspace(0, 2*np.pi, 180, endpoint=False)
    radial = np.linspace(.5, 1.65, 93)*radius
    ring_points = point+radial[:, None, None]*np.stack((np.cos(angles), np.sin(angles)), axis=-1)
    xx = np.clip(np.rint(ring_points[..., 0]).astype(int), 0, crop.shape[1]-1)
    yy = np.clip(np.rint(ring_points[..., 1]).astype(int), 0, crop.shape[0]-1)
    profile = (crop[yy, xx] > 0).mean(axis=1)
    # Separate connected peaks, rather than counting the two edges of one
    # thick circle as a double finish ring.
    groups = np.split(np.flatnonzero(profile >= .65), np.flatnonzero(np.diff(np.flatnonzero(profile >= .65)) > 1)+1)
    radii = []
    for group in groups:
        if len(group):
            radii.append(float(radial[group[np.argmax(profile[group])]]))
    double = any(abs(a-b) > radius*.16 for a in radii for b in radii)
    distance = cv2.distanceTransform(255-crop, cv2.DIST_L2, 3)
    grid_y, grid_x = np.indices(crop.shape)
    inner = (grid_x-point[0])**2+(grid_y-point[1])**2 < (radius*.84)**2
    inner_density = np.mean(crop[inner] > 0) if inner.any() else 0
    best = None
    if .035 < inner_density < .8:
        t = np.linspace(.14, .86, 27)
        for angle in np.arange(-90, 30, 6)*np.pi/180:
            unit = np.stack((np.cos(angle+np.arange(3)*2*np.pi/3), np.sin(angle+np.arange(3)*2*np.pi/3)), axis=-1)
            for relative_radius in np.arange(.85, 1.71, .05):
                for dx, dy in ((0, 0), (-.07, 0), (.07, 0), (0, -.07), (0, .07)):
                    vertices = point+[dx*radius, dy*radius]+unit*radius*relative_radius
                    next_vertices = np.roll(vertices, -1, axis=0)
                    sides = vertices[:, None, :]+(next_vertices-vertices)[:, None, :]*t[None, :, None]
                    vectors = next_vertices-vertices
                    normal = np.stack((-vectors[:, 1], vectors[:, 0]), axis=-1)
                    normal /= np.linalg.norm(normal, axis=1)[:, None]
                    offset = normal[:, None, :]*max(4., radius*.22)

                    def hit(points, tolerance):
                        x = np.rint(points[..., 0]).astype(int)
                        y = np.rint(points[..., 1]).astype(int)
                        valid = (x >= 0) & (y >= 0) & (x < crop.shape[1]) & (y < crop.shape[0])
                        result = np.zeros(x.shape, dtype=bool)
                        result[valid] = distance[y[valid], x[valid]] <= tolerance
                        return result.mean(axis=1)

                    coverage = hit(sides, max(1., radius*.045))
                    if coverage.min() < .80:
                        continue
                    # Two thick circles can cover a triangular template's
                    # sides without containing a triangle. Require ink on
                    # each side beyond the angular-average ring evidence at
                    # the same radii; circle strokes alone have no such signal.
                    sx = np.clip(np.rint(sides[..., 0]).astype(int), 0, crop.shape[1]-1)
                    sy = np.clip(np.rint(sides[..., 1]).astype(int), 0, crop.shape[0]-1)
                    baseline = np.interp(np.linalg.norm(sides-point, axis=-1), radial, profile)
                    if np.min(((crop[sy, sx] > 0)-baseline).mean(axis=1)) < .08:
                        continue
                    background = (hit(sides+offset, .5)+hit(sides-offset, .5))/2
                    contrast = coverage-background
                    if contrast.min() < .24:
                        continue
                    score = float(np.mean(coverage)-.4*np.mean(background))
                    if best is None or score > best[0]:
                        best = (score, vertices+origin, coverage.tolist())
    if best is not None and not double:
        # In a low-resolution compound symbol, triangle strokes can bridge
        # the valley between the two radial peaks. The independently fitted
        # Hough ring is still evidence of the outer circle.
        outer_support = float(profile[np.argmin(np.abs(radial-radius))])
        if outer_support >= .65 and any(abs(r-radius) > radius*.2 for r in radii):
            radii.append(radius)
            double = True
    role = 'start_finish' if best is not None and double else 'start' if best is not None else 'finish' if double else 'control'
    return {'role': role, 'ringRadii': radii or [radius],
            'triangle': best[1].tolist() if best else None,
            'triangleCoverage': best[2] if best else None,
            'confidence': round(best[0], 3) if best else round(float(profile.max()), 3)}


def standalone_starts(purple, controls):
    """Find hollow start triangles independently of the circle detector.

    Circle scale bounds the search. Closed triangular holes only propose
    candidates; their straight centre lines are fitted to the original ink.
    Slight closing permits small scan gaps without inventing annotation ink.
    """
    radii = [c['radiusPx'] for c in controls if c.get('role') == 'control']
    if not radii:
        return []
    radius = float(np.median(radii))
    closing = max(1, min(5, round(radius * .08)))
    proposals = cv2.morphologyEx(purple, cv2.MORPH_CLOSE,
                               cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (closing * 2 + 1,) * 2))
    contours, hierarchy = cv2.findContours(proposals, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    if hierarchy is None:
        return []
    height, width = purple.shape
    found = []
    for index, contour in enumerate(contours):
        if hierarchy[0, index, 3] < 0:
            continue  # A filled triangle is not the outline of a start.
        perimeter = cv2.arcLength(contour, True)
        if not 4.5 * radius <= perimeter <= 9.5 * radius:
            continue
        # A low-resolution corner often has two vertices after polygon
        # approximation. Enclose the observed hole and require it to occupy
        # the triangle; circular holes and rectangles cannot pass this check.
        area, enclosing = cv2.minEnclosingTriangle(contour)
        if area <= 0 or cv2.contourArea(contour) / area < .82:
            continue
        polygon = enclosing.reshape(-1, 2).astype(float)
        lengths = np.linalg.norm(polygon - np.roll(polygon, -1, axis=0), axis=1)
        # Rasterized inside corners shrink and distort these seed vertices;
        # the stricter shape check uses the fitted stroke centre lines below.
        if not (1.3 * radius <= lengths.min() and lengths.max() <= 2.9 * radius and lengths.max() / lengths.min() <= 1.4):
            continue
        center = polygon.mean(axis=0)
        if any(np.linalg.norm(center - c['pixel']) < max(radius, c['radiusPx']) * .9 for c in [*controls, *found]):
            continue
        origin = np.maximum(0, np.floor(center - 3 * radius).astype(int))
        end = np.minimum([width, height], np.ceil(center + 3 * radius).astype(int))
        crop = purple[origin[1]:end[1], origin[0]:end[0]]
        normals, constants, widths = [], [], []
        # A hole's boundary is the inside edge of the stroke. Find the
        # centre of that stroke on many perpendicular rays along each side.
        extent = max(5., radius * .5 + 2)
        across = np.arange(-extent, extent + .25, .25)
        for a, b in zip(polygon, np.roll(polygon, -1, axis=0)):
            vector = b - a
            normal = np.array([vector[1], -vector[0]]) / np.linalg.norm(vector)
            if np.dot((a + b) / 2 - center, normal) < 0:
                normal = -normal
            along = a + (b - a) * np.linspace(.18, .82, 29)[:, None]
            rays = along[:, None, :] + normal * across[None, :, None] - origin
            samples = cv2.remap(crop, rays[..., 0].astype(np.float32), rays[..., 1].astype(np.float32),
                                cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT) > 127
            midpoints, thicknesses = [], []
            for row, sample_point in zip(samples, along):
                transitions = np.diff(np.r_[False, row, False].astype(int))
                runs = [(a, b) for a, b in zip(np.flatnonzero(transitions == 1), np.flatnonzero(transitions == -1))
                        if a > 0 and b < len(across) and .5 <= (b-a) * .25 <= max(5., radius * .3)]
                if not runs:
                    continue
                start, stop = min(runs, key=lambda run: abs(np.mean(across[run[0]:run[1]])))
                midpoints.append(sample_point + normal * np.mean(across[start:stop]))
                thicknesses.append((stop - start) * .25)
            if len(midpoints) < 20:
                break
            midpoints = np.asarray(midpoints)
            vx, vy, x, y = cv2.fitLine(midpoints.astype(np.float32), cv2.DIST_HUBER, 0, .01, .01).ravel()
            fitted_normal = np.array([vy, -vx], dtype=float)
            if np.dot(fitted_normal, normal) < 0:
                fitted_normal = -fitted_normal
            if np.quantile(np.abs((midpoints - [x, y]) @ fitted_normal), .9) > max(.8, radius * .035):
                break
            normals.append(fitted_normal)
            constants.append(float(np.dot(fitted_normal, [x, y])))
            widths.append(float(np.median(thicknesses)))
        if len(normals) != 3:
            continue
        try:
            vertices = np.array([np.linalg.solve(np.array([normals[i-1], normals[i]]),
                                                [constants[i-1], constants[i]]) for i in range(3)])
        except np.linalg.LinAlgError:
            continue
        sides = np.roll(vertices, -1, axis=0) - vertices
        lengths = np.linalg.norm(sides, axis=1)
        if (lengths.min() <= 0 or lengths.max() / lengths.min() > 1.2 or not 1.8 * radius <= lengths.mean() <= 2.9 * radius
                or np.any(vertices < 1) or np.any(vertices >= np.array([width, height]) - 1)):
            continue
        distance = cv2.distanceTransform(255 - crop, cv2.DIST_L2, 3)
        def support(points):
            local = np.rint(points - origin).astype(int)
            valid = (local[..., 0] >= 0) & (local[..., 1] >= 0) & (local[..., 0] < crop.shape[1]) & (local[..., 1] < crop.shape[0])
            result = np.zeros(valid.shape, bool)
            result[valid] = distance[local[..., 1][valid], local[..., 0][valid]] <= max(1., np.median(widths) / 2)
            return result.mean(axis=-1)
        coverage = support(vertices[:, None, :] + sides[:, None, :] * np.linspace(.04, .96, 40)[None, :, None])
        if coverage.min() < .85:
            continue
        # The counter of a letter A or a triangular gap in hatching has two
        # sides continuing past its lower corners. A start may have one
        # outgoing leg, but not these continuing strokes at several corners.
        continuing = 0
        for i, vertex in enumerate(vertices):
            extensions = np.array([vertex + (vertex - vertices[j]) * np.linspace(.12, .5, 20)[:, None]
                                   for j in range(3) if j != i])
            continuing += bool(np.max(support(extensions)) >= .75)
        if continuing > 1:
            continue
        center = vertices.mean(axis=0)
        geometry = {'role': 'start', 'ringRadii': [], 'triangle': vertices.tolist(),
                    'triangleCoverage': coverage.tolist(), 'strokeWidthPx': float(np.median(widths)),
                    'confidence': round(float(coverage.mean()), 3)}
        found.append({'id': f'start{len(found)+1}', 'pixel': center.tolist(), 'radiusPx': radius,
                      'confidence': geometry['confidence'], 'state': 'candidate', 'role': 'start',
                      'label': '起点', 'labelSource': 'course_symbol', 'required': False,
                      'serviceBaseSec': 5, 'servicePerPersonSec': 1, 'symbolGeometry': geometry})
    return found


def classify_course_symbols(purple, controls):
    for control in controls:
        symbol = course_symbol(purple, control)
        control['role'] = symbol['role']
        control['symbolGeometry'] = symbol
        if symbol['role'] != 'control':
            control['label'] = {'start': '起点', 'finish': '终点', 'start_finish': '起终点'}[symbol['role']]
            control['labelSource'] = 'course_symbol'
            control['required'] = False
    controls.extend(standalone_starts(purple, controls))


def course_ink_mask(purple, controls):
    """Mask detected symbol strokes, not the disk enclosed by a control."""
    strokes = np.zeros_like(purple)
    for control in controls:
        center = tuple(np.rint(control['pixel']).astype(int))
        radius = control['radiusPx']
        geometry = control.get('symbolGeometry', {})
        rings = [] if geometry.get('ringRadii') == [] else [radius, *geometry.get('ringRadii', [])]
        for ring in rings:
            cv2.circle(strokes, center, round(ring), 255, max(3, round(radius*.15)))
        if geometry.get('triangle'):
            triangle = np.rint(geometry['triangle']).astype(np.int32)
            thickness = max(3, round(radius*.16), int(np.ceil(geometry.get('strokeWidthPx', 0))) - 1)
            cv2.polylines(strokes, [triangle], True, 255, thickness)
    return cv2.bitwise_and(strokes, purple)
