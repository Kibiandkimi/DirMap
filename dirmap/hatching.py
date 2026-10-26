"""Recognize the supplied map family's grey/white lower-level hatching.

Only repeated, long, narrow neutral-grey stripes with light ground on both
sides are removed. Solid roofs, black boundaries/pillars, coloured fills and
purple restrictions remain separate obstacles in the terrain pipeline.
"""
import cv2
import numpy as np


def _shift(mask, dx, dy):
    result = np.zeros_like(mask)
    h, w = mask.shape
    sx, tx = slice(max(0, -dx), min(w, w-dx)), slice(max(0, dx), min(w, w+dx))
    sy, ty = slice(max(0, -dy), min(h, h-dy)), slice(max(0, dy), min(h, h+dy))
    result[ty, tx] = mask[sy, sx]
    return result


def _diagonal_stripes(narrow, labels, count, minimum_length):
    """Components elongated along the hatch direction, in native pixels."""
    yy, xx = np.nonzero(narrow)
    index = labels[yy, xx]
    along, across = xx-yy, xx+yy
    order = np.argsort(index, kind='stable')
    index, along, across = index[order], along[order], across[order]
    starts = np.r_[0, np.flatnonzero(np.diff(index))+1]
    present = index[starts]
    length = (np.maximum.reduceat(along, starts)-np.minimum.reduceat(along, starts)+1)/np.sqrt(2)
    width = (np.maximum.reduceat(across, starts)-np.minimum.reduceat(across, starts)+1)/np.sqrt(2)
    result = np.zeros(count, bool)
    result[present] = (length >= minimum_length) & (length >= 2*width)
    return result


def lower_level_hatching(image, controls):
    shape = image.shape[:2]
    empty = np.zeros(shape, np.uint8)
    if len(controls) < 2:
        return empty, empty.copy(), []
    radius = float(np.median([c.get('radiusPx', 15) for c in controls]))
    _, saturation, value = cv2.split(cv2.cvtColor(image, cv2.COLOR_BGR2HSV))
    grey = (saturation <= 48) & (value >= 125) & (value <= 225)
    light = (saturation <= 55) & (value >= 230)
    if not grey.any() or not light.any():
        return empty, empty.copy(), []
    half_width = max(1.3, radius*.07+.5)
    thin = cv2.distanceTransform(grey.astype(np.uint8), cv2.DIST_L2, cv2.DIST_MASK_PRECISE) <= half_width
    positive, negative = np.zeros(shape, bool), np.zeros(shape, bool)
    for offset in range(1, max(3, round(radius*.18))+1):
        positive |= _shift(light, offset, offset)
        negative |= _shift(light, -offset, -offset)
    dark = ((value < 110) & (saturation < 115)).astype(np.uint8)
    protected_edges = cv2.dilate(dark, np.ones((3, 3), np.uint8)) > 0
    narrow = (grey & thin & positive & negative & ~protected_edges).astype(np.uint8)
    length = max(11, round(radius*.65))
    kernel = np.fliplr(np.eye(length, dtype=np.uint8))
    lines = cv2.morphologyEx(narrow, cv2.MORPH_OPEN, kernel) > 0
    if not lines.any():
        return empty, empty.copy(), []
    seed = np.zeros(shape, bool)
    for period in range(2, max(5, round(radius*.6))+1):
        ahead, behind = _shift(lines, period, period), _shift(lines, -period, -period)
        seed |= lines & ((ahead & behind) | (ahead & _shift(lines, period*2, period*2))
                         | (behind & _shift(lines, -period*2, -period*2)))
    join = max(5, round(radius*.45)) | 1
    square = np.ones((join, join), np.uint8)
    envelopes = cv2.dilate(seed.astype(np.uint8), square)
    count, regions, stats, _ = cv2.connectedComponentsWithStats(envelopes, 8)
    keep = np.zeros(count, bool)
    for index in range(1, count):
        x, y, w, h, area = stats[index]
        if area < max(500, radius*radius*2) or min(w, h) < radius*1.5:
            continue
        yy, xx = np.nonzero(regions[y:y+h, x:x+w] == index)
        box = cv2.minAreaRect(np.column_stack((xx+x, yy+y)).astype(np.float32))
        if min(box[1]) >= max(18, radius):
            keep[index] = True
    supported = seed & keep[regions]
    if not supported.any():
        return empty, empty.copy(), []
    count, labels = cv2.connectedComponents(narrow, connectivity=8)
    selected = np.zeros(count, bool)
    selected[np.unique(labels[supported])] = True
    # A confirmed texture continues into adjacent bands that are too narrow
    # or broken to confirm by themselves, such as the strip between two
    # roofs. Only stripes elongated in the same direction may join, and only
    # through a chain of gaps no wider than the confirmed envelope.
    stripes = _diagonal_stripes(narrow, labels, count, max(4., radius*.25))
    candidates = (stripes | selected)[labels]
    candidates[labels == 0] = False
    chains, groups = cv2.connectedComponents(cv2.dilate(candidates.astype(np.uint8), square), connectivity=8)
    connected = np.zeros(chains, bool)
    connected[np.unique(groups[supported])] = True
    connected[0] = False
    selected[np.unique(labels[candidates & connected[groups]])] = True
    selected[0] = False
    accepted = selected[labels] & grey
    area = cv2.morphologyEx((accepted | keep[regions]).astype(np.uint8), cv2.MORPH_CLOSE, square) > 0
    features = []
    contours, _ = cv2.findContours(area.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for contour in contours:
        if cv2.contourArea(contour) < max(500, radius*radius*2):
            continue
        polygon = cv2.approxPolyDP(contour, max(2., radius*.1), True)[:, 0].astype(float).tolist()
        features.append({'kind': 'lower_level', 'polygon': polygon, 'source': 'repeated_grey_white_hatching'})
    return accepted.astype(np.uint8)*255, area.astype(np.uint8)*255, features
