#!/usr/bin/env bash
# Source this file; keep bulky dependencies off the constrained work volume.
DIRMAP_PYTHON_DIR="${DIRMAP_PYTHON_DIR:-/tmp/dirmap-python}"
DIRMAP_NODE_DIR="${DIRMAP_NODE_DIR:-/tmp/dirmap-node}"
DIRMAP_OCR_DIR="${DIRMAP_OCR_DIR:-/tmp/dirmap-ocr}"
export PYTHONPATH="$DIRMAP_PYTHON_DIR${PYTHONPATH:+:$PYTHONPATH}"
if [ -x "$DIRMAP_OCR_DIR/usr/bin/tesseract" ]; then
  export DIRMAP_TESSERACT="$DIRMAP_OCR_DIR/usr/bin/tesseract"
  export LD_LIBRARY_PATH="$DIRMAP_OCR_DIR/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
  export TESSDATA_PREFIX="$DIRMAP_OCR_DIR/usr/share/tesseract-ocr/5/tessdata"
fi
