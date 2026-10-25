"""JPEG/PNG headers for runtime decoding; standard-library-only metadata."""
import struct


def exif_summary(segment):
    result = {"exif_orientation": None, "exif_gps_ifd_present": False}
    if not segment.startswith(b"Exif\x00\x00"):
        return result
    tiff = segment[6:]
    if len(tiff) < 8 or tiff[:2] not in (b"II", b"MM"):
        return result
    endian = "<" if tiff[:2] == b"II" else ">"
    if struct.unpack_from(endian + "H", tiff, 2)[0] != 42:
        return result
    offset = struct.unpack_from(endian + "I", tiff, 4)[0]
    if offset + 2 > len(tiff):
        return result
    count = struct.unpack_from(endian + "H", tiff, offset)[0]
    for index in range(min(count, 4096)):
        position = offset + 2 + index * 12
        if position + 12 > len(tiff):
            break
        tag, kind, size = struct.unpack_from(endian + "HHI", tiff, position)
        if tag == 274 and kind == 3 and size == 1:
            result["exif_orientation"] = struct.unpack_from(endian + "H", tiff, position + 8)[0]
        if tag == 34853:
            result["exif_gps_ifd_present"] = True
    return result


def image_header(path):
    with path.open("rb") as stream:
        signature = stream.read(8)
        if signature == b"\x89PNG\r\n\x1a\n":
            length, kind = struct.unpack(">I4s", stream.read(8))
            if length != 13 or kind != b"IHDR":
                raise ValueError("Invalid PNG IHDR")
            width, height, depth, color, _, _, _ = struct.unpack(">IIBBBBB", stream.read(13))
            channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
            return {"format": "PNG", "width_px": width, "height_px": height,
                    "bit_depth": depth, "channels": channels,
                    "metadata_scope": "IHDR only; PNG EXIF not inspected"}
        stream.seek(0)
        if stream.read(2) != b"\xff\xd8":
            raise ValueError("Unsupported image signature")
        result = {"format": "JPEG", "exif_orientation": None,
                  "exif_gps_ifd_present": False,
                  "metadata_scope": "SOF and APP1 headers before SOS"}
        sof_markers = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                       0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
        while True:
            byte = stream.read(1)
            if byte != b"\xff":
                raise ValueError("Invalid JPEG marker")
            code = stream.read(1)
            while code == b"\xff":
                code = stream.read(1)
            if not code:
                raise ValueError("Truncated JPEG")
            marker = code[0]
            if marker in (0xDA, 0xD9):  # Stop BEFORE compressed pixel data.
                break
            if marker == 0x01 or 0xD0 <= marker <= 0xD8:
                continue
            raw_length = stream.read(2)
            if len(raw_length) != 2:
                raise ValueError("Truncated segment length")
            length = struct.unpack(">H", raw_length)[0]
            if length < 2:
                raise ValueError("Invalid JPEG segment length")
            segment = stream.read(length - 2)
            if len(segment) != length - 2:
                raise ValueError("Truncated JPEG segment")
            if marker == 0xE1 and segment.startswith(b"Exif\x00\x00"):
                result.update(exif_summary(segment))
            if marker in sof_markers:
                depth, height, width, channels = struct.unpack_from(">BHHB", segment)
                result.update(width_px=width, height_px=height,
                              bit_depth=depth, channels=channels)
        if "width_px" not in result:
            raise ValueError("JPEG has no supported SOF header")
        return result
