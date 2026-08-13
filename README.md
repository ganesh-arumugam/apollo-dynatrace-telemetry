# Apollo Router → Dynatrace telemetry

Export Apollo Router telemetry to Dynatrace: router config templates for both
topologies, a curated instruments set, a ready-to-import dashboard, config
validators, and a test harness that verifies the export path without a tenant.

**Set up**

- [Quickstart](#quickstart--see-it-work-end-to-end) — see it work end to end
- [`docs/dynatrace-credentials.md`](docs/dynatrace-credentials.md) — getting the right tokens
- [`docs/metrics.md`](docs/metrics.md) — which metrics to collect, and why

**Operate**

- [The dashboard](#the-dashboard) — build from `tiles.yaml`, import in two commands
- [`dashboards/dql-queries.md`](dashboards/dql-queries.md) — reading the metrics: what's good, what's concerning, the DQL
- [`docs/percentiles-and-buckets.md`](docs/percentiles-and-buckets.md) — understanding percentiles and histogram buckets

**Debug**

- [Validators](#router-rules-dt) — run these first when no data arrives
- [`docs/rules.md`](docs/rules.md) — what every rule means, and its fix

**Compare and coexist**

- [`docs/studio-vs-dynatrace-latency.md`](docs/studio-vs-dynatrace-latency.md) — comparing numbers with GraphOS Studio
- [`docs/studio-vs-dynatrace-capabilities.md`](docs/studio-vs-dynatrace-capabilities.md) — standardizing on Dynatrace: what stays in Studio
- [`docs/datadog-parity.md`](docs/datadog-parity.md) — coming from the Datadog template
- [`docs/prometheus-and-dynatrace.md`](docs/prometheus-and-dynatrace.md) — keeping Prometheus alongside Dynatrace

## Common questions

| Question | Where it's answered |
|---|---|
| Which router metrics exist, and which should we collect? | [`docs/metrics.md`](docs/metrics.md) — every metric, the question it answers, what it costs to enable, and which Dynatrace refuses |
| What does a healthy value look like — when should a number worry us? | [`dashboards/dql-queries.md`](dashboards/dql-queries.md#what-each-metric-is-telling-you) — a Good / Concerning read on every charted metric |
| The config validates, but a metric never shows up in Dynatrace. | [`docs/metrics.md`](docs/metrics.md#15-metrics-dynatrace-rejects) — some types are accepted with a 2xx and refused at ingest |
| Attributes disappeared from a metric that worked for weeks. | [`docs/metrics.md`](docs/metrics.md#attributes-are-what-cost-you-not-metrics) — the 2,000-datapoint cardinality ceiling strips them silently |
| Why are p50, p95 and p99 identical in my DQL query? | [`docs/percentiles-and-buckets.md`](docs/percentiles-and-buckets.md) — `percentile()` over a rolled-up metric returns the average; use spans |
| How do we monitor whether a subgraph is reachable? | [`docs/metrics.md`](docs/metrics.md#14-subgraph-health--the-gap) — no product answer exists; the workarounds and their trade-offs |
| What are histogram buckets, and why is my p95 a guess? | [`docs/percentiles-and-buckets.md`](docs/percentiles-and-buckets.md) — the mechanism behind the recommendations, with a worked example |
| Why doesn't the p95 in Dynatrace match GraphOS Studio? | [`docs/studio-vs-dynatrace-latency.md`](docs/studio-vs-dynatrace-latency.md) — the two pipelines, which numbers are comparable, and how to read percentiles correctly |
| If we standardize on Dynatrace, what do we lose from Studio? | [`docs/studio-vs-dynatrace-capabilities.md`](docs/studio-vs-dynatrace-capabilities.md) — what each holds that the other cannot, and what to build where |
| Can we run Prometheus and Dynatrace at the same time? | [`docs/prometheus-and-dynatrace.md`](docs/prometheus-and-dynatrace.md) — yes; config, what's shared, what isn't |
| What does `temporality: delta` actually mean, and does it affect our data? | [`docs/prometheus-and-dynatrace.md`](docs/prometheus-and-dynatrace.md#cumulative-on-one-side-delta-on-the-other--both-correct) — the same counter, two transports, both correct |
| Is there a ready-made dashboard, and how do we import it? | [The dashboard](#the-dashboard) — built from `dashboards/tiles.yaml`, imported via the Documents API or uploaded by hand |
| We already use the Datadog template — what's different here? | [`docs/datadog-parity.md`](docs/datadog-parity.md) |
| Which token do I need, and where do I create it? | [`docs/dynatrace-credentials.md`](docs/dynatrace-credentials.md) |
| Why are GraphQL errors showing as successful requests? | Spans need `otel.status_code: ERROR` — [`templates/spans.router.yaml`](templates/spans.router.yaml), rule `DT021` |
| We configured everything and no data arrived. | [Validators](#router-rules-dt) first, then [`demo/`](demo/README.md) to reproduce against a tenant |

## Why this exists

Each of these returns HTTP 200 or 404, produces no data, and logs no router
error — so the config looks correct and the dashboards stay empty:

| Mistake | Symptom |
|---|---|
| `temporality: cumulative` (or omitted) | counters silently dropped |
| Prometheus scrape → Dynatrace with no `cumulativetodelta` | same, one layer down |
| `protocol: grpc` to Dynatrace | exporter can't connect; ingest is HTTP-only |
| endpoint without `:443` | connection failures on hosts with no port |
| `Bearer` instead of `Api-Token` (ingest) | 401s buried in exporter logs |
| `Api-Token` instead of `Bearer` (dashboards) | 401 that looks like an ingest problem |
| collector-style path `/v1/metrics` on the direct endpoint | 404s |
| `/v1/traces` left on the collector's exporter endpoint | 404s |
| traces path on the metrics exporter | accepted, then discarded |
| endpoint on the `.apps.` host instead of `.live.` | 404 on ingest; the host answers, so it looks reachable |
| stdout JSON without a trace id | log lines exist but no trace correlates to them |
| `ext:` prefix in DQL (or missing in the Metrics API) | "metric not found" |

Each is a rule in `scripts/validate_dynatrace.py` or
`scripts/validate_collector.py`. A second class — `logging.otlp` (`DT022`), an
invalid condition operator such as `gte` (`DT024`), `sandbox` without
`supergraph.introspection` (`DT025`) — is rejected at config load, so the router
never starts. Layer 3 of the harness catches those.

**Router logs do not reach Dynatrace directly.** `telemetry.exporters.logging`
accepts only `common` and `stdout` — there is no OTLP log exporter. Ship the
router's stdout instead (a collector `filelog` receiver, or an existing host
forwarder). The direct topology covers metrics and traces; logs need the
collector topology.

## Two topologies, both supported

```
direct     router ──OTLP/HTTP──▶ https://<env>.live.dynatrace.com:443/api/v2/otlp/v1/<signal>
collector  router ──OTLP──▶ OTel Collector ──otlphttp──▶ Dynatrace
```

**Direct** has fewer moving parts and lets the router produce delta metrics
itself. **Collector** keeps backend details out of the router config (changing or
adding a destination becomes a collector change), can fan out to more than one
backend, and can ship router logs by tailing stdout — in exchange, its metrics
pipeline has to convert cumulative to delta.

The validator infers which one you're using per exporter and applies only the
rules that topology can violate; `--mode direct|collector` forces it.

## Layout

```
demo/                              # runnable: subgraphs + router -> Dynatrace
  subgraphs.py                     # products :4011 + orders :4012, stdlib only
  subgraph-schemas/, supergraph.graphql, supergraph.yaml
  router.direct.yaml               # router -> Dynatrace
  router.collector.yaml            # router -> collector -> Dynatrace
  otel-collector.yaml, docker-compose.yaml
  up.sh / load.sh / down.sh, README.md
docs/
  prometheus-and-dynatrace.md      # running both metrics backends together
  datadog-parity.md                # migrating from the Datadog template
  dynatrace-credentials.md         # UI click-path for each token / OAuth client
  metrics.md                       # every router metric: why, cost to enable, where
  rules.md                         # every DT###/DTC### rule: symptom, mechanism, fix
  studio-vs-dynatrace-latency.md   # comparing GraphOS Studio and Dynatrace numbers
  studio-vs-dynatrace-capabilities.md  # what each holds that the other cannot
  percentiles-and-buckets.md       # how percentiles, buckets and rollup actually work
templates/
  # Start here — the exporter config for direct-to-Dynatrace
  dynatrace.router.yaml            # metrics + traces, one file
  dynatrace-activegate.router.yaml # Managed / ActiveGate endpoint shapes
  # What to measure. Separate files because they configure different things.
  instruments.router.yaml          # which metrics to emit, and their attributes
  spans.router.yaml                # span attributes + GraphQL error marking
  histogram-buckets.router.yaml    # bucket boundaries, if charting the metric
  dynatrace-logs.router.yaml       # why logs need stdout forwarding, and how
  # Alternatives to the direct path — not needed for a Dynatrace-only setup
  prometheus-and-dynatrace.router.yaml  # both metrics backends at once
  collector/
    router-to-collector.router.yaml  # router side of the collector topology
    otel-collector-dynatrace.yml     # collector side, incl. the metrics pipeline
    otel-collector-fanout.yml        # one router export -> Prometheus + Dynatrace
dashboards/
  tiles.yaml                       # source of truth for the dashboard
  dynatrace-dashboard.json         # generated Platform dashboard (version 17)
  dql-queries.md                   # one DQL query per instrument, with a Good/Concerning read on each
scripts/
  validate_dynatrace.py            # DT### rules for router configs
  validate_collector.py            # DTC### rules for collector configs
  build_dashboard.py               # tiles.yaml -> dashboard JSON (--check in CI)
  import_dashboard.sh              # upload via the Platform Documents API
  verify_ingest.sh                 # confirm a tenant kept the data
  compare_studio.py                # GraphOS Studio vs Dynatrace, side by side
harness/
  run_harness.sh                   # static -> mock contract -> live router
  mock_dynatrace.py                # strict mock of Dynatrace's OTLP ingest
  harness.router.yaml              # (subgraphs + supergraph come from demo/)
tests/                             # 96 tests: rules, mock, dashboard, demo, real configs
run-tests.sh
```

## Quickstart — see it work end to end

```bash
cp .env.example .env                      # fill in one topology block
curl -sSL https://router.apollo.dev/download/nix/latest | sh

ROUTER_BIN=./router ./demo/up.sh          # subgraphs + router -> Dynatrace
./demo/load.sh                            # traffic: joins, errors, validation failures
./scripts/verify_ingest.sh                # confirm Dynatrace kept it
./demo/down.sh
```

`./demo/up.sh collector` runs the same thing through an OTel Collector in Docker.
See `demo/README.md` for what each query exercises and how long each signal takes
to appear. The demo graph is two subgraphs with one entity join — enough to
produce real subgraph fetches, entity fetches, GraphQL errors, and validation
failures, and nothing more.

## Quickstart — apply to an existing router

```bash
cp .env.example .env      # DYNATRACE_ENV_URL (with :443) + DYNATRACE_API_TOKEN
set -a && . ./.env && set +a

# direct topology
python3 scripts/validate_dynatrace.py /path/to/your/router.yaml

# collector topology
python3 scripts/validate_dynatrace.py --mode collector /path/to/router.yaml
python3 scripts/validate_collector.py /path/to/otel-collector-config.yaml

# prove the router exports, no tenant required
./harness/run_harness.sh

# dashboard
python3 scripts/build_dashboard.py
DT_ENVIRONMENT_ID=abc12345 DT_BEARER_TOKEN=dt0s16... ./scripts/import_dashboard.sh

# confirm a real tenant kept the data
DT_ENVIRONMENT_ID=abc12345 DT_API_TOKEN=dt0c01... ./scripts/verify_ingest.sh
```

**Tokens — three different ones, easy to mix up** (click-path for each:
`docs/dynatrace-credentials.md`)**:**

| Purpose | Header | Scopes |
|---|---|---|
| OTLP ingest | `Api-Token dt0c01...` | `openTelemetryTrace.ingest`, `metrics.ingest`, `logs.ingest` |
| Metrics API read (verify) | `Api-Token dt0c01...` | `metrics.read` |
| Dashboard import | `Bearer dt0s16...` (platform token) | `document:documents:write` |

**Preflight, easy to miss:** `OTEL_EXPORTER_OTLP_ENDPOINT`,
`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, and `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT`
must be unset. Router v2.12 and earlier let them silently override the telemetry
config; v2.13+ refuses to start. The harness checks this first.

## The dashboard

`dashboards/tiles.yaml` is the source of truth. `build_dashboard.py` renders it
into the Platform document JSON, and a test asserts the two never drift.

```bash
# 1. build — pass your router's service.name, or every tile will be empty
python3 scripts/build_dashboard.py --service-name my-supergraph

# 2. import (creates it, or replaces the existing one of the same name)
DT_ENVIRONMENT_ID=abc12345 DT_BEARER_TOKEN=dt0s16... ./scripts/import_dashboard.sh
```

The import script also supports OAuth client credentials for CI and service
accounts (`DT_OAUTH_CLIENT_ID` / `DT_OAUTH_CLIENT_SECRET` / `DT_ACCOUNT_UUID`
instead of `DT_BEARER_TOKEN`) — click-paths for both credential types in
[`docs/dynatrace-credentials.md`](docs/dynatrace-credentials.md). No token at
all? The generated `dashboards/dynatrace-dashboard.json` is a standard
dashboard document — upload it by hand in the Dashboards app instead.

**Every tile filters on `service.name`.** The default is `apollo-router`; if your
router reports anything else, the import succeeds and the dashboard is blank. Use
`--service-name`, or `--service-filter` for something more specific such as
`'service.name == "router" and k8s.namespace.name == "prod"'`.

To change a tile, edit `tiles.yaml` and rebuild — don't hand-edit the JSON, since
CI regenerates it. Re-importing **deletes the previous dashboard of the same name**
rather than accumulating copies (the Documents API has no upsert). Override the
name with `DASHBOARD_NAME=...` to keep several side by side.

Some tiles stay empty by design and say so in their title: the cache tiles need
router caching enabled, the connector tiles need Apollo Connectors, and the log
tile needs the router's stdout forwarded (there is no OTLP log exporter — `DT022`).
The 5xx tiles are unlabelled because empty simply means no 5xx occurred.

Latency percentiles read from **spans**, not from the duration histogram — see
[the Studio comparison](docs/studio-vs-dynatrace-latency.md) for why.

## Comparing against GraphOS Studio

Set `APOLLO_KEY` + `APOLLO_GRAPH_REF` alongside the Dynatrace exporters and the
router reports to both at once. Then:

```bash
python3 scripts/compare_studio.py                 # last complete hour
python3 scripts/compare_studio.py --hours-ago 6 --service-name my-supergraph
```

It prints per-operation counts and p50/p90/p99 from each side, plus the error
totals, and needs `DT_BEARER_TOKEN` for the Grail query. Request counts should
match exactly; Studio errors correspond to `apollo.router.graphql_error`, not to
the 5xx counter. Full explanation of every difference:
[`docs/studio-vs-dynatrace-latency.md`](docs/studio-vs-dynatrace-latency.md).

## Router rules (`DT###`)

`python3 scripts/validate_dynatrace.py router.yaml [--mode ...] [--strict] [--json] [--allow-loopback] [--router-bin ./router]`

Exit codes: `0` clean, `1` errors (or warnings with `--strict`), `2` bad input.

This table is the one-line index — [`docs/rules.md`](docs/rules.md) has the
symptom, mechanism and fix for every rule, and `--json` findings carry a `docs`
link straight to it.

| Rule | Level | Applies to | Check |
|---|---|---|---|
| `DT000` | error | both | the router itself rejects this config — runs `router config validate` when `--router-bin` is given; these rules cover the Dynatrace contract, not the router's full schema |
| `DT001` | error | direct | otlp `protocol` must be `http` |
| `DT002` | error | direct | metrics `temporality` must be `delta` |
| `DT003` | error | direct | endpoint path matches its signal (`/api/v2/otlp/v1/{metrics,traces,logs}`) |
| `DT004` | error | direct | endpoint host carries an explicit port |
| `DT005` | error | direct | `Authorization` present, starting with `Api-Token ` |
| `DT006` | error | both | token via `${env.*}` / `${file.*}`, never inlined |
| `DT007` | error | both | exporter explicitly `enabled: true` |
| `DT008` | error | both | endpoint is a real URL, not `default` |
| `DT009` | error | direct | `grpc` (or omitted protocol, which defaults to grpc) against Dynatrace |
| `DT010` | error | direct | endpoint scheme is `https` (loopback only with `--allow-loopback`) |
| `DT011` | error | both | valid `default_requirement_level` |
| `DT012` | error/warn | both | custom instrument names follow OTel conventions, don't squat `http.*` / `apollo.*` |
| `DT013` | error/warn | both | custom instruments declare a valid `type`, plus `unit` and `description` |
| `DT014` | warn | both | `batch_processor` tuned sanely for a rate-limited SaaS endpoint |
| `DT015` | warn | both | trace `sampler: 1.0` (cost / rate limits) |
| `DT016` | warn | both | `graphql.document`-class attributes and `recommended` level (cardinality + PII) |
| `DT017` | warn | both | traces exported but stdout JSON carries no trace id — nothing correlates a log line to a span |
| `DT018` | warn | both | `parent_based_sampler: false` splits distributed traces |
| `DT019` | warn | both | `spans.mode: deprecated` (legacy naming groups badly in Dynatrace) |
| `DT020` | warn | both | `spans.default_attribute_requirement_level: recommended` adds `graphql.document` |
| `DT021` | warn | both | no `otel.status_code` error marking — GraphQL errors are HTTP 200 and look successful |
| `DT022` | error | both | `telemetry.exporters.logging.otlp` — the router has no OTLP log exporter; this key stops it starting |
| `DT023` | error | direct | endpoint on the `.apps.` host — that's the platform/UI host and never serves ingest |
| `DT024` | error | both | unknown condition operator (`gte`/`lte`); only `eq`/`gt`/`lt`/`exists`/`all`/`any`/`not` exist |
| `DT025` | error | both | `sandbox.enabled` without `supergraph.introspection: true` — router refuses to start |
| `DT026` | warn | both | histogram buckets too coarse — percentiles above 500ms are interpolation guesses ([why](docs/studio-vs-dynatrace-latency.md)) |
| `DT027` | warn | both | a `views` entry with a wildcard name silently matches nothing — the drop/rename is a no-op |
| `DT028` | warn | direct | no `service_name` — the router reports as `unknown_service:router` and every dashboard tile stays blank |
| `DT029` | warn | both | `graphql.operation.name` on a metric — one series per operation walks into the 2,000-datapoint cardinality ceiling |
| `DT030` | error | both | `persisted_queries.safelist` with APQ still on — the router refuses to start |
| `DT101` | error | collector | collector-bound protocol must be `grpc` or `http` |
| `DT102` | warn | collector | metrics need delta at the router or `cumulativetodelta` in the collector |
| `DT103` | warn | collector | OTLP/HTTP path should be `/v1/{signal}` if present |

## Collector rules (`DTC###`)

`python3 scripts/validate_collector.py otel-collector-config.yaml [--strict] [--json]`

Full definitions: [`docs/rules.md`](docs/rules.md#collector-rules-scriptsvalidate_collectorpy).

| Rule | Level | Check |
|---|---|---|
| `DTC001` | error | the Dynatrace exporter must be `otlphttp` (no gRPC ingest) |
| `DTC002` | error | endpoint must not include a `/v1/<signal>` suffix — the exporter appends it |
| `DTC003` | error | `Authorization: Api-Token ${env:...}`, not `Bearer`, never inlined |
| `DTC004` | error | a metrics pipeline fed by a cumulative receiver needs `cumulativetodelta` |
| `DTC005` | error/warn | the Dynatrace exporter must be wired into a pipeline |
| `DTC006` | error/warn | pipelines batch, and reference only defined processors |
| `DTC007` | warn | log pipelines trim request/response bodies (volume + PII) |
| `DTC008` | warn | endpoint should be `https` |
| `DTC009` | warn | `retry_on_failure` / `sending_queue` explicitly disabled — a 429 becomes dropped data instead of a retry |

## The harness

Three layers, each strictly stronger than the last:

1. **Static** — every template through the rule engines.
2. **Contract** — starts `mock_dynatrace.py` and asserts it enforces what the real
   endpoint enforces: valid export accepted; `Bearer` → 401; missing auth → 401;
   collector-style path → 404; wrong content type → 415; recorder counts exactly
   the accepted call.
3. **Live router** — boots Apollo Router with `harness.router.yaml` pointed at the
   mock, serves a real GraphQL operation plus a deliberately failing one, and
   asserts metrics **and** traces arrived with zero rejected calls. (Not logs:
   the router has no OTLP log exporter — see DT022.) This layer is also the only
   thing that catches a config the rule engines accept but the router rejects at
   startup, so a green layer 1 with a skipped layer 3 proves less than it looks.

Layers 1–2 need nothing but Python. Layer 3 needs a router binary and *skips
loudly* rather than passing silently:

```bash
curl -sSL https://router.apollo.dev/download/nix/latest | sh
ROUTER_BIN=./router ./harness/run_harness.sh
```

Against a real tenant, `scripts/verify_ingest.sh` closes the loop: collector
self-metrics (`otelcol_receiver_accepted_*` vs `otelcol_exporter_send_failed`),
then a Metrics API query per instrument to confirm Dynatrace actually kept it.

## Tests

```bash
./run-tests.sh            # fixtures + unit tests + harness layers 1-2
python3 -m unittest discover -s tests -v
```

Consistency is enforced structurally, not by eyeball:

- every `DT###` / `DTC###` rule has a fixture or test that trips it, and appears
  in this README (a rule you can't look up doesn't exist)
- every custom instrument in `instruments.router.yaml` is charted in the dashboard
  and documented in `dql-queries.md`
- the committed dashboard JSON must match `tiles.yaml`, with no overlapping tiles,
  no out-of-grid tiles, and no `singleValue` tile whose `recordField` isn't
  assigned in its query
- known-good production router and collector configs must produce zero errors — a
  rule that fires on those is a false positive. Point the suite at your own with
  `DT_REAL_ROUTER_CONFIGS` / `DT_REAL_COLLECTOR_CONFIGS` (colon-separated paths);
  the checks skip when unset
- the harness config must *fail* validation without `--allow-loopback`
- every `${env.*}` read by any template or demo config must be documented in
  `.env.example`, and no `.env.example` assignment may hold a real-looking token
- the demo subgraphs must answer the router's actual query shapes, including the
  `_entities` fetch, and the demo configs must contain no non-Dynatrace backend

## Operational notes

- **Operation-count mismatches.** `dynatrace.router.requests` (HTTP requests),
  `dynatrace.graphql.operations` (GraphQL operations), and GraphOS Studio's
  reported operations are three different numbers. Batching, subscriptions, and
  `field_level_instrumentation_sampler` drive them apart. See §3 of the DQL pack.
- **Cardinality is billable.** Every attribute multiplies series count.
  `graphql.operation.name` ships off for that reason — turn it on for an
  investigation, then turn it back off.
- **Sampling.** `sampler: 1.0` will get you rate-limited; the templates ship
  `0.05`–`0.1` with `parent_based_sampler: true`.
- **Dual instrumentation.** If subgraphs run OneAgent while the router exports
  OTLP, align propagation and sampling on one source of truth or traces duplicate
  and disconnect.
- **Rhai `traceid()` returns the span id**, not the root trace id.

## Provenance

Distilled from working Dynatrace deployments — direct OTLP tracing and logging,
collector fan-out to a local Jaeger alongside Dynatrace with logs correlated by
`trace_id` via `filelog`, and a real Platform dashboard imported through the
Documents API — each of which covered part of the picture. This pack adds the
metrics exporter and instruments, config validation for both topologies, and an
export harness that runs without a tenant.

## Sources

- [Apollo: Dynatrace metrics exporter](https://www.apollographql.com/docs/graphos/routing/observability/router-telemetry-otel/apm-guides/dynatrace/dynatrace-metrics)
- [Apollo: Dynatrace traces exporter](https://www.apollographql.com/docs/graphos/routing/observability/router-telemetry-otel/apm-guides/dynatrace/dynatrace-traces)
- [Apollo: OTLP metrics exporter](https://www.apollographql.com/docs/graphos/routing/observability/router-telemetry-otel/telemetry-pipelines/metrics-exporters/otlp)
- [Apollo: Instruments](https://www.apollographql.com/docs/graphos/routing/observability/router-telemetry-otel/enabling-telemetry/instruments)
- [Apollo: Standard metric instruments](https://www.apollographql.com/docs/graphos/routing/observability/router-telemetry-otel/enabling-telemetry/standard-instruments)
- [Dynatrace: OTLP export](https://docs.dynatrace.com/docs/extend-dynatrace/opentelemetry/getting-started/otlp-export)
