#!/usr/bin/env bash
# Bring up the demo: subgraphs + router (+ collector in collector mode), pointed
# at a real Dynatrace tenant.
#
#   ./demo/up.sh              # direct to Dynatrace (default)
#   ./demo/up.sh collector    # via an OTel Collector in Docker
#
# Then:
#   ./demo/load.sh            # generate traffic
#   ./scripts/verify_ingest.sh
#   ./demo/down.sh
#
# Requirements: python3, a router binary, and .env filled in. Collector mode also
# needs Docker. Nothing else — the subgraphs are stdlib Python.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
MODE="${1:-direct}"
RUN_DIR="$HERE/.run"
mkdir -p "$RUN_DIR"

die() { echo "ERROR: $*" >&2; exit 1; }
say() { echo "  $*"; }

case "$MODE" in direct|collector) ;; *) die "mode must be 'direct' or 'collector'";; esac

# ── .env ─────────────────────────────────────────────────────────────────────
[ -f "$ROOT/.env" ] || die "no .env — copy .env.example and fill it in"
set -a; . "$ROOT/.env"; set +a

ROUTER_CONFIG="$HERE/router.${MODE}.yaml"
ROUTER_PORT="${ROUTER_PORT:-4000}"
ROUTER_HEALTH_PORT="${ROUTER_HEALTH_PORT:-8088}"
PRODUCTS_PORT="${PRODUCTS_PORT:-4011}"
ORDERS_PORT="${ORDERS_PORT:-4012}"

# ── preflight ────────────────────────────────────────────────────────────────
echo "Preflight"
for var in OTEL_EXPORTER_OTLP_ENDPOINT OTEL_EXPORTER_OTLP_TRACES_ENDPOINT \
           OTEL_EXPORTER_OTLP_METRICS_ENDPOINT; do
  [ -z "${!var:-}" ] || die "$var is set. Router v2.12 and earlier silently
       redirect telemetry to it; v2.13+ refuses to start. unset it."
done
say "OTEL_EXPORTER_OTLP_* unset"

if [ "$MODE" = "direct" ]; then
  [ -n "${DYNATRACE_ENV_URL:-}" ] || die "DYNATRACE_ENV_URL is not set (see .env.example)"
  [ -n "${DYNATRACE_API_TOKEN:-}" ] || die "DYNATRACE_API_TOKEN is not set"
  case "$DYNATRACE_ENV_URL" in
    *:*[0-9]) ;;
    *) die "DYNATRACE_ENV_URL needs an explicit port, e.g. https://abc12345.live.dynatrace.com:443" ;;
  esac
  say "DYNATRACE_ENV_URL = ${DYNATRACE_ENV_URL}"
else
  [ -n "${DT_OTLP_ENDPOINT:-}" ] || die "DT_OTLP_ENDPOINT is not set (collector mode)"
  [ -n "${DT_API_TOKEN:-}" ] || die "DT_API_TOKEN is not set (collector mode)"
  case "$DT_OTLP_ENDPOINT" in
    */v1/traces|*/v1/metrics|*/v1/logs)
      die "DT_OTLP_ENDPOINT must not include a /v1/<signal> suffix — the collector appends it" ;;
  esac
  say "DT_OTLP_ENDPOINT = ${DT_OTLP_ENDPOINT}"
fi

# Same rules the CI gate uses.
if [ "$MODE" = "direct" ]; then
  python3 "$ROOT/scripts/validate_dynatrace.py" "$ROUTER_CONFIG" >"$RUN_DIR/validate.out" 2>&1 \
    || { cat "$RUN_DIR/validate.out"; die "router config failed validation"; }
else
  python3 "$ROOT/scripts/validate_dynatrace.py" --mode collector "$ROUTER_CONFIG" \
    >"$RUN_DIR/validate.out" 2>&1 || { cat "$RUN_DIR/validate.out"; die "router config failed validation"; }
  python3 "$ROOT/scripts/validate_collector.py" "$HERE/otel-collector.yaml" \
    >>"$RUN_DIR/validate.out" 2>&1 || { cat "$RUN_DIR/validate.out"; die "collector config failed validation"; }
