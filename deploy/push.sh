#!/usr/bin/env bash
# Upload one tested artifact. The server key accepts only the deploy protocol.
set -euo pipefail
: "${DEPLOY_HOST:?}" "${DEPLOY_PORT:?}" "${DEPLOY_USER:?}"
: "${DEPLOY_SSH_KEY:?}" "${DEPLOY_KNOWN_HOSTS:?}" "${DEPLOY_URL:?}"
: "${GITHUB_SHA:?}" "${GITHUB_RUN_ID:?}" "${GITHUB_RUN_ATTEMPT:?}"
[[ "$DEPLOY_HOST" =~ ^[a-zA-Z0-9.-]+$ && "$DEPLOY_USER" =~ ^[a-zA-Z_][a-zA-Z0-9_.-]*$ ]]
[[ "$DEPLOY_PORT" =~ ^[0-9]+$ && "$GITHUB_SHA" =~ ^[0-9a-f]{40}$ ]]
[[ "$GITHUB_RUN_ID" =~ ^[0-9]+$ && "$GITHUB_RUN_ATTEMPT" =~ ^[0-9]+$ ]]
test -f "${1:?release archive required}"
umask 077
DIRMAP_SSH_DIR="$(mktemp -d "${RUNNER_TEMP:-/tmp}/dirmap-ci-ssh.XXXXXX")"
trap 'rm -rf -- "$DIRMAP_SSH_DIR"' EXIT
printf '%s\n' "$DEPLOY_SSH_KEY" > "$DIRMAP_SSH_DIR/key"
printf '%s\n' "$DEPLOY_KNOWN_HOSTS" > "$DIRMAP_SSH_DIR/known_hosts"
unset DEPLOY_SSH_KEY DEPLOY_KNOWN_HOSTS
ssh-keygen -y -P '' -f "$DIRMAP_SSH_DIR/key" > /dev/null
DIRMAP_ARCHIVE_SHA="$(sha256sum "$1" | cut -d ' ' -f 1)"
ssh -F /dev/null -i "$DIRMAP_SSH_DIR/key" -p "$DEPLOY_PORT" \
  -o BatchMode=yes -o IdentitiesOnly=yes -o ConnectTimeout=20 \
  -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$DIRMAP_SSH_DIR/known_hosts" \
  -o ServerAliveInterval=15 -o ServerAliveCountMax=12 \
  "$DEPLOY_USER@$DEPLOY_HOST" "deploy $GITHUB_SHA $GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT $DIRMAP_ARCHIVE_SHA" < "$1"

# Confirm the running public service, not just the current symlink. A public
# network failure does not roll back an internally healthy release.
python3 -B - <<'PY'
import json, os, time
from urllib.parse import urlsplit
from urllib.request import urlopen
url = os.environ['DEPLOY_URL'].rstrip('/')
if urlsplit(url).scheme != 'https':
    raise SystemExit('DEPLOY_URL must use trusted HTTPS')
revision = os.environ['GITHUB_SHA']
for attempt in range(12):
    try:
        with urlopen(url+'/api/health', timeout=10) as response:
            body = json.load(response)
        if body.get('status') == 'ok' and body.get('revision') == revision:
            break
    except (OSError, ValueError):
        pass
    time.sleep(5)
else:
    raise SystemExit('Public HTTPS did not report this commit; the server may still be healthy internally. Check the deployment log.')
print('Public HTTPS is healthy and serves commit '+revision)
if os.environ.get('GITHUB_STEP_SUMMARY'):
    with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
        summary.write(f'Deployed `{revision}` to [{url}]({url}).\n\nPublic HTTPS and the running commit were verified. Teaching data was retained.\n')
PY
