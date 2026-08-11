# GraphOS Studio vs Dynatrace: comparing the numbers

Both describe the same router, over two independent pipelines. **Request counts
match exactly. Latency matches too — but only if you read it from spans.** The
differences below are all explainable; none of them mean telemetry is broken.

## What differs

| | GraphOS Studio | Dynatrace |
|---|---|---|
| Pipeline | Apollo Usage Reporting (protobuf) | OTLP |
| Enabled by | `APOLLO_KEY` + `APOLLO_GRAPH_REF` | `telemetry.exporters.*` |
| Unit measured | one GraphQL **operation** | one **HTTP request** |
| Latency percentiles available | p50, p90, p99 only | any, from spans |
| Latency unit | milliseconds | seconds (metric), nanoseconds (spans) |
| Errors counted | GraphQL errors + request failures | depends which metric you read |
| Sampling | unsampled | traces sampled (`0.05`–`0.1` in these templates) |

## Sample metrics, identical traffic

One router reporting to both at once. 240 requests, three operations, one hour,
latency stepped deliberately (0 → 120 → 350 ms of subgraph delay).

**Request counts — exact agreement:**

| | Studio | Dynatrace |
|---|---|---|
| Requests in the hour | 240 (80 x 3 operations) | 240 (`dynatrace.router.requests`) |

**Latency — Studio vs percentiles from spans:**

| Operation | p50 Studio / spans | p90 Studio / spans | p99 Studio / spans |
|---|---|---|---|
| A (2 subgraph fetches) | 249.4 / **252** ms | 730.8 / **713** ms | 752.3 / **715** ms |
| B (1 fetch) | 127.0 / **126** ms | 374.5 / **357** ms | 386.0 / **358** ms |
| C (1 fetch) | 127.3 / **127** ms | 375.0 / **357** ms | 386.0 / **360** ms |

p50 agrees within ~1%. The gap widens to 5–8% at p99, Studio always higher —
partly its log-scale buckets rounding up, partly that at 80 samples the p99 is the
79th-of-80 value. At production volumes expect it to narrow toward the p50
agreement; don't quote 5–8% as a general figure.

**Errors — same hour of traffic containing GraphQL errors and validation failures:**

| Source | Count |
|---|---|
| Studio `REQUEST_WITH_ERROR_COUNT` | **22** |
| `apollo.router.graphql_error` | **22** |
| `dynatrace.subgraph.errors` | 14 |
| 5xx counter (`dynatrace.router.server.errors`) | **0 — no series** |
| `dynatrace.router.requests` by status | 194x `200`, 8x `400` |

GraphQL errors return **HTTP 200** and validation failures **400**, so a 5xx
counter correctly reads zero while Studio reports 22.

## Comparing correctly

| Compare | Against | Not |
|---|---|---|
| Studio request count | `dynatrace.router.requests` | — |
| Studio p50/p90/p99 | percentiles over **spans** | `percentile()` on the duration metric |
| Studio errors | `apollo.router.graphql_error` | the 5xx counter |
| Anything | same time window, same units | Dynatrace's own service response-time view (span-derived and sampled) |

Pick **p90 or p99** on both sides. Studio exposes no p95, so a p95 tile has no
counterpart.

> New to histogram buckets or `rollup`? [`percentiles-and-buckets.md`](percentiles-and-buckets.md) explains the mechanism behind every recommendation here, with a worked example.

## Why percentiles must come from spans

`percentile()` on a metric requires a `rollup:`, and rollup collapses each time
slot to a single value *before* the percentile is taken. Measured over the same
hour as the table above:

| | p50 | p90 | p99 |
|---|---|---|---|
| `percentile(http.server.request.duration, N, rollup: avg)` | 0.4753 | 0.4753 | 0.4753 |

Byte-identical — it is the average, labelled as a percentile. The true p90 was
713 ms. Comparing Studio's 730 ms against that 475 ms suggests a 35% discrepancy
that does not exist.

Spans carry one `duration` per request, so a percentile over spans is a real
percentile:

```dql
fetch spans
| filter service.name == "apollo-router" and request.is_root_span == true
| makeTimeseries {
    p50 = percentile(duration, 50),
    p90 = percentile(duration, 90),
    p99 = percentile(duration, 99)
  }
```

