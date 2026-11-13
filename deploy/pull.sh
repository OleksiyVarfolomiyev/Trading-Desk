#!/usr/bin/env bash
# Run by desk-deploy.timer every two minutes, as the desk user: when main has moved, pull it,
# update the Python packages if their lists changed, rebuild the page and restart the relay.
# The trading daemon keeps running the code it started with; restart it by hand when a change
# should reach it (sudo systemctl restart desk-daemon), as on the Mac.
set -euo pipefail
cd "$(dirname "$0")/.."

git fetch -q origin main
if [ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ]; then
  exit 0
fi
before=$(git rev-parse HEAD)
git merge -q --ff-only origin/main
echo "deployed $(git rev-parse --short HEAD): $(git log -1 --format=%s)"
if ! git diff --quiet "$before" HEAD -- requirements-trading.txt requirements-dashboard.txt; then
  .venv/bin/pip install -q -r requirements-trading.txt -r requirements-dashboard.txt keyrings.alt
fi
bash web/build_site.sh >/dev/null
sudo /usr/bin/systemctl restart desk-relay
if ! git diff --quiet "$before" HEAD -- execution desk; then
  echo "trading code changed: the daemon still runs the earlier version until it is restarted"
fi
