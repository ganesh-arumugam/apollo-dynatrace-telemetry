#!/usr/bin/env bash
# Verify that router telemetry is actually landing in a REAL Dynatrace tenant.
#
# The harness (harness/run_harness.sh) proves the router exports correctly against
# a mock. This script answers the other half of the question — did Dynatrace keep
# it? — which is where delta temporality, token scopes, and metric registration
# actually bite.
#
# Checks, in order of how often each one is the culprit:
#   1. collector self-metrics (collector topology only): accepted vs send_failed
#   2. Dynatrace Metrics API: is the metric registered, does it have data points
#   3. token scope probe: a 401 here vs a 403 there tells you which scope is missing
#
# OTLP-ingested metrics are queried through the classic Metrics API with an `ext:`
# prefix (ext:http.server.request.duration), while Grail/DQL uses the plain dotted
# key. Both are correct in their own surface — mixing them up produces a
# "metric not found" that looks like an ingest failure.
#
# Two token families, and they serve different APIs:
#
#   dt0c01...  classic API token   -> Metrics API v2, header `Api-Token`
#   dt0s16...  platform token      -> Grail DQL,      header `Bearer`
#
# A platform token on the Metrics API returns 403, which reads like an ingest
# failure and is not one. Section 2 runs for a dt0c01 token, section 3 for a
# platform token; whichever you have, one of them proves the data landed.
#
# Env:
#   DT_ENVIRONMENT_ID   required, e.g. abc12345
#   DT_API_TOKEN        classic ingest/read token (metrics.ingest + metrics.read)
#   DT_PLATFORM_TOKEN   platform token for DQL (falls back to DT_API_TOKEN /
#                       DT_BEARER_TOKEN if either looks like dt0s16...)
#   COLLECTOR_METRICS   optional, default http://localhost:8888/metrics
#   METRICS             optional, space-separated metric keys to probe
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
COLLECTOR_METRICS="${COLLECTOR_METRICS:-http://localhost:8888/metrics}"
METRICS="${METRICS:-http.server.request.duration dynatrace.router.requests apollo.router.overhead}"

if [ -z "${DT_ENVIRONMENT_ID:-}" ] || [ -z "${DT_API_TOKEN:-}" ]; then
  if [ -f "$ROOT/.env" ]; then set -a; . "$ROOT/.env"; set +a; fi
fi

PASS=0; FAIL=0; SKIP=0
pass() { echo "  PASS  $1"; PASS=$((PASS+1)); }
fail() { echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
skip() { echo "  SKIP  $1"; SKIP=$((SKIP+1)); }

echo "=============================================================="
echo " 1. OTel Collector self-metrics"
echo "=============================================================="
COLLECTOR_OUT=$(curl -s --max-time 5 "$COLLECTOR_METRICS" 2>/dev/null || true)
if [ -z "$COLLECTOR_OUT" ]; then
  skip "no collector at ${COLLECTOR_METRICS} (direct-to-Dynatrace topology, or"
  echo "        port 8888 not exposed — expose service.telemetry to check)"
else
  sum_metric() {
    echo "$COLLECTOR_OUT" | grep "^$1" | awk '{s+=$2} END {printf "%.0f", s+0}'
  }
  spans=$(sum_metric otelcol_receiver_accepted_spans)
  points=$(sum_metric otelcol_receiver_accepted_metric_points)
  logs=$(sum_metric otelcol_receiver_accepted_log_records)
  failed=$(sum_metric otelcol_exporter_send_failed)
  echo "  spans accepted        : ${spans:-0}"
  echo "  metric points accepted: ${points:-0}"
  echo "  log records accepted  : ${logs:-0}"
  echo "  export failures       : ${failed:-0}"

  if [ "${spans:-0}" -gt 0 ] || [ "${points:-0}" -gt 0 ]; then
    pass "collector is receiving telemetry from the router"
  else
    fail "collector received nothing — check the router's otlp endpoint/port"
  fi
  if [ "${failed:-0}" -eq 0 ]; then
    pass "collector reports no export failures"
  else
    fail "collector had ${failed} export failures — usually a bad token or scope"
    echo "        docker logs <collector> 2>&1 | grep -i 'dynatrace\|401\|403\|404'"
  fi
fi

echo
echo "=============================================================="
echo " 2. Dynatrace Metrics API"
echo "=============================================================="
case "${DT_API_TOKEN:-}" in
  dt0s16.*) CLASSIC_TOKEN="" ;;
  ?*)       CLASSIC_TOKEN="$DT_API_TOKEN" ;;
  *)        CLASSIC_TOKEN="" ;;
esac

if [ -z "${DT_ENVIRONMENT_ID:-}" ]; then
  skip "DT_ENVIRONMENT_ID not set — cannot query the tenant"
elif [ -z "$CLASSIC_TOKEN" ]; then
  skip "no classic dt0c01 token in DT_API_TOKEN — the Metrics API only accepts"
  echo "        those (a platform token gets 403 here). Section 3 verifies via DQL."
else
  DT_API_TOKEN="$CLASSIC_TOKEN"
  DT_BASE="https://${DT_ENVIRONMENT_ID}.live.dynatrace.com"

  for metric in $METRICS; do
    # OTLP-ingested metrics are addressed with the ext: prefix in this API.
    selector="ext:${metric}"
    url="${DT_BASE}/api/v2/metrics/query?metricSelector=$(python3 -c "
