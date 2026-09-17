# Dynatrace Dashboard Template

This folder contains a ready-to-import [Dynatrace Platform dashboard](./dashboard-template.json)
for Apollo Router (GraphOS Runtime) telemetry, plus the router configuration that feeds it.

The dashboard JSON is a generated artifact. You will edit it yourself in one common case — see
[the blank-dashboard warning](#before-you-import-the-servicename-filter) below before importing.

**The code in this repository is experimental and has been provided for reference purposes only.
Community feedback is welcome but this project may not be supported in the same way that
repositories in the official [Apollo GraphQL GitHub organization](https://github.com/apollographql)
are. If you need help you can file an issue on this repository,
[contact Apollo](https://www.apollographql.com/contact-sales) to talk to an expert, or create a
ticket directly in Apollo Studio.**

## Contents

| File | What it is |
| --- | --- |
| `dashboard-template.json` | The importable Platform dashboard (version-17 document format) |
| `import_dashboard.sh` | Uploads the JSON via the Dynatrace Platform Documents API |
| `complete-configuration.router.yaml` | **Start here.** Exporters + instruments + spans + histogram bucket tuning, pre-merged into one paste-ready block |
| `dynatrace.router.yaml` | Same exporter config, split out for review |
| `instruments.router.yaml` | Same metrics config, split out for review |
| `spans.router.yaml` | Same span config, split out for review |
| `histogram-buckets.router.yaml` | Optional — same bucket tuning as in the complete config, split out for review, with a per-instrument override example |
| `dql-queries.md` | The DQL query shapes, one per instrument, with a Good/Concerning read on each |

## Prerequisites

- Apollo Router 2.x (GraphOS Runtime), exporting OTLP either directly to Dynatrace or through an
  OpenTelemetry Collector.
- A Dynatrace SaaS or Managed environment with Grail (Platform dashboards and DQL).
- The right tokens. There are three, and they are easy to mix up:

| Purpose | Header | Token / scopes |
| --- | --- | --- |
| OTLP ingest | `Authorization: Api-Token dt0c01...` | access token with `openTelemetryTrace.ingest`, `metrics.ingest`, `logs.ingest` |
| Metrics API read (verifying ingest) | `Authorization: Api-Token dt0c01...` | access token with `metrics.read` |
| Dashboard import | `Authorization: Bearer dt0s16...` | platform token with `document:documents:write` (or an OAuth client with the same permission) |

The ingest token and the dashboard token are different credential types with different prefixes
and different headers. Using the ingest `Api-Token` against the dashboard API returns a 401 that
looks like an ingest problem; it isn't.

## Router configuration

Copy [`complete-configuration.router.yaml`](./complete-configuration.router.yaml) into your
`router.yaml` — it's exporters, instruments, and spans in one paste-ready `telemetry:` block, the
same pattern Apollo's Datadog and New Relic guides use.

<!-- markdown-link-check-disable -->
> **Deploying only the exporters, with no instruments, is the most common reason a freshly
> imported dashboard is mostly empty.** Every `dynatrace.*` custom counter, `apollo.router.overhead`,
> and `subgraph.name` attribution live in the instruments/spans blocks, not in the exporter config.
> If your dashboard looks sparse, confirm your router's actual telemetry config includes the full
> `instrumentation.instruments` and `instrumentation.spans` blocks below — not just `exporters`.
<!-- markdown-link-check-enable -->

If you'd rather review these concerns separately before merging them yourself, the same content is
also split out:

- [`dynatrace.router.yaml`](./dynatrace.router.yaml) — the exporters: where telemetry goes and how
  it authenticates.
- [`instruments.router.yaml`](./instruments.router.yaml) — the metrics: standard OTel instruments
  plus four custom counters (`dynatrace.router.requests`, `dynatrace.router.server.errors`,
  `dynatrace.graphql.operations`, `dynatrace.subgraph.errors`) that the dashboard charts directly.
- [`spans.router.yaml`](./spans.router.yaml) — the spans: operation-name grouping and, critically,
  `otel.status_code: ERROR` on GraphQL errors.
- [`histogram-buckets.router.yaml`](./histogram-buckets.router.yaml) — optional: tighter histogram
  boundaries than the router's coarse defaults, plus a per-instrument override example. Nothing in
  this dashboard needs it (every percentile tile reads from spans, not this histogram), but it's a
  real fix if you build a metric-based percentile of your own.

Full walkthroughs live in Apollo's docs:
[Dynatrace metrics](https://www.apollographql.com/docs/graphos/routing/observability/router-telemetry-otel/apm-guides/dynatrace/dynatrace-metrics)
and
[Dynatrace traces](https://www.apollographql.com/docs/graphos/routing/observability/router-telemetry-otel/apm-guides/dynatrace/dynatrace-traces).

### Some tiles only populate once a feature is configured

A handful of tiles are empty by design until a specific router feature is turned on — this is true
of the Datadog and New Relic templates too (New Relic ships dedicated "Coprocessors" and "Entity
Caching" pages that are blank without them):

| Section | Needs |
| --- | --- |
| Response Cache by Subgraph | GraphOS Router Enterprise + a Redis-backed cache store (entity caching) |
| Connector HTTP (both tiles) | Apollo Connectors in use |
| Coprocessor Calls / Duration | `coprocessor.url` configured |
| Router Error Logs | Router stdout forwarded to Dynatrace (no OTLP log exporter exists — see the non-negotiables above) |

The rest of the dashboard — traffic, errors, latency, saturation, query planning/cache hit-miss —
needs only the `complete-configuration.router.yaml` block and real traffic (including some repeated
queries, so the query-plan cache actually warms, and at least one GraphQL error, so the error tiles
have something to show). If those tiles are still empty after confirming the full instrumentation
block is deployed, query the Dynatrace Metrics API or Grail directly for the underlying metric name
(see [`dql-queries.md`](./dql-queries.md)) to check whether it's arriving with no matching data
versus not arriving at all — the troubleshooting table below covers the most common silent failures.

The non-negotiables, because each failure mode is silent or misleading:

- `temporality: delta` on the metrics exporter. Dynatrace accepts cumulative metrics with a 2xx
  and then drops them — the config looks correct and no counter ever appears.
- `protocol: http` on **every** signal. Dynatrace has no gRPC OTLP ingest, and an omitted
  `protocol` defaults to gRPC.
- Per-signal endpoint paths: `/api/v2/otlp/v1/metrics`, `/api/v2/otlp/v1/traces`,
  `/api/v2/otlp/v1/logs` — each exporter gets its own path. A traces path on the metrics exporter
  is accepted, then discarded.
- An explicit port on the endpoint host (`https://<env-id>.live.dynatrace.com:443`).
- `Authorization: "Api-Token ${env.DYNATRACE_API_TOKEN}"` — `Api-Token`, not `Bearer`, and the
  token via an environment variable, never inlined.
- `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, and
  `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT` must be unset. Router v2.12 and earlier let them silently
  override the telemetry config; v2.13+ refuses to start.

One more, structural: **router logs do not reach Dynatrace directly.** The router has no OTLP log
exporter — ship its JSON stdout with a collector `filelog` receiver or an existing host forwarder.
The dashboard's error-log tile stays empty until you do.

### If you route through an OpenTelemetry Collector

The router side simplifies (plain OTLP to the collector), and the Dynatrace constraints move into
the collector config:

- The Dynatrace exporter must be `otlphttp` — same no-gRPC rule, one layer down.
- The exporter `endpoint` must **not** include a `/v1/<signal>` suffix; the exporter appends the
  signal path itself. A `/v1/traces` left on the endpoint produces 404s.
- Same `Authorization: Api-Token ...` header.
- If the metrics pipeline is fed by a Prometheus scrape (or anything else cumulative), it needs
  the `cumulativetodelta` processor — otherwise you get the same silent 2xx-then-dropped behavior
  as `temporality: cumulative` on the router.

See [Dynatrace's OTLP export documentation](https://docs.dynatrace.com/docs/extend-dynatrace/opentelemetry/getting-started/otlp-export)
for the ingest API's own statement of these rules.

## Before you import: the `service.name` filter

**Every tile in this dashboard filters on `service.name == "apollo-router"`.** If your router
reports any other service name, the import succeeds and every tile is empty — there is no error
anywhere, just a blank dashboard.

Dynatrace Platform dashboards have no template variables, so the filter is baked into each query.
If your `telemetry.exporters.*.common.service_name` is anything other than `apollo-router`,
re-target the JSON before importing. The string `apollo-router` appears in the file only inside
that filter, so a plain find-and-replace is safe and complete:

```shell
# a different service name (macOS/BSD sed: use  sed -i '' )
sed -i 's/apollo-router/my-supergraph/g' dashboard-template.json
```

For a compound filter, replace the whole expression instead — note the escaped quotes, because the
DQL lives inside JSON strings:

```shell
python3 - <<'EOF'
import pathlib
p = pathlib.Path("dashboard-template.json")
p.write_text(p.read_text().replace(
    'service.name == \\"apollo-router\\"',
    'service.name == \\"router\\" and k8s.namespace.name == \\"prod\\"'))
EOF
```

Then import as below.

## Import

### Option 1: script, via the Platform Documents API

Platform dashboards are documents, imported by POSTing to
`/platform/document/v0/documents` (or `v1`, which the script uses by default — set
`DOCUMENT_API_VERSION=v0` if your tenant still serves v0) on the `.apps.` host. This API wants the
**Bearer platform token** (`dt0s16...`, scope `document:documents:write`) — explicitly **not** the
OTLP ingest token. The 401 you get with the wrong one looks like an ingest problem.

```shell
DT_ENVIRONMENT_ID=abc12345 \
DT_BEARER_TOKEN=dt0s16.XXXX.YYYY \
./import_dashboard.sh
```

For CI or service accounts, OAuth client credentials work instead: set `DT_OAUTH_CLIENT_ID`,
`DT_OAUTH_CLIENT_SECRET`, and `DT_ACCOUNT_UUID` (the OAuth client needs the same
`document:documents:write` permission). The script header documents both flows.

Re-running the script **replaces** the dashboard of the same name — the Documents API has no
upsert, so the script deletes previous copies rather than accumulating duplicates. To keep several
variants side by side (for example, one per environment), name them:

```shell
DASHBOARD_NAME="Apollo Router — Staging" ./import_dashboard.sh
```

### Option 2: manual upload

No token? `dashboard-template.json` is a standard dashboard document. In your Dynatrace
environment, open the **Dashboards** app and use **Upload** to import the file directly.

## What the dashboard shows

Nine sections, organized by concern rather than by metric:

| Section | Question it answers |
| --- | --- |
| Overview | Is the graph healthy right now? Request rate, 5xx, p95, in-flight, SLA compliance |
| Traffic | Shape of the load: status codes, operation types, payload sizes |
| Latency & overhead | Is it the router or the subgraphs? Percentiles, router overhead, slowest subgraphs |
| Errors | Where failures come from: GraphQL error codes, per-subgraph errors, 5xx |
| Saturation | Is the router itself under pressure? Connections, compute-job queue |
| Cache & query planning | Hit rates, planning percentiles, plans generated |
| Connector HTTP | Latency and rate per connector source |
| Coprocessors | Calls and duration by coprocessor stage |
| Traces & Dependencies | Top operations by p99, failed-request exemplars, dependency table, error logs |

Three design decisions worth knowing before you read the tiles:

- **Latency percentiles come from spans, not from the duration histogram.** DQL's `percentile()`
  over a rolled-up metric returns the average — p50, p95, and p99 all come back identical. Spans
  carry one `duration` per request, so span percentiles are real percentiles. Tiles for values no
  span carries (router overhead, payload sizes) report avg/max instead.
- **GraphQL errors are marked via `otel.status_code` on spans.** GraphQL errors return HTTP 200,
  so without the marking in `spans.router.yaml` every failing request looks successful and the
  error tiles stay flat while clients see errors.
- **Some tiles are empty by design, and say so in their titles.** The cache tiles need router
  caching enabled (query-plan cache config; entity caching needs GraphOS Router Enterprise plus
  Redis), the connector tiles need Apollo Connectors, the coprocessor tiles need a configured
  coprocessor, and the error-log tile needs the router's stdout forwarded. The 5xx tiles are
  unlabelled because empty simply means no 5xx occurred.

[`dql-queries.md`](./dql-queries.md) explains every query shape used, and gives a Good/Concerning
reading for each charted metric.

## Troubleshooting

Each of these returns HTTP 200 or 404, produces no data, and logs no router error — so the config
looks correct and the dashboard stays empty:

| Mistake | Symptom |
| --- | --- |
| `temporality: cumulative` (or omitted) | counters silently dropped after a 2xx |
| Prometheus scrape → Dynatrace with no `cumulativetodelta` | same, one layer down |
| `protocol: grpc` (or omitted, which defaults to gRPC) | exporter can't connect; ingest is HTTP-only |
| endpoint without `:443` | connection failures on hosts with no port |
| `Bearer` instead of `Api-Token` (ingest) | 401s buried in exporter logs |
| `Api-Token` instead of `Bearer` (dashboard import) | 401 that looks like an ingest problem |
| collector-style path `/v1/metrics` on the direct endpoint | 404s |
| `/v1/traces` left on the collector's exporter endpoint | 404s |
| traces path on the metrics exporter | accepted, then discarded |
| endpoint on the `.apps.` host instead of `.live.` | 404 on ingest; the host answers, so it looks reachable |
| `ext:` prefix in DQL, or missing in the classic Metrics API | "metric not found" either way — DQL wants the plain dotted key, the Metrics API wants `ext:` |
| router `service.name` ≠ the dashboard's filter | import succeeds, every tile empty — [re-target the filter](#before-you-import-the-servicename-filter) and re-import |

## Feedback

This template is experimental. If something doesn't work against your tenant, or a tile you need
is missing, please file an issue on this repository.
