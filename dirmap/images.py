"""Bounded image decoding with an explicit working-pixel coordinate frame."""
from pathlib import Path
import struct

import cv2
import numpy as np

MAX_SOURCE_PIXELS = 200_000_000
MAX_WORKING_SIDE = 4096


def read_map_image(path, target_size=None):
    from .image_metadata import image_header
    try:
        header = image_header(Path(path))
        width, height = header['width_px'], header['height_px']
    except (ValueError, OSError, KeyError, struct.error) as exc:
        raise ValueError('无法读取地图，支持 JPEG/PNG') from exc
    if width <= 0 or height <= 0 or width*height > MAX_SOURCE_PIXELS:
        raise ValueError('地图尺寸无效或超过两亿像素')
    orientation = header.get('exif_orientation') or 1
    if orientation not in range(1, 9):
        orientation = 1
    oriented_width, oriented_height = (height, width) if orientation in (5, 6, 7, 8) else (width, height)
    if target_size is None:
        scale = min(1., MAX_WORKING_SIDE/max(oriented_width, oriented_height))
        target_size = (max(1, round(oriented_width*scale)), max(1, round(oriented_height*scale)))
    target_width, target_height = map(int, target_size)
    if (target_width < 1 or target_height < 1 or target_width*target_height > 30_000_000
            or target_width > oriented_width or target_height > oriented_height):
        raise ValueError('地图工作像素范围无效，请重新导入原图')
    reduction = 1
    if header['format'] == 'JPEG':
        for factor in (8, 4, 2):
            if ((oriented_width+factor-1)//factor >= target_width
                    and (oriented_height+factor-1)//factor >= target_height):
                reduction = factor
                break
    flags = {1: cv2.IMREAD_COLOR, 2: cv2.IMREAD_REDUCED_COLOR_2,
             4: cv2.IMREAD_REDUCED_COLOR_4, 8: cv2.IMREAD_REDUCED_COLOR_8}[reduction]
    try:
        image = cv2.imdecode(np.fromfile(Path(path), dtype=np.uint8), flags | cv2.IMREAD_IGNORE_ORIENTATION)
    except cv2.error as exc:
        raise ValueError('无法解码地图，请检查 JPEG/PNG 文件是否完整') from exc
    if image is None:
        raise ValueError('无法解码地图，请检查 JPEG/PNG 文件是否完整')
    if orientation == 2:
        image = cv2.flip(image, 1)
    elif orientation == 3:
        image = cv2.rotate(image, cv2.ROTATE_180)
    elif orientation == 4:
        image = cv2.flip(image, 0)
    elif orientation == 5:
        image = cv2.transpose(image)
    elif orientation == 6:
        image = cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)
    elif orientation == 7:
        image = cv2.flip(cv2.transpose(image), -1)
    elif orientation == 8:
        image = cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE)
    if image.shape[1::-1] != (target_width, target_height):
        image = cv2.resize(image, (target_width, target_height), interpolation=cv2.INTER_AREA)
    sx, sy = oriented_width/target_width, oriented_height/target_height
    info = {'encodedSize': [width, height], 'orientedSourceSize': [oriented_width, oriented_height],
            'workingSize': [target_width, target_height], 'exifOrientation': orientation,
            'decoderReduction': reduction, 'resized': [target_width, target_height] != [oriented_width, oriented_height],
            'pixelFrame': 'working_image_after_exif_orientation',
            'workingToSource': [[sx, 0, .5*sx-.5], [0, sy, .5*sy-.5]]}
    return image, info