import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$selector")&from=now-2h&resolution=1h"
    status=$(curl -s -o /tmp/dt_metric.json -w '%{http_code}' --max-time 15 \
      -H "Authorization: Api-Token ${DT_API_TOKEN}" "$url" 2>/dev/null || echo 000)

    case "$status" in
      200)
        points=$(python3 -c "
import json
try:
    d=json.load(open('/tmp/dt_metric.json'))
    print(sum(1 for r in d.get('result',[]) for s in r.get('data',[])
              for v in s.get('values',[]) if v is not None))
except Exception:
    print(0)")
        if [ "${points:-0}" -gt 0 ]; then
          pass "${selector}: ${points} data point(s) in the last 2h"
        else
          fail "${selector}: registered but no data points"
          echo "        Most likely cumulative temporality (Dynatrace drops those),"
          echo "        or the metric hasn't been produced yet. Send traffic and retry."
        fi ;;
      401)
        fail "${selector}: 401 — token lacks metrics.read (ingest and read are"
        echo "        separate scopes; this script needs read)"; break ;;
      403)
        fail "${selector}: 403 — the token is valid but lacks \`metrics.read\`."
        echo "        Dynatrace answers 403 (not 401) for a missing scope here. Ingest"
        echo "        and read are separate scopes: a token that can push metrics"
        echo "        cannot necessarily read them back. Add metrics.read to it, or"
        echo "        rely on section 3 (DQL), which needs no Metrics API scope."; break ;;
      404)
        fail "${selector}: 404 — metric not registered yet. First ingest can take"
        echo "        a few minutes; after that, suspect the metric was never sent." ;;
      *)
        fail "${selector}: unexpected HTTP ${status}"
        head -c 300 /tmp/dt_metric.json 2>/dev/null | sed 's/^/        /' ;;
    esac
  done

  echo
  echo "  Grail/DQL equivalent (paste into a notebook — no ext: prefix there):"
  echo "    timeseries requests = sum(dynatrace.router.requests), filter: {service.name == \"apollo-router\"}"
fi

echo
echo "=============================================================="
echo " 3. Grail (DQL)"
echo "=============================================================="
# Platform tokens (dt0s16...) are Bearer-authenticated and serve Grail, not the
# Metrics API. This is the only verification path that works with one — and it
# checks spans too, which the Metrics API cannot see at all.
PLATFORM_TOKEN=""
for candidate in "${DT_PLATFORM_TOKEN:-}" "${DT_API_TOKEN:-}" "${DT_BEARER_TOKEN:-}" \
                 "${DYNATRACE_API_TOKEN:-}"; do
  case "$candidate" in
    dt0s16.*) PLATFORM_TOKEN="$candidate"; break ;;
  esac
done

if [ -z "${DT_ENVIRONMENT_ID:-}" ]; then
  skip "DT_ENVIRONMENT_ID not set — cannot query Grail"
elif [ -z "$PLATFORM_TOKEN" ]; then
  skip "no platform token (dt0s16...) found in DT_PLATFORM_TOKEN / DT_API_TOKEN /"
  echo "        DT_BEARER_TOKEN — skipping DQL verification"
else
  # Grail is served by the .apps. host; the .live. host 403s these paths.
  DQL_BASE="https://${DT_ENVIRONMENT_ID}.apps.dynatrace.com/platform/storage/query/v1"

  dql_records() {
    # echoes the record count, or "ERR <detail>"
    local query="$1" body token state out
    body=$(python3 -c 'import json,sys; print(json.dumps({"query": sys.argv[1]}))' "$query")
    out=$(curl -sS --max-time 30 -X POST "${DQL_BASE}/query:execute" \
      -H "Authorization: Bearer ${PLATFORM_TOKEN}" \
      -H "Content-Type: application/json" --data "$body" 2>/dev/null)
    token=$(printf '%s' "$out" | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("requestToken",""))
except Exception: print("")')
    if [ -z "$token" ]; then
      echo "ERR $(printf '%s' "$out" | head -c 160)"; return
    fi
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      out=$(curl -sS --max-time 30 -G "${DQL_BASE}/query:poll" \
        --data-urlencode "request-token=$token" \
        -H "Authorization: Bearer ${PLATFORM_TOKEN}" 2>/dev/null)
      state=$(printf '%s' "$out" | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("state","?"))
except Exception: print("PARSE_ERR")')
      if [ "$state" = "SUCCEEDED" ]; then
        printf '%s' "$out" | python3 -c 'import sys,json
d=json.load(sys.stdin); print(len((d.get("result") or {}).get("records") or []))'
        return
      fi
      sleep 3
    done
    echo "ERR timed out in state ${state}"
  }

  spans=$(dql_records 'fetch spans, from: -2h | filter service.name == "apollo-router" | limit 5')
  case "$spans" in
    ERR*) fail "spans query failed: ${spans#ERR }" ;;
    0)    fail "no apollo-router spans in the last 2h — traces are not arriving" ;;
    *)    pass "traces: ${spans} span record(s) from apollo-router in the last 2h" ;;
  esac

  for metric in $METRICS; do
    n=$(dql_records "timeseries v = sum(\`${metric}\`), from: -2h")
    case "$n" in
      ERR*) fail "${metric}: DQL error: ${n#ERR }" ;;
      0)    fail "${metric}: no series in Grail (not ingested, or dropped as cumulative)" ;;
      *)    pass "${metric}: ${n} series in the last 2h" ;;
    esac
  done
fi

echo
echo "=============================================================="
echo "TOTAL: ${PASS} passed, ${FAIL} failed, ${SKIP} skipped"
if [ -n "${DT_ENVIRONMENT_ID:-}" ]; then
  echo "  traces  : https://${DT_ENVIRONMENT_ID}.apps.dynatrace.com/ui/apps/dynatrace.distributed-traces"
  echo "  metrics : https://${DT_ENVIRONMENT_ID}.apps.dynatrace.com/ui/apps/dynatrace.metrics"
fi
echo "=============================================================="
[ "$FAIL" -eq 0 ]
