# Querying router telemetry in Dynatrace

There are only five query shapes. Once you know which shape a metric needs you can
write the query yourself rather than looking one up, so this explains the shapes and
then lists which metric takes which.

For *which* metrics exist and why you would want them, see
[`docs/metrics.md`](../docs/metrics.md). This document is about reading them in
Dynatrace.

## Three things decide the shape

**1. The instrument type decides the aggregation.** This is the one thing you cannot
guess:

| Type | Aggregate with | Getting it wrong gives you |
|---|---|---|
| counter | `sum()` | — |
| histogram | `avg()` / `max()` | `sum()` of a histogram is meaningless |
| gauge | `avg()` / `max()` | `sum()` across instances double-counts |

**2. Percentiles come from spans, never from a metric.** `percentile()` on a metric
requires a `rollup:`, and rollup collapses each interval to a single value *before*
the percentile is taken, so p50, p95 and p99 all return the same number: the
average. See [percentiles-and-buckets.md](../docs/percentiles-and-buckets.md).

**3. Counters arrive as delta.** Dynatrace requires delta temporality, so `sum()`
over an interval *is* the count in that interval. Do not apply a delta function on
top of it.

One surface difference worth knowing before debugging a missing metric:

| Surface | How to address the metric |
|---|---|
| Grail / DQL — notebooks and dashboards | plain dotted key: `sum(dynatrace.router.requests)` |
| Classic Metrics API v2 | `ext:` prefix: `ext:dynatrace.router.requests` |

Using `ext:` in DQL returns nothing; omitting it in the Metrics API returns 404.
Neither looks like a spelling mistake — both look like an ingest failure.

---

## The five shapes

### A. Counter — how much of something happened

```dql
timeseries requests = sum(dynatrace.router.requests),
  filter: {service.name == "apollo-router"}
```

Swap in any counter. Add `, scalar: true` inside `sum()` for a single number instead
of a series, which is how you confirm a metric exists at all.

### B. Counter split by a dimension — where it happened

```dql
timeseries errors = sum(dynatrace.subgraph.errors),
  by: {subgraph.name},
  filter: {service.name == "apollo-router"}
```

The `by:` clause turns a graph-wide number into one that names an owner. This is the
shape for `dynatrace.router.requests` by `http.response.status_code`,
`dynatrace.graphql.operations` by `graphql.operation.type`,
`apollo.router.graphql_error` by `code`, `apollo.router.response.cache` by
`subgraph.name`, and `apollo.router.operations.coprocessor` by `coprocessor.stage`.

### C. Histogram — average and peak

```dql
timeseries {
    avg_value = avg(http.server.request.body.size),
    max_value = max(http.server.request.body.size)
  },
  filter: {service.name == "apollo-router"}
```

Two series, because the average alone hides the tail and the max alone is noise.
Use this for every duration and size metric where no span carries the value:
`apollo.router.overhead`, `apollo.router.compute_jobs.duration`,
`apollo.router.compute_jobs.execution.duration`,
`apollo.router.compute_jobs.queue.wait.duration`,
`apollo.router.query_planning.plan.duration`, `http.client.request.duration`,
`http.server.response.body.size`, `apollo.router.cache.hit.time.count`,
`apollo.router.cache.miss.time.count`, and
`apollo.router.operations.coprocessor.duration`.

**Multi-series syntax matters.** Inside `{ }` a `rollup:` must go inside each
function call; a command-level `rollup:` raises `UNKNOWN_PARAMETER_DEFINED`.

### D. Percentile — from spans

```dql
fetch spans
| filter service.name == "apollo-router" and request.is_root_span == true
| makeTimeseries {
    p50 = percentile(duration, 50),
    p95 = percentile(duration, 95),
    p99 = percentile(duration, 99)
  }
```

Change the `filter` to choose what you are measuring: `request.is_root_span == true`
for client-observed latency, `span.name == "subgraph"` for subgraph latency (add
`by: {subgraph.name}`), `span.name == "query_planning"` for planning.

Span `duration` is in **nanoseconds**. The metric equivalents are in seconds and
Studio reports milliseconds — three scales for one quantity.

### E. Gauge — current level and peak

```dql
timeseries {
    current = avg(apollo.router.open_connections),
    peak = max(apollo.router.open_connections)
  },
  filter: {service.name == "apollo-router"}
```

