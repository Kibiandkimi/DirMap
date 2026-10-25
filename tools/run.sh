#!/usr/bin/env bash
set -euo pipefail
DIRMAP_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIRMAP_ROOT"
source tools/runtime-env.sh
if [ ! -f web/dist/index.html ]; then npm run build; fi
exec python3 -B -m uvicorn dirmap.app:app --host "${DIRMAP_HOST:-127.0.0.1}" --port "${DIRMAP_PORT:-8765}"
