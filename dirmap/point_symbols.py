"""Recover compact black map symbols over green print and JPEG colour fringes.

Near black, chroma noise can produce high HSV saturation: a black stone can
become a hollow green/olive ring. Reclassify only a bounded point symbol in
otherwise open ground. Every old obstacle pixel remains an obstacle.
"""
import cv2
import numpy as np


def recover_point_ink(image, masks, controls):
    radii = [c['radiusPx'] for c in controls if c.get('role', 'control') == 'control']
    radius = float(np.median(radii)) if radii else 15.
    h, s, v = cv2.split(cv2.cvtColor(image, cv2.COLOR_BGR2HSV))
    # Saturation is ill-conditioned near zero intensity, so it cannot veto
    # these very dark cores. Size/shape and the surrounding print still must
    # establish a point symbol, rather than a long wall or dense vegetation.
    _, labels, stats, _ = cv2.connectedComponentsWithStats((v < 70).astype(np.uint8), 8)
    recovered = np.zeros(v.shape, np.uint8)
    count = 0
    padding = max(1, min(3, round(radius*.06)))
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (padding*2+1,)*2)
    for index, (x, y, width, height, area) in enumerate(stats[1:], 1):
        if (area < 5 or min(width, height) < 3 or max(width, height) > max(6., radius*.45)
                or max(width, height) > min(width, height)*2.5 or area < width*height*.5):
            continue
        if x <= padding or y <= padding or x+width+padding >= image.shape[1] or y+height+padding >= image.shape[0]:
            continue
        margin = max(width, height)+padding*2
        x0, y0 = max(0, x-margin), max(0, y-margin)
        x1, y1 = min(image.shape[1], x+width+margin), min(image.shape[0], y+height+margin)
        box = np.s_[y0:y1, x0:x1]
        core = (labels[box] == index).astype(np.uint8)
        halo = cv2.dilate(core, kernel) > 0
        green = (masks['green'][box] > 0) & (masks['olive'][box] == 0) & (v[box] >= 159)
        paving = (h[box] <= 25) & (s[box] <= 100) & (v[box] >= 205)
        if green[~halo].mean() < .25 or (green | paving)[~halo].mean() < .6:
            continue
        # Real forbidden olive fills have stable colour support. A print
        # fringe has a sharp colour/intensity gradient around the dark core.
        # Keep small filled areas too, and protect the adjoining pixel rim.
        colour = image[box].astype(np.float32)
        mean = cv2.blur(colour, (3, 3))
        variance = np.maximum(cv2.blur(colour*colour, (3, 3))-mean*mean, 0)
        stable = cv2.erode(masks['olive'][box], np.ones((3, 3), np.uint8)) > 0
        stable &= variance.max(axis=2) < 12**2
        stable = cv2.dilate(stable.astype(np.uint8), np.ones((3, 3), np.uint8)) > 0
        if (stable & halo).any() or ((masks['building'][box] | masks['water'][box]) & halo).any():
            continue
        old_ink = (masks['black'][box] | masks['dark_green'][box] | masks['olive'][box]) > 0
        owned = (core > 0) | (old_ink & halo)
        recovered[box][owned] = 255
        count += 1
    return recovered, count