## If you do chart the histogram metric

Bucket width is the error bar — percentiles are interpolated inside whichever
bucket they land in. The router's defaults (seconds):

```
0.001, 0.005, 0.015, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 1.0, 5.0, 10.0
```

| True p95 | Bucket | Width | Error |
|---|---|---|---|
| 180 ms | 0.15–0.2 | 33% | a few ms |
| 450 ms | 0.4–0.5 | 25% | tens of ms |
| 800 ms | **0.5–1.0** | 100% | **hundreds of ms** |
| 2.1 s | **1.0–5.0** | **400%** | **seconds** |

Studio's usage-reporting histogram is log-scale with hundreds of buckets, each
~10% wider than the last, so it stays accurate across the whole range. Fix the
router side with `templates/histogram-buckets.router.yaml`: boundaries either side
of your p95/p99 region and a top boundary at or above the request timeout. Rule
`DT026` flags configs still on the defaults.

Also avoid averaging percentiles across intervals: `rollup: avg` with
`interval: 1m` computes 60 per-minute p95s and averages them. Set `interval` to
the whole comparison window so Dynatrace merges before computing, as Studio does.
Per-minute is still the right chart for *trend* — just not for *comparison*.

## It is not temporality

Delta vs cumulative changes how counts accumulate between exports, not where a
percentile falls. Studio is not on the OTLP metrics pipeline at all — usage
metrics travel over the Apollo Usage Reporting protocol, which has no
`temporality` setting. Flipping temporality and re-measuring leaves the p95
unchanged.

## Other reasons the two drift

- **More than one router on the graph.** Studio aggregates every router reporting
  to a graph variant; a Dynatrace tile is filtered to one `service.name`. Totals
  only match when a single router feeds the variant — compare per operation, or
  give each router its own variant.
- **Rejected operations are named differently.** An operation that fails
  validation has no name to report: Studio files it under
  `# GraphQLValidationFailure`, Dynatrace records the root span as
  `GraphQL Operation`. Same requests, two labels.
- **Batching.** One HTTP request carrying 5 operations is 5 measurements in
  Studio and 1 in `dynatrace.router.requests`.
- **Non-GraphQL traffic.** Health checks and introspection hit the same listener:
  excluded from Studio, included in the HTTP metric unless filtered.
- **Clock skew.** GraphOS rejects reports older than 50 minutes
  (`Rejecting report … with skewed timestamp`). A host with drifting time loses
  Studio data while Dynatrace keeps working — an NTP problem that looks like a
  metrics problem.

## Pulling the Studio numbers

```bash
curl -s https://api.apollographql.com/api/graphql \
  -H "Content-Type: application/json" \
  -H "X-API-KEY: $APOLLO_KEY" \
  -H "apollographql-client-name: comparison" \
  -H "apollographql-client-version: 1.0" \
  -d '{"query":"query I($id: ID!) { graph(id: $id) { operationInsightsTimeseriesReport(dimensions: [OPERATION_NAME], metrics: [REQUEST_COUNT, REQUEST_LATENCY_P50_MS, REQUEST_LATENCY_P90_MS, REQUEST_LATENCY_P99_MS, REQUEST_WITH_ERROR_COUNT], resolution: HOUR, from: \"-518400\", to: \"-0\", limit: 200) { csv } } }","variables":{"id":"YOUR_GRAPH_ID"}}'
```

Both `apollographql-client-name` **and** `-client-version` headers are required.
`HOUR` resolution accepts a window under 7 days — exactly `-604800` is rejected.

## Sources

- [Router: metrics exporters — `buckets`, `views`, `cardinality_limit`](https://www.apollographql.com/docs/graphos/routing/observability/router-telemetry-otel/telemetry-pipelines/metrics-exporters/overview)
- [GraphOS reporting (Apollo Usage Reporting protocol; OTLP for traces only)](https://www.apollographql.com/docs/graphos/routing/observability/graphos/graphos-reporting)
- [Sending metrics to GraphOS — report format, 50-minute skew limit](https://www.apollographql.com/docs/graphos/platform/insights/sending-operation-metrics)
