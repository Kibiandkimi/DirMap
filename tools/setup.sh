#!/usr/bin/env bash
set -euo pipefail
DIRMAP_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIRMAP_ROOT"
source tools/runtime-env.sh
mkdir -p "$DIRMAP_PYTHON_DIR" "$DIRMAP_NODE_DIR"
python3 -m pip install --no-cache-dir --target "$DIRMAP_PYTHON_DIR" --upgrade -r requirements.lock
python3 -B -c 'from dirmap.native import library; print("Shortest paths: native" if library() else "Shortest paths: SciPy fallback")'
if ! python3 -B tools/setup_ocr.py; then
  printf '%s\n' 'OCR is optional; labels will need review until Tesseract is installed.'
fi
source tools/runtime-env.sh
cp package.json package-lock.json "$DIRMAP_NODE_DIR/"
npm --prefix "$DIRMAP_NODE_DIR" ci --ignore-scripts --no-audit --no-fund --cache /tmp/dirmap-npm-cache
if [ ! -e node_modules ] && [ ! -L node_modules ]; then
  ln -s "$DIRMAP_NODE_DIR/node_modules" node_modules
elif [ -L node_modules ] && [ ! -e node_modules ]; then
  ln -sfn "$DIRMAP_NODE_DIR/node_modules" node_modules
fi
npm run build
python3 -B tools/prepare_maps.py
