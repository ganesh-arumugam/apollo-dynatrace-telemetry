#!/usr/bin/env bash
# End-to-end harness for the Dynatrace router telemetry template.
#
# Three layers, each one strictly stronger than the last:
#
#   1. static      — validate every template against the DT### rules
#   2. contract    — start the mock Dynatrace endpoint and prove it enforces the
#                    real endpoint's contract (path, Api-Token, content type)
#   3. live router — boot Apollo Router with the harness config pointed at the
#                    mock, send a real GraphQL operation, and assert that
#                    metrics AND traces actually arrived, and that the router's
#                    stdout carries trace ids (the only path logs can take)
#
# Layers 1 and 2 always run and always assert. Layer 3 runs only if a router
# binary is available; it SKIPs (loudly, with instructions) rather than lying.
#
# Router binary resolution order:  $ROUTER_BIN  ->  ./bin/router  ->  $PATH
#
# Usage: ./harness/run_harness.sh [--keep-going]
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
MOCK_PORT="${MOCK_PORT:-4318}"
SUBGRAPH_PORT="${SUBGRAPH_PORT:-4011}"
ROUTER_PORT="${ROUTER_PORT:-4010}"
ROUTER_HEALTH_PORT="${ROUTER_HEALTH_PORT:-8098}"
RECORD_FILE="${RECORD_FILE:-/tmp/dynatrace-harness-otlp.jsonl}"

