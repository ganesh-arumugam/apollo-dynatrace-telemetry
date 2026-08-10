# Coming from the Datadog APM template

Apollo publishes APM dashboard templates in
[`apollographql/apm-templates`](https://github.com/apollographql/apm-templates),
and Apollo's docs include a full Datadog guide. This pack is the Dynatrace
equivalent.

If you already run the Datadog setup, the tables below map each piece to its
counterpart here, and show where the two backends work differently — the parts
worth knowing before assuming a config translates one-for-one.

## Artifact-by-artifact

| If you use this on Datadog | The equivalent here |
|---|---|
| `connecting-to-datadog` — choosing a connection method | [README — two topologies](../README.md#two-topologies-both-supported) |
| Connecting via the OpenTelemetry Collector | `templates/collector/otel-collector-dynatrace.yml` + `router-to-collector.router.yaml` |
| Connecting via the Datadog Agent (traces + metrics) | `templates/dynatrace-activegate.router.yaml` — ActiveGate plays a similar role when the router has no direct egress |
| Agentless / direct OTLP ingest | `templates/dynatrace.router.yaml` and the per-signal snippets |
| Router logs into the platform | `templates/dynatrace-logs.router.yaml` (stdout JSON + a log shipper; the router has no OTLP log exporter) |
| `router-instrumentation` — spans, error tracking, instruments | `templates/spans.router.yaml` (spans + GraphQL error marking) and `templates/instruments.router.yaml` (metrics) |
| The dashboard template you import | `dashboards/tiles.yaml` → `dashboards/dynatrace-dashboard.json`, imported with `scripts/import_dashboard.sh` |
| — | Extras with no Datadog counterpart: `scripts/validate_dynatrace.py`, `scripts/validate_collector.py`, `harness/`, `scripts/verify_ingest.sh` |

## What changes between the two backends

Most of the router config carries over. These are the parts that don't:

| Concern | Datadog | Dynatrace |
|---|---|---|
| Metric temporality | accepts cumulative | **delta only** — cumulative is accepted with a 2xx and dropped |
| Transport | OTLP gRPC or HTTP | **HTTP/protobuf only** |
| Auth header | `DD-API-KEY`, or agent-local (no auth) | `Api-Token <token>` for ingest; `Bearer` for the dashboard API |
| Endpoint | agent on `localhost:4317`, or `otlp.datadoghq.com` | per-signal path `/api/v2/otlp/v1/{metrics,traces,logs}`, explicit port |
| Trace grouping | `operation.name` + `resource.name` drive APM views | service + span name; `resource.name` has no meaning |
| Error detection | Error Tracking reads span status + `error.message` | failure analysis reads span status — same `otel.status_code` trick, different UI |
| Dashboard install | paste JSON into a blank dashboard | POST to the Platform Documents API with a Bearer token |
| Dashboard query language | Datadog metric queries + template variables | DQL (`timeseries ... filter:`), no template variables in the generated doc |
| Metric addressing | one name everywhere | plain dotted key in DQL, `ext:`-prefixed in the classic Metrics API |
| Cost driver called out | metric cardinality | metric cardinality **and** DDU ingest volume |

## Where GraphOS Studio fits — neither delta nor cumulative

A fair question when you see `temporality: delta` for Dynatrace and cumulative
for Datadog: what does Studio do? Answer: **Studio is not on the OTLP metrics
pipeline at all**, so temporality never applies to it.

| | Studio usage metrics | Third-party metrics (Dynatrace / Datadog) |
|---|---|---|
| Protocol | Apollo Usage Reporting protocol (protobuf `Report`) | OTLP |
| Endpoint | `https://usage-reporting.api.apollographql.com/api/ingress/traces` | your APM / collector |
| Auth | `X-Api-Key: service:…` (graph API key) | `Api-Token` / `DD-API-KEY` / none |
| Enabled by | `APOLLO_KEY` + `APOLLO_GRAPH_REF` | `telemetry.exporters.metrics.*` |
| Shape | pre-aggregated per operation signature, per report window: counts, latency histogram, field stats | OTel counters/histograms with a temporality |
| Temporality | not a concept — each report *is* an interval | `delta` (Dynatrace) / cumulative (Datadog) |

Because each Apollo report describes one time window, it is semantically already
delta-like: there is no monotonic counter to reset and nothing to negotiate. The
`temporality` setting you put in `telemetry.exporters.metrics.otlp` has **zero**
effect on Studio, which is why one router can feed Studio, a delta Dynatrace
pipeline, and a cumulative Datadog pipeline at the same time.

What *did* move to OTel is Studio **traces**: since router v1.49 they can be sent
over OTLP, and in v2.x that is the default (`telemetry.apollo.otlp_tracing_sampler:
always_on`, gRPC by default, `experimental_otlp_tracing_protocol: http` to switch).
Usage **metrics** still use the Apollo protocol.

Practical consequences:

- When Studio and APM counts disagree, temporality isn't the cause. Look at
  the samplers (`telemetry.apollo.sampler`,
  `telemetry.apollo.field_level_instrumentation_sampler`), at operation-signature
  normalization (aliases, whitespace and string literals are normalized, so
  functionally identical operations are grouped), and at what each side counts —
  HTTP requests vs GraphQL operations vs reported operations.
- Studio has its own silent-loss mode with no OTLP equivalent: the ingress
  **rejects reports older than 50 minutes** (`Rejecting report … with skewed
  timestamp`). Clock drift on a router host loses Studio data while the Dynatrace
  pipeline continues normally — a divergence that can look like a metrics issue.
- Reports batch on their own schedule (~20s, ~4 MB cap), independent of
  `telemetry.exporters.metrics.otlp.batch_processor`. Short windows will never
  line up exactly between the two systems.

## What carries over unchanged

The Datadog `router-instrumentation` guide and this pack agree on the parts that
are really about the router, not the vendor:

- `default_requirement_level: required` on instruments.
- `apollo.router.overhead: true` — router processing time vs downstream wait.
- `http.server.request.duration` with error attribution, and
  `http.client.request.duration` split by `subgraph.name`.
- Marking spans `ERROR` on GraphQL errors, since the HTTP status is 200 either way.
- The cardinality warning about `graphql.operation.name` /
  `subgraph_operation_name` on high-traffic graphs.

## Using both at once

Nothing here conflicts with an existing Datadog or Prometheus setup — the router
can export to more than one metrics backend simultaneously. See
[`docs/prometheus-and-dynatrace.md`](prometheus-and-dynatrace.md), which applies
equally to running Datadog and Dynatrace side by side during an evaluation.
