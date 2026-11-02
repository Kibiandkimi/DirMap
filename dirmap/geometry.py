"""Explicit WGS84/local metre coordinates and an invertible registration mesh."""

from dataclasses import dataclass
from collections import Counter, defaultdict
import copy
import math
from types import SimpleNamespace

import cv2
import numpy as np
from scipy.spatial import Delaunay


@dataclass(frozen=True)
class Frame:
    lng: float = 113.9304
    lat: float = 22.535

    @property
    def scales(self):
        phi = math.radians(self.lat)
        e2 = 0.00669437999014
        n = 6378137 / math.sqrt(1 - e2 * math.sin(phi) ** 2)
        m = 6378137 * (1-e2) / (1-e2 * math.sin(phi) ** 2) ** 1.5
        return np.array([n * math.cos(phi), m]) * math.pi / 180

    def to_local(self, lnglat):
        points = np.asarray(lnglat, dtype=float)
        if not np.isfinite(points).all() or np.any(np.abs(points[..., 0]) > 180) or np.any(np.abs(points[..., 1]) > 90):
            raise ValueError("经纬度无效")
        return (points - [self.lng, self.lat]) * self.scales

    def to_lnglat(self, xy):
        return np.asarray(xy, dtype=float) / self.scales + [self.lng, self.lat]

    def as_dict(self):
        return {"crs": "WGS84", "origin": [self.lng, self.lat], "unit": "metre", "axes": "east_north"}

    @classmethod
    def of(cls, state):
        """The frame a saved map's local metres were measured in.

        Each place has its own origin; maps saved before places existed
        carry the original campus origin, which is also the default.
        """
        origin = ((state or {}).get("frame") or {}).get("origin")
        return cls(float(origin[0]), float(origin[1])) if origin else cls()


def mercator(lnglat):
    points = np.asarray(lnglat, dtype=float)
    lat = np.clip(points[..., 1], -85.05112878, 85.05112878)
    return np.stack([np.radians(points[..., 0]) * 6378137,
                     np.log(np.tan(np.pi / 4 + np.radians(lat) / 2)) * 6378137], axis=-1)


def unmercator(xy):
    points = np.asarray(xy, dtype=float)
    return np.stack([np.degrees(points[..., 0] / 6378137),
                     np.degrees(2 * np.arctan(np.exp(points[..., 1] / 6378137)) - np.pi / 2)], axis=-1)


def affine_points(matrix, points):
    points = np.asarray(points, dtype=float)
    return points @ np.asarray(matrix)[:, :2].T + np.asarray(matrix)[:, 2]


def fit_affine(source, target, threshold=15):
    source, target = np.asarray(source, dtype=float), np.asarray(target, dtype=float)
    if len(source) < 3 or source.shape != target.shape or not np.isfinite(source).all() or not np.isfinite(target).all():
        raise ValueError("至少需要三组有效的同名点")
    if np.linalg.matrix_rank(source - source.mean(axis=0)) < 2:
        raise ValueError("配准点不能共线")
    cv2.setRNGSeed(7)
    matrix, inliers = cv2.estimateAffine2D(source, target, method=cv2.RANSAC,
                                        ransacReprojThreshold=threshold, maxIters=4000,
                                        confidence=0.999, refineIters=20)
    if matrix is None or abs(np.linalg.det(matrix[:, :2])) < 1e-10:
        raise ValueError("无法得到稳定配准")
    return matrix, inliers.ravel().astype(bool)


