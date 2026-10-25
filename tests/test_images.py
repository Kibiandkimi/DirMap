import hashlib
import struct

import cv2
import numpy as np
import pytest

from dirmap.images import read_map_image


def exif_jpeg(image, orientation):
    data = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 98])[1].tobytes()
    tiff = b'II'+struct.pack('<HIH', 42, 8, 1)+struct.pack('<HHIHHI', 274, 3, 1, orientation, 0, 0)
    exif = b'Exif\0\0'+tiff
    return data[:2]+b'\xff\xe1'+struct.pack('>H', len(exif)+2)+exif+data[2:]


def test_exif_working_frame_matches_displayed_orientation_and_original_stays_intact(tmp_path):
    image = np.zeros((40, 80, 3), np.uint8)
    image[:20, :40] = [10, 30, 230]
    image[20:, 40:] = [230, 40, 10]
    path = tmp_path/'rotated.jpg'; raw = exif_jpeg(image, 6); path.write_bytes(raw)
    result, info = read_map_image(path)
    assert result.shape[:2] == (80, 40)
    assert info['encodedSize'] == [80, 40] and info['orientedSourceSize'] == [40, 80]
    assert info['workingSize'] == [40, 80] and info['exifOrientation'] == 6
    assert result[10, 30, 2] > 200 and result[70, 10, 0] > 200
    assert path.read_bytes() == raw


def test_large_jpeg_is_decoded_at_reduced_resolution_with_a_reversible_pixel_frame(tmp_path):
    image = np.full((5000, 6000, 3), 240, np.uint8)
    image[1000:1400, 2000:2400] = [20, 50, 210]
    path = tmp_path/'large.jpg'; cv2.imwrite(str(path), image)
    original_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    result, info = read_map_image(path, (3000, 2500))
    assert result.shape[:2] == (2500, 3000) and info['decoderReduction'] == 2
    point = np.array([1099.75, 599.75, 1])
    np.testing.assert_allclose(np.array(info['workingToSource']) @ point, [2200, 1200])
    assert result[600, 1100, 2] > 180
    assert hashlib.sha256(path.read_bytes()).hexdigest() == original_hash


def test_extreme_image_dimensions_are_rejected_before_decompression(tmp_path):
    path = tmp_path/'too-large.png'
    path.write_bytes(b'\x89PNG\r\n\x1a\n'+struct.pack('>I4sIIBBBBB', 13, b'IHDR', 60000, 60000, 8, 2, 0, 0, 0))
    with pytest.raises(ValueError, match='尺寸'):
        read_map_image(path)
