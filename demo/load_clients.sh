#!/usr/bin/env bash
# Generate traffic tagged as three distinct clients, so GraphOS Studio's Client
# Insights has something to show beyond a single "unidentified client" bucket.
# Also fires the addProduct mutation, so Studio sees a mutation alongside the
# queries — Insights groups by operation, and a schema with only Query ops
# makes for a thin Insights view.
#
# Clients (name / version), each with its own query mix:
#   web-storefront@1.4.2      Products, ProductById  (read-heavy, browsing)
#   mobile-ios@3.0.0          OrdersWithItems         (the entity-join path)
#   internal-admin-tool@0.9.1 addProduct mutation     (writes)
#
# Usage: ./demo/load_clients.sh [iterations-per-client]   (default 15)
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
[ -f "$ROOT/.env" ] && { set -a; . "$ROOT/.env"; set +a; }

ROUTER="http://127.0.0.1:${ROUTER_PORT:-4000}/"
N="${1:-15}"

post() {  # client_name client_version query [variables_json] -> prints trace id
  local name="$1" version="$2" query="$3" vars="${4:-}"
  # `${4:-{}}` looks like a safe default but isn't: bash doesn't brace-match
  # inside parameter expansion, so it closes at the first `}` and leaves a
  # stray `}` appended whenever $4 IS passed — which broke every JSON body
  # that included variables. Two-step default avoids the ambiguity.
  [ -z "$vars" ] && vars='{}'
  curl -s -D /tmp/dt-demo-headers -o /tmp/dt-demo-body \
    -X POST "$ROUTER" \
    -H 'Content-Type: application/json' \
    -H "apollographql-client-name: ${name}" \
    -H "apollographql-client-version: ${version}" \
    -d "$(python3 -c '
import json, sys
print(json.dumps({"query": sys.argv[1], "variables": json.loads(sys.argv[2])}))
' "$query" "$vars")"
  grep -i '^apollo-trace-id:' /tmp/dt-demo-headers | tr -d '\r' | awk '{print $2}'
}

echo "Sending ${N} iterations per client (3 clients) to ${ROUTER}"
last_trace=""
for i in $(seq 1 "$N"); do
  # web-storefront: browsing queries
  post "web-storefront" "1.4.2" \
    'query Products { products { id title price } }' >/dev/null
  last_trace=$(post "web-storefront" "1.4.2" \
    'query ProductById($id: ID!) { product(id: $id) { id title price } }' \
    "{\"id\": \"product:$(( (i % 3) + 1 ))\"}")

  # mobile-ios: the entity-join path
  post "mobile-ios" "3.0.0" \
    'query OrdersWithItems { orders { id total items { id title price } } }' >/dev/null

  # internal-admin-tool: writes
  post "internal-admin-tool" "0.9.1" \
    'mutation AddProduct($title: String!, $price: Float!) { addProduct(title: $title, price: $price) { id title price } }' \
    "{\"title\": \"Demo Item ${i}\", \"price\": $(( (i % 5) * 10 + 5 )).99}" >/dev/null

  printf '.'
done
echo

cat <<SUMMARY

Traffic sent: $((N * 3)) requests across 3 clients (web-storefront@1.4.2,
mobile-ios@3.0.0, internal-admin-tool@0.9.1). Last trace id: ${last_trace:-<none>}

Studio Insights takes a similar batch-plus-ingest delay to Dynatrace. Check:
  https://studio.apollographql.com/graph/${APOLLO_GRAPH_REF%%@*}/variant/${APOLLO_GRAPH_REF##*@}/insights

In Dynatrace, find that trace:
  fetch spans | filter trace.id == "${last_trace}"
SUMMARY