PASS=0; FAIL=0; SKIP=0
pass() { echo "  PASS  $1"; PASS=$((PASS+1)); }
fail() { echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
skip() { echo "  SKIP  $1"; SKIP=$((SKIP+1)); }

MOCK_PID=""; SUBGRAPH_PID=""; ROUTER_PID=""
cleanup() {
  for pid in "$ROUTER_PID" "$SUBGRAPH_PID" "$MOCK_PID"; do
    [ -n "$pid" ] && kill "$pid" 2>/dev/null
  done
  wait 2>/dev/null
}
trap cleanup EXIT

wait_for_http() {  # url, seconds
  local deadline=$(( $(date +%s) + ${2:-20} ))
  until curl -sf "$1" >/dev/null 2>&1; do
    [ "$(date +%s)" -ge "$deadline" ] && return 1
    sleep 0.3
  done
}

echo "=============================================================="
echo " Layer 0 — preflight"
echo "=============================================================="
for var in OTEL_EXPORTER_OTLP_ENDPOINT OTEL_EXPORTER_OTLP_TRACES_ENDPOINT \
           OTEL_EXPORTER_OTLP_METRICS_ENDPOINT; do
  if [ -n "${!var:-}" ]; then
    fail "$var is set (${!var}) — router v2.12 and earlier silently redirect"
    echo "        telemetry to it; v2.13+ refuses to start. unset it and re-run."
  else
    pass "$var unset"
  fi
done
if python3 -c "import yaml" 2>/dev/null; then
  pass "python3 + pyyaml available"
else
  fail "pyyaml missing (pip3 install pyyaml)"
fi

echo
echo "=============================================================="
echo " Layer 1 — static validation of templates"
echo "=============================================================="
for cfg in "$ROOT"/templates/*.router.yaml; do
  if python3 "$ROOT/scripts/validate_dynatrace.py" "$cfg" >/tmp/validate.out 2>&1; then
    pass "$(basename "$cfg")"
  else
    fail "$(basename "$cfg")"
    sed 's/^/        /' /tmp/validate.out
  fi
done
if python3 "$ROOT/scripts/validate_dynatrace.py" --allow-loopback \
     "$HERE/harness.router.yaml" >/tmp/validate.out 2>&1; then
  pass "harness.router.yaml (loopback mode)"
else
  fail "harness.router.yaml (loopback mode)"; sed 's/^/        /' /tmp/validate.out
fi

# Collector topology: router side through the DT### rules in collector mode,
# collector side through the DTC### rules.
for cfg in "$ROOT"/templates/collector/*.router.yaml; do
  [ -e "$cfg" ] || continue
  if python3 "$ROOT/scripts/validate_dynatrace.py" --mode collector "$cfg" \
       >/tmp/validate.out 2>&1; then
    pass "$(basename "$cfg") (collector mode)"
  else
    fail "$(basename "$cfg") (collector mode)"; sed 's/^/        /' /tmp/validate.out
  fi
done
for cfg in "$ROOT"/templates/collector/otel-collector-*.y*ml; do
  [ -e "$cfg" ] || continue
  if python3 "$ROOT/scripts/validate_collector.py" "$cfg" >/tmp/validate.out 2>&1; then
    pass "$(basename "$cfg")"
  else
    fail "$(basename "$cfg")"; sed 's/^/        /' /tmp/validate.out
  fi
done

# The demo configs are what a customer actually runs, so gate them too.
if python3 "$ROOT/scripts/validate_dynatrace.py" "$ROOT/demo/router.direct.yaml" \
     >/tmp/validate.out 2>&1; then
  pass "demo/router.direct.yaml"
else
  fail "demo/router.direct.yaml"; sed 's/^/        /' /tmp/validate.out
fi
if python3 "$ROOT/scripts/validate_dynatrace.py" --mode collector \
     "$ROOT/demo/router.collector.yaml" >/tmp/validate.out 2>&1; then
  pass "demo/router.collector.yaml (collector mode)"
else
  fail "demo/router.collector.yaml (collector mode)"; sed 's/^/        /' /tmp/validate.out
fi
if python3 "$ROOT/scripts/validate_collector.py" "$ROOT/demo/otel-collector.yaml" \
     >/tmp/validate.out 2>&1; then
  pass "demo/otel-collector.yaml"
else
  fail "demo/otel-collector.yaml"; sed 's/^/        /' /tmp/validate.out
fi

if python3 "$ROOT/scripts/build_dashboard.py" --check >/tmp/validate.out 2>&1; then
  pass "dashboard JSON matches dashboards/tiles.yaml"
else
  fail "dashboard JSON is stale"; sed 's/^/        /' /tmp/validate.out
fi

echo
echo "=============================================================="
echo " Layer 2 — mock Dynatrace endpoint contract"
echo "=============================================================="
rm -f "$RECORD_FILE"
python3 "$HERE/mock_dynatrace.py" --port "$MOCK_PORT" --record "$RECORD_FILE" \
  >/tmp/mock-dynatrace.log 2>&1 &
MOCK_PID=$!
if wait_for_http "http://127.0.0.1:${MOCK_PORT}/_harness/health" 15; then
  pass "mock endpoint up on :${MOCK_PORT}"
else
  fail "mock endpoint failed to start"; sed 's/^/        /' /tmp/mock-dynatrace.log
  echo; echo "TOTAL: ${PASS} passed, ${FAIL} failed, ${SKIP} skipped"; exit 1
fi

code() {  # method path extra-curl-args...
  local method="$1" path="$2"; shift 2
  curl -s -o /dev/null -w '%{http_code}' -X "$method" \
    "http://127.0.0.1:${MOCK_PORT}${path}" "$@"
}

got=$(code POST /api/v2/otlp/v1/metrics \
        -H 'Authorization: Api-Token dt0c01.test' \
        -H 'Content-Type: application/x-protobuf' --data-binary 'payload')
[ "$got" = "200" ] && pass "valid metrics export accepted (200)" \
                   || fail "valid metrics export got $got, expected 200"

got=$(code POST /api/v2/otlp/v1/traces \
        -H 'Authorization: Bearer dt0c01.test' \
        -H 'Content-Type: application/x-protobuf' --data-binary 'payload')
[ "$got" = "401" ] && pass "Bearer token rejected (401)" \
                   || fail "Bearer token got $got, expected 401"

got=$(code POST /api/v2/otlp/v1/metrics \
        -H 'Content-Type: application/x-protobuf' --data-binary 'payload')
[ "$got" = "401" ] && pass "missing Authorization rejected (401)" \
                   || fail "missing Authorization got $got, expected 401"

got=$(code POST /v1/metrics \
        -H 'Authorization: Api-Token dt0c01.test' \
        -H 'Content-Type: application/x-protobuf' --data-binary 'payload')
[ "$got" = "404" ] && pass "wrong OTLP path rejected (404)" \
                   || fail "wrong OTLP path got $got, expected 404"

got=$(code POST /api/v2/otlp/v1/logs \
        -H 'Authorization: Api-Token dt0c01.test' \
        -H 'Content-Type: text/plain' --data-binary 'payload')
[ "$got" = "415" ] && pass "bad Content-Type rejected (415)" \
                   || fail "bad Content-Type got $got, expected 415"

accepted=$(curl -s "http://127.0.0.1:${MOCK_PORT}/_harness/stats" | jq -r '.total_accepted')
[ "$accepted" = "1" ] && pass "recorder counted exactly the 1 valid export" \
                      || fail "recorder counted $accepted, expected 1"

echo
echo "=============================================================="
echo " Layer 3 — live router export"
echo "=============================================================="
ROUTER=""
if [ -n "${ROUTER_BIN:-}" ] && [ -x "${ROUTER_BIN}" ]; then ROUTER="$ROUTER_BIN"
elif [ -x "$ROOT/bin/router" ]; then ROUTER="$ROOT/bin/router"
elif command -v router >/dev/null 2>&1; then ROUTER="$(command -v router)"
fi

if [ -z "$ROUTER" ]; then
  skip "no router binary found — layer 3 not run"
  echo "        Install one, then re-run:"
  echo "          curl -sSL https://router.apollo.dev/download/nix/latest | sh"
  echo "          ROUTER_BIN=./router ./harness/run_harness.sh"
else
  echo "  using router: $ROUTER ($("$ROUTER" --version 2>/dev/null | head -1))"
  curl -s -X POST "http://127.0.0.1:${MOCK_PORT}/_harness/reset" >/dev/null

  # Reuse the demo subgraphs so there is exactly one subgraph implementation in
  # the repo. The harness only needs the products one.
  python3 "$ROOT/demo/subgraphs.py" --only products --products-port "$SUBGRAPH_PORT" \
    >/tmp/harness-subgraph.log 2>&1 &
  SUBGRAPH_PID=$!
  sleep 0.5

  DYNATRACE_ENV_URL="http://127.0.0.1:${MOCK_PORT}" \
  DYNATRACE_API_TOKEN="dt0c01.harness" \
  APOLLO_ROUTER_LOG=info \
  "$ROUTER" --supergraph "$ROOT/demo/supergraph.graphql" \
            --config "$HERE/harness.router.yaml" \
            --dev >/tmp/router-harness.log 2>&1 &
  ROUTER_PID=$!

  if wait_for_http "http://127.0.0.1:${ROUTER_HEALTH_PORT}/health" 45; then
    pass "router started against mock Dynatrace endpoint"

    resp=$(curl -s -X POST "http://127.0.0.1:${ROUTER_PORT}/" \
             -H 'Content-Type: application/json' \
             -d '{"query":"{ products { id title } }"}')
    if echo "$resp" | jq -e '.data.products | length > 0' >/dev/null 2>&1; then
      pass "GraphQL operation served through the router"
    else
      fail "GraphQL operation failed: $resp"
    fi
    # An operation that errors, to drive the error instruments.
    curl -s -X POST "http://127.0.0.1:${ROUTER_PORT}/" \
      -H 'Content-Type: application/json' -d '{"query":"{ boom }"}' >/dev/null

    # batch_processor scheduled_delay is 1s in the harness config.
    deadline=$(( $(date +%s) + 30 )); stats='{}'
    while [ "$(date +%s)" -lt "$deadline" ]; do
      stats=$(curl -s "http://127.0.0.1:${MOCK_PORT}/_harness/stats")
      m=$(echo "$stats" | jq -r '.counts.metrics'); t=$(echo "$stats" | jq -r '.counts.traces')
      [ "${m:-0}" -gt 0 ] && [ "${t:-0}" -gt 0 ] && break
      sleep 1
    done
    # Metrics and traces only: the router has no OTLP log exporter (DT022), so a
    # logs assertion here could never pass. What it can prove is the mechanism
    # that does work — stdout JSON carrying a trace id for a log forwarder to
    # correlate on — which is asserted below.
    for signal in metrics traces; do
      n=$(echo "$stats" | jq -r ".counts.${signal}")
      if [ "${n:-0}" -gt 0 ]; then
        pass "${signal} export received by Dynatrace endpoint (${n} request(s))"
      else
        fail "no ${signal} export received — check /tmp/router-harness.log"
      fi
    done

    logs_n=$(echo "$stats" | jq -r '.counts.logs')
    if [ "${logs_n:-0}" -gt 0 ]; then
      fail "router posted to the logs endpoint (${logs_n}) — it has no OTLP log
        exporter, so this means something else is sending to it"
    else
      pass "no OTLP log export attempted (correct — the router cannot)"
    fi

    # The real log path: JSON on stdout with a trace id, which is what a
    # collector filelog receiver or host forwarder correlates to a span.
    if grep -q '"trace_id":"[0-9a-f]\{32\}"' /tmp/router-harness.log 2>/dev/null; then
      pass "router logged JSON with a 32-hex trace id (correlatable by a forwarder)"
    else
      fail "no trace id in the router's JSON stdout — logs would not correlate to
        spans in Dynatrace. Check logging.stdout.format.json.display_trace_id"
    fi
    rej=$(echo "$stats" | jq -r '.rejections | length')
    [ "${rej:-0}" = "0" ] && pass "router made zero malformed ingest calls" \
                          || { fail "router made ${rej} rejected call(s)"; echo "$stats" | jq '.rejections'; }
  else
    fail "router failed to become healthy in 45s"
    sed 's/^/        /' /tmp/router-harness.log | tail -30
  fi
fi

echo
echo "=============================================================="
echo "TOTAL: ${PASS} passed, ${FAIL} failed, ${SKIP} skipped"
echo "  router log : /tmp/router-harness.log"
echo "  ingest log : ${RECORD_FILE}"
echo "=============================================================="
[ "$FAIL" -eq 0 ]
