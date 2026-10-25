#!/usr/bin/env python3
"""Inventory original assets without decoding or displaying raster pixels.

Standard library only. pdfinfo/pdftotext are optional external metadata/text
readers. No OCR, thumbnail generation, downloads, or image model is used.
"""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess


import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dirmap.image_metadata import exif_summary, image_header


def inspect(root):
    assets = []
    for path in sorted(root.iterdir()):
        if not path.is_file() or path.suffix.lower() not in (".jpg", ".jpeg", ".png", ".pdf"):
            continue
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(128 * 1024), b""):
                digest.update(chunk)
        item = {"file": path.name, "bytes": path.stat().st_size,
                "sha256": digest.hexdigest()}
        if path.suffix.lower() == ".pdf":
            item["format"] = "PDF"
            if shutil.which("pdfinfo"):
                output = subprocess.run(["pdfinfo", str(path)], check=True,
                                        capture_output=True, text=True, timeout=15)
                item["pdfinfo"] = output.stdout.strip().splitlines()
            if shutil.which("pdftotext"):
                output = subprocess.run(["pdftotext", "-layout", str(path), "-"],
                                        check=True, capture_output=True, text=True, timeout=15)
                item["extractable_non_whitespace_characters"] = len("".join(output.stdout.split()))
            item["visual_content_verified"] = False
        else:
            item.update(image_header(path))
            item["uncompressed_u8_native_mib_estimate"] = round(
                item["width_px"] * item["height_px"] * item["channels"] / 1024**2, 3)
            item["visual_content_verified"] = False
        assets.append(item)
    return {"method": "file hashes; raster headers; optional PDF metadata and text only",
            "pixel_decoding": False, "ocr": False, "network_requests": False,
            "asset_count": len(assets), "total_bytes": sum(a["bytes"] for a in assets),
            "assets": assets}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = json.dumps(inspect(args.root), ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
    print(output, end="")


if __name__ == "__main__":
    main()