Also the shape for `http.server.active_requests` and
`apollo.router.compute_jobs.queued`.

**Do not use this shape on an UpDownCounter.**
`apollo.router.compute_jobs.active_jobs` and `apollo.router.opened.subscriptions`
report negative or corrupted values under delta temporality, because one dropped
delta corrupts the running total until the router restarts. Dynatrace requires
delta, so these cannot be charted reliably. (`apollo.router.cache.redis.connections`
used to be in this list under an older name — it's `apollo.router.cache.redis.clients`
now, a gauge, and charts fine.)

---

## Going from a metric to a working chart

The shapes tell you what to write; this is the order to do it in.
`dynatrace.subgraph.errors` is the example because it is a custom counter and so
exercises every step.

**1. Enable it.** In
[`instruments.router.yaml`](../templates/instruments.router.yaml), under
`telemetry.instrumentation.instruments.subgraph`:

```yaml
dynatrace.subgraph.errors:
  value: unit
  type: counter
  unit: "{error}"
  description: "Subgraph responses that carried GraphQL errors"
  condition:
    eq:
      - true
      - subgraph_on_graphql_error: true
  attributes:
    subgraph.name: true          # the attribution that makes it actionable
```

Run `python3 scripts/validate_dynatrace.py your-router.yaml` before restarting.
Rules `DT012` and `DT013` catch naming and type mistakes the router accepts
silently.

**2. Confirm it arrived, before building anything.** A counter that has never
incremented is never created, so "no series" is ambiguous between *misconfigured*
and *nothing has failed yet*:

```dql
timeseries n = sum(dynatrace.subgraph.errors, scalar: true),
  filter: {service.name == "apollo-router"}, from: -2h
```

If that returns nothing and you expected data, widen the window before changing any
config — it distinguishes a wrong query from a quiet metric. Metrics need one batch
interval plus ingest lag, usually under a minute, and `scripts/verify_ingest.sh`
waits for it rather than reporting a false negative.

