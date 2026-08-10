#!/usr/bin/env bash
# Stop the demo: router, subgraphs, and the collector if it was started.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="$HERE/.run"

for name in router subgraphs; do
  pidfile="$RUN_DIR/${name}.pid"
  if [ -f "$pidfile" ]; then
    pid="$(cat "$pidfile")"
    if kill "$pid" 2>/dev/null; then echo "  stopped $name ($pid)"; fi
    rm -f "$pidfile"
  fi
done

if command -v docker >/dev/null 2>&1; then
  if [ -n "$(docker compose -f "$HERE/docker-compose.yaml" ps -q 2>/dev/null)" ]; then
    ( cd "$HERE" && docker compose down ) >/dev/null 2>&1 && echo "  stopped collector"
  fi
fi
echo "done"
