#!/usr/bin/env bash
# Assemble the web dashboard into web/site: the page and data builder from web/, and the local
# dashboard's own renderer and styles from desk/dashboard_static, so both always look the same.
# The desk server runs this on every pull (deploy/pull.sh); run it yourself to try the site locally.
set -euo pipefail
cd "$(dirname "$0")/.."
rm -rf web/site
mkdir -p web/site
cp web/index.html web/live.js desk/dashboard_static/app.js desk/dashboard_static/app.css web/site/
echo "web/site: $(ls web/site | tr '\n' ' ')"
