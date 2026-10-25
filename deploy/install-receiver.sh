#!/usr/bin/env bash
# Run once on the existing server as its deployment user, with a CI public key.
set -euo pipefail
DIRMAP_DEPLOY_ROOT="${DIRMAP_DEPLOY_ROOT:-/opt/dirmap}"
[[ "$DIRMAP_DEPLOY_ROOT" =~ ^/[a-zA-Z0-9_/-]+$ ]]
DIRMAP_INSTALL_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
test -x "$DIRMAP_DEPLOY_ROOT/venv/bin/python"
test -L "$DIRMAP_DEPLOY_ROOT/current"
test -d "$DIRMAP_DEPLOY_ROOT/data"
sudo -n true
read -r key_type key_data _ < "${1:?CI public key file required}"
[[ "$key_type" == ssh-ed25519 && "$key_data" =~ ^[A-Za-z0-9+/=]+$ ]]
ssh-keygen -l -f "$1" > /dev/null
mkdir -p "$DIRMAP_DEPLOY_ROOT/bin" "$DIRMAP_DEPLOY_ROOT/releases"
install -m 644 "$DIRMAP_INSTALL_DIR/receiver.py" "$DIRMAP_DEPLOY_ROOT/bin/github-receiver.py.new"
mv -f "$DIRMAP_DEPLOY_ROOT/bin/github-receiver.py.new" "$DIRMAP_DEPLOY_ROOT/bin/github-receiver.py"
install -m 755 "$DIRMAP_INSTALL_DIR/run-server.sh" "$DIRMAP_DEPLOY_ROOT/bin/dirmap-run.new"
mv -f "$DIRMAP_DEPLOY_ROOT/bin/dirmap-run.new" "$DIRMAP_DEPLOY_ROOT/bin/dirmap-run"
printf '#!/bin/sh\nexport DIRMAP_DEPLOY_ROOT=%s\nexec /usr/bin/python3 -B %s/bin/github-receiver.py\n' \
  "$DIRMAP_DEPLOY_ROOT" "$DIRMAP_DEPLOY_ROOT" > "$DIRMAP_DEPLOY_ROOT/bin/dirmap-github-receive.new"
chmod 755 "$DIRMAP_DEPLOY_ROOT/bin/dirmap-github-receive.new"
mv -f "$DIRMAP_DEPLOY_ROOT/bin/dirmap-github-receive.new" "$DIRMAP_DEPLOY_ROOT/bin/dirmap-github-receive"

mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"
touch "$HOME/.ssh/authorized_keys"
chmod 600 "$HOME/.ssh/authorized_keys"
if ! grep -Fq " $key_data " "$HOME/.ssh/authorized_keys"; then
  printf '\nrestrict,command="%s/bin/dirmap-github-receive" %s %s github-actions:dirmap\n' \
    "$DIRMAP_DEPLOY_ROOT" "$key_type" "$key_data" >> "$HOME/.ssh/authorized_keys"
fi
sudo -n mkdir -p /etc/systemd/system/dirmap.service.d
printf '[Service]\nEnvironment=DIRMAP_DEPLOY_ROOT=%s\nExecStart=\nExecStart=%s/bin/dirmap-run\n' \
  "$DIRMAP_DEPLOY_ROOT" "$DIRMAP_DEPLOY_ROOT" | sudo -n tee /etc/systemd/system/dirmap.service.d/github-deploy.conf > /dev/null
sudo -n systemctl daemon-reload
printf '%s\n' 'Receiver installed and systemd reloaded. The next deployment will restart the application.'
