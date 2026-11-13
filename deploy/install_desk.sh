#!/usr/bin/env bash
# Install or update the desk code and services on the server, as root (after deploy/setup_server.sh
# and after the GitHub deploy key is added to the repository):
#
#   ssh root@SERVER 'bash -s' < deploy/install_desk.sh
#
# As the desk user: clones the repository, makes the Python environment, builds the page. As root:
# installs the systemd services (deploy/systemd). It starts the relay and the timers; the trading
# daemon is enabled for boot but its first start is by hand (sudo systemctl start desk-daemon),
# once the state files and the HL key are in place. Run it again after a change in deploy/systemd.
set -euo pipefail

REPO_URL=${REPO_URL:-git@github.com:OleksiyVarfolomiyev/Trading-Desk.git}  # or https://github.com/... while the repository is public and no deploy key is added
REPO=/home/desk/trading-desk

sudo -u desk -H bash -euo pipefail <<EOF
if [ ! -d "$REPO/.git" ]; then git clone "$REPO_URL" "$REPO"; fi
cd "$REPO"
git pull --ff-only
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements-trading.txt -r requirements-dashboard.txt keyrings.alt
bash web/build_site.sh
grep -q "deploy/desk.env" ~/.profile || printf '\n# The desk settings (deploy/desk.env), as the services have them\nset -a; . ~/trading-desk/deploy/desk.env; set +a\ncd ~/trading-desk\n' >> ~/.profile
EOF

install -m 644 "$REPO"/deploy/systemd/* /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now desk-relay.service desk-daily.timer desk-deploy.timer
systemctl enable desk-daemon.service
echo "Installed. Relay: $(systemctl is-active desk-relay). Daemon: $(systemctl is-active desk-daemon || true) (first start by hand)."
