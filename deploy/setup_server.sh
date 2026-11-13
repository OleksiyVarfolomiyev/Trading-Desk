#!/usr/bin/env bash
# One-time setup of the desk server (Hetzner, Ubuntu 24.04). Run as root:
#
#   ssh root@SERVER 'bash -s' < deploy/setup_server.sh
#
# It makes the "desk" user, installs Python, Node and Caddy, opens only SSH and HTTPS, and installs
# the services (deploy/systemd). It does not start trading: the repository, the state files and the
# keys arrive in later steps (deploy/README.md), and the daemon starts only when you say so.
# Safe to run again.
set -euo pipefail

DESK_USER=desk
HOME_DIR=/home/$DESK_USER
REPO=$HOME_DIR/trading-desk

export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get upgrade -yq
apt-get install -yq python3 python3-venv python3-pip git curl ufw caddy unattended-upgrades nodejs

# Node 20+ for web/server.mjs (Ubuntu 24.04 ships 18; the relay needs global fetch, which 18 has, but stay current).
if ! node -e 'process.exit(Number(process.versions.node.split(".")[0]) >= 20 ? 0 : 1)'; then
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
  apt-get install -yq nodejs
fi

# The desk user: owns the code, the state and the keys; no password login.
id -u $DESK_USER >/dev/null 2>&1 || adduser --disabled-password --gecos "" $DESK_USER
install -d -m 700 -o $DESK_USER -g $DESK_USER $HOME_DIR/.ssh $HOME_DIR/.trading-desk
if [ -f /root/.ssh/authorized_keys ]; then  # the same SSH key that reaches root reaches desk
  install -m 600 -o $DESK_USER -g $DESK_USER /root/.ssh/authorized_keys $HOME_DIR/.ssh/authorized_keys
fi
# A read-only deploy key for GitHub (its public half goes into the repository's Deploy keys).
if [ ! -f $HOME_DIR/.ssh/github_deploy ]; then
  sudo -u $DESK_USER ssh-keygen -q -t ed25519 -N "" -C "desk server deploy key" -f $HOME_DIR/.ssh/github_deploy
fi
cat > $HOME_DIR/.ssh/config <<'EOF'
Host github.com
  IdentityFile ~/.ssh/github_deploy
  IdentitiesOnly yes
  StrictHostKeyChecking accept-new
EOF
chown $DESK_USER:$DESK_USER $HOME_DIR/.ssh/config
chmod 600 $HOME_DIR/.ssh/config

# SSH by key only; HTTPS for the dashboard; nothing else.
sed -i 's/^#\?PasswordAuthentication .*/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl reload ssh || systemctl reload sshd || true
ufw allow OpenSSH
ufw allow 80/tcp     # Caddy's certificate challenge and the redirect to HTTPS
ufw allow 443/tcp
ufw --force enable

# The desk's commands may restart the desk services without a root password.
cat > /etc/sudoers.d/desk <<'EOF'
desk ALL=(root) NOPASSWD: /usr/bin/systemctl start desk-*, /usr/bin/systemctl stop desk-*, /usr/bin/systemctl restart desk-*, /usr/bin/systemctl status desk-*
EOF
chmod 440 /etc/sudoers.d/desk

# HTTPS on a free sslip.io name built from this server's IPv4 address, e.g. 5-161-1-2.sslip.io.
IP=$(curl -fsS -4 https://ifconfig.me || hostname -I | awk '{print $1}')
HOST="${IP//./-}.sslip.io"
cat > /etc/caddy/Caddyfile <<EOF
$HOST {
  encode gzip
  reverse_proxy 127.0.0.1:8787
}
EOF
systemctl reload caddy || systemctl restart caddy

timedatectl set-timezone UTC
echo
echo "Server ready. Dashboard address: https://$HOST"
echo "GitHub deploy key (add it to the repository as a read-only Deploy key):"
cat $HOME_DIR/.ssh/github_deploy.pub
