"""Coloured relief images of height models, for laying over course maps.

An image depends only on its model file, which is named after its content,
so it can be cached indefinitely. Image rows run north to south; cells
without observation support are transparent.
"""
from functools import lru_cache

import cv2
import numpy as np
from scipy.ndimage import distance_transform_edt

# Same ramp as the shared terrain page, low to high.
PALETTE = np.array([[66, 127, 107], [136, 164, 111], [205, 191, 128], [171, 116, 79], [116, 77, 61]], float)
MAX_SIDE = 1024


def contour_step(low, high):
    span = high-low
    return 10. if span > 60 else 5. if span > 25 else 2.


def relief(height, spacing, max_side=MAX_SIDE):
    """RGBA image for a node grid whose row 0 is the southern edge."""
    grid = np.flipud(np.asarray(height, float))
    valid = np.isfinite(grid)
    rows, columns = grid.shape
    image = np.zeros((rows, columns, 4), np.uint8)
    if not valid.any():
        return image
    low, high = float(grid[valid].min()), float(grid[valid].max())
    factor = int(max(1, min(4, max_side//max(rows, columns))))
    # Interpolation must not pull the unknown value into the edge of the
    # surface: carry the nearest known height outward first.
    nearest = distance_transform_edt(~valid, return_distances=False, return_indices=True)
    filled = grid[tuple(nearest)]
    size = (columns*factor, rows*factor)
    surface = cv2.resize(filled, size, interpolation=cv2.INTER_LINEAR)
    support = cv2.resize(valid.astype(np.float32), size, interpolation=cv2.INTER_LINEAR) >= .5
    pixel = spacing/factor
    # Light from the north-west, slopes exaggerated so a campus shows relief.
    north, east = np.gradient(surface*3, pixel)
    slope = np.arctan(np.hypot(east, north))
    aspect = np.arctan2(-north, east)
    azimuth, altitude = np.radians(315.), np.radians(45.)
    shade = np.sin(altitude)*np.cos(slope)+np.cos(altitude)*np.sin(slope)*np.cos(azimuth-np.pi/2-aspect)
    position = np.clip((surface-low)/max(high-low, 1.), 0, .99999)*(len(PALETTE)-1)
    index = np.floor(position).astype(int)
    weight = (position-index)[..., None]
    colour = PALETTE[index]*(1-weight)+PALETTE[index+1]*weight
    colour *= (.6+.4*np.clip(shade, 0, 1))[..., None]
    step = contour_step(low, high)
    level = np.floor(surface/step)
    line = np.zeros(level.shape, bool)
    line[:, 1:] |= level[:, 1:] != level[:, :-1]
    line[1:] |= level[1:] != level[:-1]
    index_line = np.zeros(level.shape, bool)
    major = np.floor(surface/(5*step))
    index_line[:, 1:] |= major[:, 1:] != major[:, :-1]
    index_line[1:] |= major[1:] != major[:-1]
    colour[line] = colour[line]*.62+np.array([38, 58, 46])*.38
    colour[index_line] = colour[index_line]*.35+np.array([34, 52, 40])*.65
    result = np.zeros((size[1], size[0], 4), np.uint8)
    result[..., :3] = np.clip(colour, 0, 255).astype(np.uint8)
    result[..., 3] = np.where(support, 255, 0)
    return result


@lru_cache(maxsize=16)
def relief_png(path, modified, fallback_spacing=10.):
    """PNG bytes of a model file; `modified` keys the cache to the file.

    Models record their grid spacing; the earliest ones did not, and only
    the shading depends on it.
    """
    with np.load(path, allow_pickle=False) as data:
        height = data['height']
        spacing = float(data['spacing']) if 'spacing' in data.files else fallback_spacing
    rgba = relief(height, spacing)
    ok, encoded = cv2.imencode('.png', cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGRA))
    if not ok:
        raise ValueError('高程图生成失败')
    return encoded.tobytes()