Some metrics never arrive because Dynatrace rejects the type at ingest, which leaves
no error in the router log — see
[`docs/metrics.md` §15](../docs/metrics.md#15-metrics-dynatrace-rejects).

**3. Chart it** using shape B.

**4. Place it.** Add a tile to [`tiles.yaml`](tiles.yaml) and rebuild, rather than
editing the dashboard JSON — a test asserts the two match.

---

## What each metric is telling you

Shape from the tables above; this is what to conclude.

### Traffic and health

| Metric | Shape | Good | Concerning |
|---|---|---|---|
| `dynatrace.router.requests` | B, by status code | tracks your known traffic shape, 2xx dominant | a step change with no deploy behind it |
| `apollo.router.graphql_error` | B, by `code` | near zero | a new code right after a schema publish, usually a nullability or value-completion regression |
| `dynatrace.router.server.errors` | A | flat zero. A router 5xx means no GraphQL response was produced at all | anything sustained |
| `dynatrace.graphql.operations` | B, by operation type | matches what clients should be doing | a mutation or subscription share that jumps without a client release |
| `http.server.active_requests` | E | a stable band proportional to request rate | a climbing floor that never returns to baseline |
| root span `duration` | D | p99 within ~3x of p50, stable across deploys | p99 detaching from p50: a subset of operations is degrading |
| `http.server.request.body.size` · `http.server.response.body.size` | C | steady | growing responses, usually an over-fetching client |

Three of these count different things and will not match.
`dynatrace.router.requests` counts HTTP requests,
`dynatrace.graphql.operations` counts GraphQL operations — one batched request
carries several — and Studio counts reported operations and is sampled. Check
batching and samplers before treating a gap as a defect.

### Router versus subgraph

| Metric | Shape | Good | Concerning |
|---|---|---|---|
| `apollo.router.overhead` | C, **filtered** — see below | single-digit milliseconds, flat | rising while subgraph latency is flat: the router is the bottleneck |
| `http.client.request.duration` | C or D, by `subgraph.name` | each subgraph inside its own budget | one subgraph climbing while the rest hold |
| `dynatrace.subgraph.errors` | B, by `subgraph.name` | zero, or a small constant from known partial-data paths | a spike isolated to one subgraph |

**`apollo.router.overhead` has three pitfalls, and any one makes it meaningless:**

```dql
timeseries {
    avg_overhead = avg(apollo.router.overhead),
    max_overhead = max(apollo.router.overhead)
  },
  filter: {service.name == "apollo-router" and subgraph.active_requests == "false"}
```

1. **Filter `subgraph.active_requests == "false"`**, or you average router-only time
   together with router-while-waiting-on-a-subgraph. The value is a **string**:
   `== false` unquoted matches nothing and the chart is silently empty.
2. **Coprocessor time and plugin network calls are counted inside overhead**, so a
   Rhai script or coprocessor that makes a call inflates it. Easily mistaken for a
   router regression.
3. **It is meaningless when the router is CPU-bound.** Keep CPU under ~50%; above
   that you are measuring scheduling delay.

Units are seconds. Compare only within one router version, since work moves in and
out of the router's critical path between releases.

### Saturation, planning and cache

| Metric | Shape | Good | Concerning |
|---|---|---|---|
| `apollo.router.compute_jobs.queued` | E | near zero, work starts as it arrives | climbing. A leading indicator that CPU-based autoscaling does not detect |
| `apollo.router.compute_jobs.queue.wait.duration` vs `apollo.router.compute_jobs.execution.duration` | C, both | wait far below execution | wait approaching execution: the pool is the constraint |
| `apollo.router.compute_jobs.duration` | C, by `job.type` | steady for `query_parsing` and `query_planning` | planning rising, usually large or newly deployed operations missing from cache |
| `apollo.router.query_planning.plan.duration` | C | stable | growing with schema size or operation complexity |
| `apollo.router.cache.hit.time.count` · `apollo.router.cache.miss.time.count` | C, by `kind` | plan-cache hit rate near 1.0 in steady state | not recovering after a deploy: warm-up misconfigured or cache undersized |
| `apollo.router.response.cache` | B, by `subgraph.name` and `cache.hit` | hits dominant on subgraphs you added cache control to | all misses on a subgraph you expected to cache |
| `apollo.router.open_connections` | E | flat, well under your ingress or file-descriptor limit | a rising floor: connections held open rather than turned over |

### Coprocessor

| Metric | Shape | Good | Concerning |
|---|---|---|---|
| `apollo.router.operations.coprocessor` | B, by `coprocessor.stage` | one stage, low and flat, succeeding | failures at any stage |
| `apollo.router.operations.coprocessor.duration` | C, by `coprocessor.stage` | well under your latency budget | comparable to total request latency: the coprocessor is the bottleneck |

Both require a configured coprocessor to exist at all. The operation name is
not propagated into the Router→coprocessor span, so Dynatrace shows only `/` and
coprocessor cost cannot be attributed to an operation.

---

## Queries that are not one of the five shapes

A few tiles need a pipeline rather than a plain `timeseries`.

**Rank subgraphs by average latency** — collapse each series to a scalar, then sort:

```dql
timeseries lat = avg(http.client.request.duration),
  by: {subgraph.name},
  filter: {service.name == "apollo-router"}
| summarize avg_lat = avg(arrayAvg(lat)), by: {subgraph.name}
| sort avg_lat desc
```

**Compute a percentage from spans** — here, requests inside a 3 second budget:

```dql
fetch spans
| filter service.name == "apollo-router" and request.is_root_span == true
| fieldsAdd meets_sla = if(request.is_failed == false and duration < 3s, 1, else: 0)
| summarize total = count(), compliant = sum(meets_sla)
| fieldsAdd sla_compliance_pct = (compliant * 100.0) / total
```

**List failed requests with trace IDs**, so a reader can open the trace:

```dql
fetch spans
| filter service.name == "apollo-router" and request.is_root_span == true
  and request.is_failed == true
| fields timestamp, span.name, duration, trace.id
| sort timestamp desc
| limit 20
```

The same pipeline shape builds a dependency map (`summarize` by `subgraph.name`), a
top-operations table (`summarize` by span name with a percentile), and an error-log
table where router logs are being forwarded — the router has no OTLP log exporter,
so that requires shipping stdout.

---

## Sources

- [Router: standard metric instruments](https://www.apollographql.com/docs/graphos/routing/observability/router-telemetry-otel/enabling-telemetry/standard-instruments)
- [Router: metrics exporters — buckets, views, cardinality limits](https://www.apollographql.com/docs/graphos/routing/observability/router-telemetry-otel/telemetry-pipelines/metrics-exporters/overview)
