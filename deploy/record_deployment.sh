#!/usr/bin/env bash
# Record when these commits became active for a particular component.
set -euo pipefail
cd "$(dirname "$0")/.."
component=$1
case "$component" in desk|daemon) ;; *) exit 2 ;; esac
before=${2:-}
head=$(git rev-parse HEAD)
if [[ -n "$before" && "$before" != "$head" ]]; then
  commits=$(git rev-list "$before..$head")
else
  commits=$head
fi
mkdir -p data
umask 077
at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
while IFS= read -r commit; do
  printf '{"component":"%s","commit":"%s","at":"%s"}\n' "$component" "$commit" "$at"
done <<< "$commits" >> data/deployments.jsonl
