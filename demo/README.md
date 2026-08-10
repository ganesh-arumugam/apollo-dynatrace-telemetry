# Demo — run the router, ingest into Dynatrace, verify

A minimal federated graph (two subgraphs, one entity join) whose only job is to
make the router emit real telemetry so you can watch it land in a Dynatrace
tenant. No storefront, no extra backends — if you want a full product demo, use
a real federated demo graph.

```
demo/
  subgraphs.py            products :4011 + orders :4012, stdlib Python only
  subgraph-schemas/       the two federated SDLs (source for rover)
  supergraph.graphql      composed, committed so rover isn't required
  supergraph.yaml         rover config to regenerate it
  router.direct.yaml      router -> Dynatrace
  router.collector.yaml   router -> OTel Collector -> Dynatrace
  otel-collector.yaml     collector config (metrics pipeline w/ cumulativetodelta)
  docker-compose.yaml     collector only, on the host network
  up.sh / load.sh / down.sh
```

Requirements: `python3`, a router binary, a Dynatrace tenant. Collector mode also
needs Docker. Nothing to `npm install`.

## 1. Configure

```bash
cp .env.example .env        # from the repo root
```

Fill in **one** topology:

- **Direct** — `DYNATRACE_ENV_URL` (with `:443`) and `DYNATRACE_API_TOKEN`
  (scopes `openTelemetryTrace.ingest`, `metrics.ingest`, `logs.ingest`).
- **Collector** — `DT_OTLP_ENDPOINT` (no `/v1/<signal>` suffix) and `DT_API_TOKEN`.

Add `metrics.read` to the token if you want `verify_ingest.sh` to read data back.
Creating the tokens: `docs/dynatrace-credentials.md` has the click-path for each.

## 2. Get a router

```bash
curl -sSL https://router.apollo.dev/download/nix/latest | sh
export ROUTER_BIN=./router
```

## 3. Run

```bash
./demo/up.sh              # direct (default)
./demo/up.sh collector    # via the collector in Docker
```

`up.sh` checks the common setup problems before starting, so you get a clear
message instead of an empty dashboard: a set
`OTEL_EXPORTER_OTLP_*`, a `DYNATRACE_ENV_URL` with no port, a `DT_OTLP_ENDPOINT`
carrying a `/v1/traces` suffix, or a router config that fails the `DT###` rules.

## 4. Generate traffic

```bash
./demo/load.sh            # 20 iterations, ~90 requests
```

Each iteration drives a different part of the telemetry:

| Query | What it exercises in Dynatrace |
|---|---|
| `{ products { … } }` | one subgraph fetch; baseline latency + request counters |
| `{ orders { items { … } } }` | **entity join** — an `_entities` fetch to `products`, the interesting span |
| `{ product(id:) { … } }` | variable-carrying operation |
| `{ boom }` | GraphQL error with **HTTP 200** → error instruments and `otel.status_code = ERROR` |
| `{ doesNotExist }` | router-side validation failure (4xx, never reaches a subgraph) |

`load.sh` prints the last trace id (via `apollo-trace-id`), so you can jump
straight to it.

## 5. Verify it landed

```bash
./scripts/verify_ingest.sh
```

Collector self-metrics first (`otelcol_receiver_accepted_*` vs
`otelcol_exporter_send_failed`), then a Metrics API query per instrument. Then in
Dynatrace:

```dql
timeseries requests = sum(dynatrace.router.requests),
  by: {http.response.status_code},
  filter: {service.name == "apollo-router"}
```

```dql
fetch spans | filter trace.id == "<id printed by load.sh>"
```

Import the dashboard for the full set:

```bash
DT_ENVIRONMENT_ID=abc12345 DT_BEARER_TOKEN=dt0s16... ./scripts/import_dashboard.sh
```

## 6. Stop

```bash
./demo/down.sh
```

## Timing expectations

| Signal | When it shows up |
|---|---|
| Traces | seconds (2s batch delay in the demo config) |
| Metrics, existing key | under a minute |
| Metrics, first-ever key | a few minutes while Dynatrace registers it — a 404 from the Metrics API during this window is normal |
| Logs | seconds |

## If nothing arrives

1. `tail -f demo/.run/router.log` — exporter errors appear here (401/403/404).
2. `python3 scripts/validate_dynatrace.py demo/router.direct.yaml` — the rule set
   catches every silent-failure mode.
3. Collector mode: `docker compose -f demo/docker-compose.yaml logs otel-collector`,
   and check `curl -s localhost:8888/metrics | grep send_failed`.
4. Metric registered but empty ⇒ temporality. 401 ⇒ token scope. 404 on the
   Metrics API ⇒ never sent, or first-ingest lag.
5. Spans but no log events ⇒ the logging pipeline is missing, not broken.

## Regenerating the supergraph

Only needed if you edit `subgraph-schemas/`:

```bash
rover supergraph compose --config demo/supergraph.yaml > demo/supergraph.graphql
```

The subgraph resolver is a deliberate stub: it keyword-matches the router's query
and returns every field of the type. Add a field to a schema and you must add it
to `subgraphs.py` too — `tests/test_demo.py` will tell you if they drift.
