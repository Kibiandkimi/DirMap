#!/usr/bin/env bash
# Used by systemd. Legacy releases retain the original shared environment.
set -euo pipefail
DIRMAP_DEPLOY_ROOT="${DIRMAP_DEPLOY_ROOT:-/opt/dirmap}"
DIRMAP_RUNTIME="$DIRMAP_DEPLOY_ROOT/current/.venv/bin/python"
if [[ ! -x "$DIRMAP_RUNTIME" ]]; then
  DIRMAP_RUNTIME="$DIRMAP_DEPLOY_ROOT/venv/bin/python"
fi
cd "$DIRMAP_DEPLOY_ROOT/current"
exec "$DIRMAP_RUNTIME" -B -m uvicorn dirmap.app:app --host 127.0.0.1 --port "${DIRMAP_APP_PORT:-8765}" \
  --proxy-headers --forwarded-allow-ips=127.0.0.1
