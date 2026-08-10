#!/usr/bin/env bash
# Generate traffic so every instrument has data:
#
#   products          plain root-field query, one subgraph fetch
#   orders + items    entity join -> products `_entities` fetch (the good span)
#   boom              GraphQL error with HTTP 200 -> error instruments,
#                     otel.status_code = ERROR on the span
#   ?fail=1           subgraph HTTP 500 -> subgraph error path
#   bad query         router-side validation failure (4xx)
#
# Usage: ./demo/load.sh [iterations]   (default 20)
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
[ -f "$ROOT/.env" ] && { set -a; . "$ROOT/.env"; set +a; }

ROUTER="http://127.0.0.1:${ROUTER_PORT:-4000}/"
N="${1:-20}"

post() {  # query -> prints the trace id
  curl -s -D /tmp/dt-demo-headers -o /tmp/dt-demo-body \
    -X POST "$ROUTER" -H 'Content-Type: application/json' \
    -d "{\"query\":$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$1")}"
  grep -i '^apollo-trace-id:' /tmp/dt-demo-headers | tr -d '\r' | awk '{print $2}'
}

echo "Sending ${N} iterations to ${ROUTER}"
last_trace=""
for i in $(seq 1 "$N"); do
  post 'query Products { products { id title price } }' >/dev/null
  last_trace=$(post 'query OrdersWithItems { orders { id total items { id title price } } }')
  post 'query ProductById { product(id: "product:2") { id title price } }' >/dev/null
  if [ $((i % 4)) -eq 0 ]; then
    post 'query Boom { boom }' >/dev/null                       # GraphQL error
  fi
  if [ $((i % 7)) -eq 0 ]; then
    curl -s -o /dev/null -X POST "$ROUTER" -H 'Content-Type: application/json' \
      -d '{"query":"query Nope { doesNotExist }"}'              # validation error
  fi
  printf '.'
done
echo

cat <<SUMMARY

Traffic sent. Last trace id: ${last_trace:-<none>}

Metrics take one batch interval (2s) plus Dynatrace ingest lag — usually under a
minute for traces, a few minutes for a metric key appearing for the first time.

  ./scripts/verify_ingest.sh

In Dynatrace, find that trace:
  fetch spans | filter trace.id == "${last_trace}"
SUMMARY