fi
say "config validated ($MODE mode)"

ROUTER=""
if [ -n "${ROUTER_BIN:-}" ] && [ -x "${ROUTER_BIN}" ]; then ROUTER="$ROUTER_BIN"
elif [ -x "$ROOT/bin/router" ]; then ROUTER="$ROOT/bin/router"
elif command -v router >/dev/null 2>&1; then ROUTER="$(command -v router)"
fi
[ -n "$ROUTER" ] || die "no router binary found. Install one:
       curl -sSL https://router.apollo.dev/download/nix/latest | sh
       then re-run with ROUTER_BIN=./router ./demo/up.sh $MODE"
say "router: $ROUTER ($("$ROUTER" --version 2>/dev/null | head -1))"

# ── collector (collector mode only) ──────────────────────────────────────────
if [ "$MODE" = "collector" ]; then
  echo
  echo "Collector"
  command -v docker >/dev/null 2>&1 || die "collector mode needs Docker"
  ( cd "$HERE" && docker compose up -d ) || die "docker compose up failed"
  deadline=$(( $(date +%s) + 30 ))
  until curl -sf "http://127.0.0.1:8888/metrics" >/dev/null 2>&1; do
    [ "$(date +%s)" -ge "$deadline" ] && die "collector did not come up — docker compose -f demo/docker-compose.yaml logs"
    sleep 1
  done
  say "collector up, self-metrics on http://127.0.0.1:8888/metrics"
fi

# ── subgraphs ────────────────────────────────────────────────────────────────
echo
echo "Subgraphs"
PRODUCTS_PORT="$PRODUCTS_PORT" ORDERS_PORT="$ORDERS_PORT" \
  python3 "$HERE/subgraphs.py" >"$RUN_DIR/subgraphs.log" 2>&1 &
echo $! > "$RUN_DIR/subgraphs.pid"
deadline=$(( $(date +%s) + 15 ))
for port in "$PRODUCTS_PORT" "$ORDERS_PORT"; do
  until curl -sf "http://127.0.0.1:${port}/" >/dev/null 2>&1; do
    [ "$(date +%s)" -ge "$deadline" ] && die "subgraph on :${port} did not start — see $RUN_DIR/subgraphs.log"
    sleep 0.3
  done
  say "subgraph up on :${port}"
done

# ── router ───────────────────────────────────────────────────────────────────
echo
echo "Router"
APOLLO_ROUTER_LOG="${APOLLO_ROUTER_LOG:-info}" \
  "$ROUTER" --supergraph "$HERE/supergraph.graphql" --config "$ROUTER_CONFIG" \
  >"$RUN_DIR/router.log" 2>&1 &
echo $! > "$RUN_DIR/router.pid"
deadline=$(( $(date +%s) + 45 ))
until curl -sf "http://127.0.0.1:${ROUTER_HEALTH_PORT}/health" >/dev/null 2>&1; do
  if ! kill -0 "$(cat "$RUN_DIR/router.pid")" 2>/dev/null; then
    tail -30 "$RUN_DIR/router.log"; die "router exited on startup"
  fi
  [ "$(date +%s)" -ge "$deadline" ] && { tail -30 "$RUN_DIR/router.log"; die "router not healthy in 45s"; }
  sleep 1
done
say "router healthy on http://127.0.0.1:${ROUTER_PORT}/ (Sandbox in a browser)"

cat <<EOF

Next:
  ./demo/load.sh                     generate traffic (queries, entity joins, errors)
  ./scripts/verify_ingest.sh         confirm Dynatrace kept the data
  ./demo/down.sh                     stop everything

Logs: $RUN_DIR/router.log, $RUN_DIR/subgraphs.log
EOF