class Registration:
    """Global affine plus a smoothed, nonfolding piecewise affine correction.

    Pixel y is down; local y is north. Signed areas are compared to the
    global affine determinant, so a deliberate axis reflection is allowed.
    Points outside the measured hull remain explicitly uncalibrated.
    """

    def __init__(self, affine, source=None, target=None, regularization=1.5):
        self.affine = np.asarray(affine, dtype=float)
        if self.affine.shape != (2, 3) or not np.isfinite(self.affine).all() or abs(np.linalg.det(self.affine[:, :2])) < 1e-10:
            raise ValueError("配准矩阵无效")
        self.source = np.asarray(source if source is not None else [], dtype=float).reshape(-1, 2)
        self.target = np.asarray(target if target is not None else [], dtype=float).reshape(-1, 2)
        self.tri = None
        self._inverse_grid = None
        self._inverse_domain = None
        self._forward_inverse = None
        self.folded = 0
        if len(self.source) >= 3:
            if len(self.source) != len(self.target) or not np.isfinite(self.source).all() or not np.isfinite(self.target).all():
                raise ValueError("配准网格点无效")
            if len(np.unique(self.source, axis=0)) != len(self.source):
                raise ValueError("配准点不能重复")
            self.tri = Delaunay(self.source)
            lap = np.zeros((len(self.source), len(self.source)))
            edges = set()
            for triangle in self.tri.simplices:
                for i, j in zip(triangle, np.roll(triangle, -1)):
                    edges.add(tuple(sorted((int(i), int(j)))))
            for i, j in edges:
                lap[i, i] += 1
                lap[j, j] += 1
                lap[i, j] -= 1
                lap[j, i] -= 1
            base = affine_points(self.affine, self.source)
            delta = self.target - base
            self.target = base + np.linalg.solve(np.eye(len(self.source)) + regularization * lap, delta)
            reference_sign = np.sign(np.linalg.det(self.affine[:, :2]))
            for ids in self.tri.simplices:
                a, b = self.source[ids], self.target[ids]
                area_a = np.linalg.det(np.stack((a[1]-a[0], a[2]-a[0])))
                area_b = np.linalg.det(np.stack((b[1]-b[0], b[2]-b[0])))
                if abs(area_b) < 1e-5 or np.sign(area_a * area_b) != reference_sign:
                    self.folded += 1
            # A locally orientation-preserving mesh with a simple, convex
            # boundary has no nonlocal overlap. Reject a nonconvex boundary
            # instead of silently accepting a possibly self-intersecting warp.
            hull = cv2.convexHull(self.source.astype(np.float32), returnPoints=False).ravel()
            boundary = self.target[hull].astype(np.float32)
            if self.folded or not cv2.isContourConvex(boundary):
                raise ValueError("局部配准出现折叠或边界重叠，请修正同名点")

    def forward(self, points, strict=True):
        points = np.asarray(points, dtype=float).reshape(-1, 2)
        result = affine_points(self.affine, points)
        valid = np.zeros(len(points), dtype=bool)
        if self.tri is not None and not hasattr(self.tri, 'find_simplex'):
            if self._forward_inverse is None:
                reverse = copy.copy(self)
                reverse.source, reverse.target = self.target, self.source
                reverse._inverse_grid = None
                self._forward_inverse = reverse
            projected = self._forward_inverse.inverse(points)
            valid = np.isfinite(projected).all(axis=1)
            result[valid] = projected[valid]
            if strict:
                result[~valid] = np.nan
            return result, valid
        if self.tri is not None:
            simplex = self.tri.find_simplex(points)
            valid = simplex >= 0
            transforms = self.tri.transform[simplex[valid]]
            bary = np.einsum('nij,nj->ni', transforms[:, :2], points[valid] - transforms[:, 2])
            weights = np.column_stack((bary, 1-bary.sum(axis=1)))
            result[valid] = np.einsum('ni,nij->nj', weights, self.target[self.tri.simplices[simplex[valid]]])
        if strict:
            result[~valid] = np.nan
        return result, valid

    def inverse(self, points, index_bounds=None):
        """Invert the same triangles, optionally indexing a smaller query window.

        Display-only exterior triangles can extend far beyond the paper.
        Indexing their entire bounding box puts most image pixels in a few
        crowded buckets. A window changes only the search index; queries
        outside it still use the full mesh, and no triangles are refitted.
        """
        points = np.asarray(points, dtype=float).reshape(-1, 2)
        result = np.full_like(points, np.nan)
        if self.tri is None:
            return result
        domain = None
        if index_bounds is not None:
            bounds = np.asarray(index_bounds, dtype=float)
            if bounds.shape != (2, 2) or not np.isfinite(bounds).all() or np.any(bounds[1] <= bounds[0]):
                raise ValueError('配准查询范围无效')
            domain = tuple(bounds.ravel())
        if self._inverse_grid is None or self._inverse_domain != domain:
            lower, upper = self.target.min(axis=0)-1e-6, self.target.max(axis=0)+1e-6
            if domain is not None:
                lo, hi = np.maximum(lower, bounds[0]-1e-6), np.minimum(upper, bounds[1]+1e-6)
                if np.all(hi > lo):
                    lower, upper = lo, hi
            span = upper-lower
            cell_count = max(1, len(self.tri.simplices)//3)
            shape = np.clip(np.rint(np.sqrt(cell_count)*np.sqrt(span/span[::-1])).astype(int), 1, 64)
            buckets = {}
            transforms, anchors = [], []
            for triangle, ids in enumerate(self.tri.simplices):
                vertices = self.target[ids]
                a, b, c = vertices
                transforms.append(np.linalg.inv(np.column_stack((a-c, b-c))))
                anchors.append(c)
                lo, hi = np.floor((np.array([vertices.min(axis=0), vertices.max(axis=0)])-lower)/span*shape).astype(int)
                lo, hi = np.maximum(0, lo), np.minimum(shape-1, hi)
                for y in range(lo[1], hi[1]+1):
                    for x in range(lo[0], hi[0]+1):
                        buckets.setdefault(int(y*shape[0]+x), []).append(triangle)
            self._inverse_grid = lower, span, shape, buckets, np.array(transforms), np.array(anchors)
            self._inverse_domain = domain
        lower, span, shape, buckets, transforms, anchors = self._inverse_grid
        finite = np.isfinite(points).all(axis=1)
        candidates = np.flatnonzero(finite & np.all((points >= lower) & (points <= lower+span), axis=1))
        cells = np.minimum(shape-1, np.floor((points[candidates]-lower)/span*shape).astype(int))
        keys = cells[:, 1]*shape[0]+cells[:, 0]
        order = np.argsort(keys)
        keys, candidates = keys[order], candidates[order]
        unique, offsets = np.unique(keys, return_index=True)
        for key, begin, end in zip(unique, offsets, np.r_[offsets[1:], len(keys)]):
            indexes = candidates[begin:end]
            for triangle in buckets.get(int(key), []):
                weights01 = (points[indexes]-anchors[triangle]) @ transforms[triangle].T
                weights = np.column_stack((weights01, 1-weights01.sum(axis=1)))
                keep = np.all(weights >= -1e-7, axis=1)
                result[indexes[keep]] = weights[keep] @ self.source[self.tri.simplices[triangle]]
        if domain is not None:
            outside = finite & np.any((points < lower) | (points > lower+span), axis=1)
            if outside.any():
                cached = self._inverse_grid, self._inverse_domain
                try:
                    result[outside] = self.inverse(points[outside])
                finally:
                    self._inverse_grid, self._inverse_domain = cached
        return result

    def for_display(self, width, height):
        """Continue the measured mesh to the whole paper, for display only.

        Keep every measured triangle exactly. A ring of exterior triangles
        carries its boundary displacement continuously towards the global
        affine fit. These invented vertices never enter saved registration,
        target calibration, field-check accuracy, or the routing graph.
        """
        corners = np.array([[0., 0.], [width-1., 0.], [width-1., height-1.], [0., height-1.]])
        if self.tri is None:
            return Registration(self.affine, corners, affine_points(self.affine, corners), regularization=0)
        counts = Counter(tuple(sorted((int(i), int(j)))) for ids in self.tri.simplices for i, j in zip(ids, np.roll(ids, -1)))
        neighbours = defaultdict(list)
        for (i, j), count in counts.items():
            if count == 1:
                neighbours[i].append(j); neighbours[j].append(i)
        # Keep collinear boundary vertices as well: their measured local
        # corrections may bend the geographic boundary between its corners.
        start = min(neighbours)
        hull, previous, current = [start], start, neighbours[start][0]
        while current != start:
            hull.append(current)
            following = next(i for i in neighbours[current] if i != previous)
            previous, current = current, following
        hull = np.asarray(hull, np.int32)
        boundary = self.source[hull]
        centre = boundary.mean(axis=0)
        edges = np.roll(boundary, -1, axis=0)-boundary
        delta = centre-boundary
        distances = np.abs(edges[:, 0]*delta[:, 1]-edges[:, 1]*delta[:, 0])/np.linalg.norm(edges, axis=1)
        factor = max(2., np.linalg.norm(corners-centre, axis=1).max()/distances.min()*1.1)
        count = len(self.source)
        outer = np.arange(count, count+len(hull))
        triangles = np.array([*self.tri.simplices, *[
            triangle for i, j in zip(range(len(hull)), np.roll(np.arange(len(hull)), -1))
            for triangle in ([hull[i], hull[j], outer[i]], [outer[i], hull[j], outer[j]])]], dtype=np.int32)
        for _ in range(8):
            ring = centre+(boundary-centre)*factor
            source = np.vstack((self.source, ring))
            target = np.vstack((self.target, affine_points(self.affine, ring)))
            a, b = source[triangles], target[triangles]
            area_a = np.linalg.det(np.stack((a[:, 1]-a[:, 0], a[:, 2]-a[:, 0]), axis=1))
            area_b = np.linalg.det(np.stack((b[:, 1]-b[:, 0], b[:, 2]-b[:, 0]), axis=1))
            if np.all(area_a*area_b*np.linalg.det(self.affine[:, :2]) > 0):
                display = copy.copy(self)
                display.source, display.target = source, target
                display.tri = SimpleNamespace(simplices=triangles)
                display._inverse_grid = display._forward_inverse = None
                return display
            factor *= 2
        raise ValueError('配准外推出现折叠，请检查边界同名点')

    def image_extent_points(self, width, height):
        """Exact extrema of a piecewise affine image rectangle, before CRS conversion."""
        limit = np.array([width-1., height-1.])
        inside = np.all((self.source >= 0) & (self.source <= limit), axis=1)
        corners = np.array([[0., 0.], [limit[0], 0.], limit, [0., limit[1]]])
        points = [*self.target[inside], *self.forward(corners)[0]]
        edges = {tuple(sorted((int(i), int(j)))) for ids in self.tri.simplices for i, j in zip(ids, np.roll(ids, -1))}
        for i, j in edges:
            a, delta = self.source[i], self.source[j]-self.source[i]
            for axis in (0, 1):
                if abs(delta[axis]) < 1e-10:
                    continue
                for value in (0., limit[axis]):
                    fraction = (value-a[axis])/delta[axis]
                    other = a[1-axis]+fraction*delta[1-axis]
                    if 0 <= fraction <= 1 and 0 <= other <= limit[1-axis]:
                        points.append(self.target[i]+fraction*(self.target[j]-self.target[i]))
        return np.asarray(points)

    def as_dict(self):
        return {"affine": self.affine.tolist(), "source": self.source.tolist(),
                "target": self.target.tolist(), "triangles": self.tri.simplices.tolist() if self.tri is not None else [],
                "model": "regularized_piecewise_affine", "foldedTriangles": self.folded}

    @classmethod
    def from_dict(cls, value):
        return cls(value['affine'], value['source'], value['target'], regularization=0)
