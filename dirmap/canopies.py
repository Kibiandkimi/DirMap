"""Thin outlines of light-grey, traversable covered areas.

A pale passage through a darker building can have a thin black outline at
both open ends. Keep real walls and roof interiors; connect only opposite
free sides, with a supported pale-grey region on at least one side.
"""
import math

import cv2
import numpy as np


def _opening_tile(value, labels, blocked, buildings, forbidden, width_limit, thin_radius, guard):
    pale = labels > 0
    free = blocked == 0
    to_pale = cv2.distanceTransform((~pale).astype(np.uint8), cv2.DIST_L2, cv2.DIST_MASK_PRECISE)
    to_free = cv2.distanceTransform((~free).astype(np.uint8), cv2.DIST_L2, cv2.DIST_MASK_PRECISE)
    dark = (value < 110).astype(np.uint8)
    thickness = cv2.distanceTransform(dark, cv2.DIST_L2, cv2.DIST_MASK_PRECISE)
    thick = cv2.dilate((thickness > thin_radius).astype(np.uint8),
                       cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (guard*2+1,)*2)) > 0
    roof = cv2.erode(buildings.astype(np.uint8), np.ones((3, 3), np.uint8)) > 0
    near_dark = cv2.dilate(dark, np.ones((3, 3), np.uint8)) > 0
    candidates = ((blocked > 0) & (value < 196) & near_dark & ~thick & ~roof & (forbidden == 0)
                  & (to_pale+to_free <= width_limit))
    yy, xx = np.nonzero(candidates)
    opposite = np.zeros(len(xx), bool)
    # Distance alone can see both colours around a corner on the SAME side.
    # Require opposite free samples belonging to different pale regions (or
    # one pale region and open ground). An isolated bench inside one grey
    # region therefore remains an obstacle.
    height, width = pale.shape
    for dx, dy in ((1, 0), (0, 1), (1, 1), (-1, 1)):
        for step in range(1, math.ceil(width_limit/2)+2):
            px, py, mx, my = xx+dx*step, yy+dy*step, xx-dx*step, yy-dy*step
            valid = ((px >= 0) & (py >= 0) & (mx >= 0) & (my >= 0)
                     & (px < width) & (mx < width) & (py < height) & (my < height))
            at = np.flatnonzero(valid)
            first, second = labels[py[at], px[at]], labels[my[at], mx[at]]
            opposite[at] |= (free[py[at], px[at]] & free[my[at], mx[at]] & (first != second)
                             & ((first > 0) | (second > 0)))
    result = np.zeros(pale.shape, np.uint8)
    result[yy[opposite], xx[opposite]] = 1
    return result


def open_boundaries(image, blocked, buildings, forbidden, controls, tile_size=512):
    if not isinstance(tile_size, int) or tile_size < 8:
        raise ValueError('灰区检查分块大小无效')
    radii = [c['radiusPx'] for c in controls if c.get('role', 'control') == 'control']
    radius = float(np.median(radii)) if radii else 15.
    hue, saturation, value = cv2.split(cv2.cvtColor(image, cv2.COLOR_BGR2HSV))
    del hue
    pale = ((saturation < 48) & (value >= 196) & (value <= 235)).astype(np.uint8)
    core = cv2.erode(pale & (saturation < 24), np.ones((3, 3), np.uint8))
    del saturation
    if not core.any():
        return np.zeros(pale.shape, np.uint8), 0
    count, labels, stats, _ = cv2.connectedComponentsWithStats(pale, 8)
    supported = np.bincount(labels[core > 0], minlength=count) > 0
    supported &= stats[:, cv2.CC_STAT_AREA] >= max(24, radius*radius*.02)
    supported[0] = False
    labels[~supported[labels]] = 0
    area_count = int(supported.sum())
    del pale, core, stats, supported
    result = np.zeros(value.shape, np.uint8)
    if not area_count:
        return result, 0
    width_limit = max(3.5, radius/30*1.6+2.25)
    thin_radius = max(1.5, radius*.025+.75)
    guard = int(math.ceil(thin_radius))+1
    # Retain global region identities, including connections outside a tile.
    # Only the distance fields are local. The halo exceeds every distance,
    # morphology and opposite-side probe used for a pixel in the core tile.
    # Component-length filtering happens once after all core tiles are joined.
    halo = math.ceil(width_limit)+math.ceil(thin_radius)+guard+2
    height, width = value.shape
    for top in range(0, height, tile_size):
        bottom = min(height, top+tile_size)
        for left in range(0, width, tile_size):
            right = min(width, left+tile_size)
            x0, y0 = max(0, left-halo), max(0, top-halo)
            x1, y1 = min(width, right+halo), min(height, bottom+halo)
            box = np.s_[y0:y1, x0:x1]
            if not labels[box].any():
                continue
            tile = _opening_tile(value[box], labels[box], blocked[box], buildings[box], forbidden[box], width_limit, thin_radius, guard)
            result[top:bottom, left:right] = tile[top-y0:bottom-y0, left-x0:right-x0]
    count, labels, stats, _ = cv2.connectedComponentsWithStats(result, 8)
    keep = np.maximum(stats[:, cv2.CC_STAT_WIDTH], stats[:, cv2.CC_STAT_HEIGHT]) >= max(4., radius*.12)
    keep[0] = False
    return keep[labels].astype(np.uint8)*255, area_count
