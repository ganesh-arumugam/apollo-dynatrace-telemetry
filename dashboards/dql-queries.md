# Apollo Router on Dynatrace — DQL query pack

One query per instrument, each with a **Good / Bad** reading so an on-call
engineer who has never seen the router can still interpret the chart. This is the
Every tile carries the same kind of note, so a dashboard is readable by someone
who has never seen the router before.

Queries use the form proven against a live tenant (an inline `filter:` on the
`timeseries` command, `by:` for dimensions, `| summarize ... | sort | limit` for
tables). The same queries back `dashboards/tiles.yaml`, and the test suite asserts
that every metric charted in the dashboard is documented here.

**Two surfaces, two spellings of the same metric**

| Surface | How to address the metric | Example |
|---|---|---|
| Grail / DQL (notebooks, dashboards) | plain dotted key | `sum(dynatrace.router.requests)` |
| Classic Metrics API v2 (`/api/v2/metrics/query`) | `ext:` prefix | `ext:dynatrace.router.requests` |

Using `ext:` in DQL returns nothing; omitting it in the Metrics API returns 404.
Neither looks like a spelling problem — both look like an ingest failure.

**Delta temporality**: all counters arrive as delta, so `sum()` over an interval
*is* the count in that interval. Do not apply a delta function again.

**Percentiles**: `rollup: avg` over 1-minute intervals computes a p95 per minute
and then averages them — good for a trend line, but not the percentile for the
window. To
compare against GraphOS Studio (which merges histograms across the whole selected
range), set `interval` to the full comparison window instead. And remember the
error bar is the bucket width: on the router's default boundaries a p95 between
1s and 5s is interpolated inside a bucket 400% wide. See
`docs/studio-vs-dynatrace-latency.md` and `templates/histogram-buckets.router.yaml`.

---

## 1. Golden signals

### Request rate

```dql
timeseries requests = sum(dynatrace.router.requests),
  by: {http.response.status_code},
  filter: {service.name == "apollo-router"}
```

**Good**: tracks your known traffic shape; 2xx dominates.
**Bad**: a step change with no deploy behind it. Compare against
`http.server.active_requests` — rate down while active climbs means requests are
piling up behind a slow dependency rather than disappearing.

### Error rate (5xx)

```dql
timeseries errors = sum(dynatrace.router.server.errors),
  filter: {service.name == "apollo-router"}
```

**Good**: flat zero. A router 5xx is not a GraphQL error — it means the request
never produced a GraphQL response at all.
**Bad**: anything sustained. Correlate with `apollo.router.overhead` (router
saturation) and subgraph latency (downstream slowness) to see which layer it is.

### Latency percentiles (client-observed)

```dql
timeseries {
    p50 = percentile(http.server.request.duration, 50, rollup: avg),
    p95 = percentile(http.server.request.duration, 95, rollup: avg),
    p99 = percentile(http.server.request.duration, 99, rollup: avg)
  },
  filter: {service.name == "apollo-router"}
```

**Good**: p99 within ~3x of p50 and stable across deploys.
**Bad**: p99 detaching from p50 means a subset of operations is degrading. Split
by subgraph before blaming the router — this histogram includes downstream wait.

### In-flight requests

```dql
timeseries active = avg(http.server.active_requests), rollup: avg,
  filter: {service.name == "apollo-router"}
```

**Good**: a stable band proportional to request rate.
**Bad**: a climbing floor that never returns to baseline — connections held open
by subgraph timeouts, leaking subscriptions, or a stalled coprocessor.

---

## 2. Is it the router or the subgraphs?

### Router overhead (the question customers actually ask)

```dql
timeseries {
    p50 = percentile(apollo.router.overhead, 50, rollup: avg),
    p99 = percentile(apollo.router.overhead, 99, rollup: avg)
  },
  filter: {service.name == "apollo-router"}
```

`apollo.router.overhead` excludes time waiting on subgraphs and connectors, so it
isolates parsing, validation, query planning, response composition, and plugin
(Rhai/coprocessor) execution.

**Good**: single-digit milliseconds, flat.
**Bad**: rising overhead with flat subgraph latency = the router is the
bottleneck; check CPU saturation and the query-planner metrics below. Compare only
within one router version — overhead shifts between versions.

### Subgraph latency, by subgraph

```dql
timeseries p95 = percentile(http.client.request.duration, 95),
  by: {subgraph.name}, rollup: avg,
  filter: {service.name == "apollo-router"}
```

**Good**: each subgraph inside its own SLO.
**Bad**: one subgraph's p95 climbing while the rest hold — that is where to look,
and it explains a client-facing p99 without any router change.

### Slowest subgraphs, ranked

```dql
timeseries lat = avg(http.client.request.duration),
  by: {subgraph.name},
  filter: {service.name == "apollo-router"}
| summarize avg_lat = avg(arrayAvg(lat)), by: {subgraph.name}
| sort avg_lat desc
```

### Subgraph errors, by subgraph

```dql
timeseries errors = sum(dynatrace.subgraph.errors),
  by: {subgraph.name},
  filter: {service.name == "apollo-router"}
```

**Good**: zero, or a small constant from known partial-data paths.
**Bad**: a spike isolated to one subgraph. Pair with the trace view filtered on
the same subgraph to read the actual error extensions.

---

## 3. GraphQL-level view

### Operations by type

```dql
timeseries operations = sum(dynatrace.graphql.operations),
  by: {graphql.operation.type},
  filter: {service.name == "apollo-router"}
```

**Good**: query/mutation/subscription mix matches what the clients should be doing.
**Bad**: a mutation or subscription share that jumps without a client release.

> **Reconciling with GraphOS operation counts.** These counters answer three
> different questions and will not match:
> `dynatrace.router.requests` counts **HTTP requests**;
> `dynatrace.graphql.operations` counts **GraphQL operations** (one batched HTTP
> request carries several); GraphOS Studio counts **reported operations** and is
> subject to `field_level_instrumentation_sampler`. Check batching,
> subscriptions, and the sampler before treating a delta as a bug.

### GraphQL errors by code

```dql
timeseries errors = sum(apollo.router.graphql_error),
  by: {code},
  filter: {service.name == "apollo-router"}
```

**Good**: near zero. `RESPONSE_VALIDATION_FAILED` here corresponds to
`extensions.valueCompletion` in client responses, not a hard GraphQL error.
**Bad**: a new code appearing right after a schema publish — usually a nullability
or value-completion regression.

---

## 4. Cache

### Query-planner / APQ / introspection caches

```dql
timeseries {
    hits = sum(apollo.router.cache.hit.time.count),
    misses = sum(apollo.router.cache.miss.time.count)
  }, by: {kind},
  filter: {service.name == "apollo-router"}
```

**Good**: query-planner cache hit rate near 1.0 in steady state.
**Bad**: hit rate collapsing after a deploy is expected briefly (cold cache); if
it does not recover, warm-up is misconfigured or the cache is undersized.

### Response / entity cache, by subgraph

```dql
timeseries cache = sum(apollo.router.response.cache),
  by: {subgraph.name, cache.hit},
  filter: {service.name == "apollo-router"}
```

**Good**: `cache.hit == true` dominating for the subgraphs you added cache
control to.
**Bad**: all misses on a subgraph you expected to cache — usually a missing or
zero `max-age` in that subgraph's `Cache-Control`.

---

## 5. Query planning

```dql
timeseries {
    p50 = percentile(apollo.router.query_planning.plan.duration, 50, rollup: avg),
    p95 = percentile(apollo.router.query_planning.plan.duration, 95, rollup: avg),
    p99 = percentile(apollo.router.query_planning.plan.duration, 99, rollup: avg)
  },
  filter: {service.name == "apollo-router"}
```

**Good**: low and flat, because most operations hit the plan cache. Note the
`.seconds` suffix — that is how this histogram lands in Grail.
**Bad**: p99 climbing, or the plan *rate* rising (below) — a rising rate means
cache misses, which usually means unbounded operation shapes or a cache that is
too small.

```dql
timeseries plans = count(apollo.router.query_planning.plan.duration),
  filter: {service.name == "apollo-router"}
```

---

## 6. Payload sizes

```dql
timeseries {
    req_p95 = percentile(http.server.request.body.size, 95, rollup: avg),
    resp_p95 = percentile(http.server.response.body.size, 95, rollup: avg)
  },
  filter: {service.name == "apollo-router"}
```

**Good**: stable and well inside any WAF/CDN body limits on the path (relevant
when traffic passes through Akamai or similar).
**Bad**: response p95 growing without a client change — usually an unbounded list
field that needs pagination or a demand-control limit.

---

## 7. Saturation — is the router itself the constraint?

The compute-job pool runs query parsing and planning. Work sitting in its queue is
latency no subgraph is responsible for, which makes this the section to check when
subgraph timings look fine and clients still complain.

Durations here are metrics with no span equivalent, so they report avg/max rather
than a percentile — `percentile()` on a metric needs a `rollup` that collapses the
slot to one value first, which returns the average. See
[percentiles-and-buckets.md](../docs/percentiles-and-buckets.md).

```dql
timeseries {
    avg_open = avg(apollo.router.open_connections),
    max_open = max(apollo.router.open_connections)
  },
  filter: {service.name == "apollo-router"}
```

**Good**: flat, well under whatever your ingress or file-descriptor limit is.
**Bad**: a rising floor — connections are being held open, not turned over.

```dql
timeseries {
    queued = max(apollo.router.compute_jobs.queued),
    active = max(apollo.router.compute_jobs.active_jobs)
  },
  filter: {service.name == "apollo-router"}
```

**Good**: `queued` near zero. Jobs start as soon as they arrive.
**Bad**: `queued` climbing while `active` is flat — the pool is saturated and every
queued job is added latency.

```dql
timeseries {
    queue_wait = avg(apollo.router.compute_jobs.queue.wait.duration),
    execution = avg(apollo.router.compute_jobs.execution.duration)
  },
  filter: {service.name == "apollo-router"}
```

**Good**: wait time far below execution time. On an idle router, wait is tens of
microseconds.
**Bad**: wait approaching or exceeding execution — the router is spending more time
waiting for a worker than doing the work.

```dql
timeseries duration = avg(apollo.router.compute_jobs.duration),
  by: {job.type},
  filter: {service.name == "apollo-router"}
```

**Good**: `query_planning` and `query_parsing` both steady. `job.type` is the
dimension that tells you which of the two is growing.
**Bad**: `query_planning` rising — usually large or newly-deployed operations
missing from the plan cache.

---

## 8. Coprocessors (only if `coprocessor.url` is configured)

A coprocessor adds a network hop to every request it handles, so its duration is
router latency that no subgraph can explain. These instruments do not exist until a
coprocessor is configured.

```dql
timeseries calls = sum(apollo.router.operations.coprocessor),
  by: {coprocessor.stage, coprocessor.succeeded},
  filter: {service.name == "apollo-router"}
```

```dql
timeseries {
    avg_duration = avg(apollo.router.operations.coprocessor.duration),
    max_duration = max(apollo.router.operations.coprocessor.duration)
  },
  by: {coprocessor.stage},
  filter: {service.name == "apollo-router"}
```

**Good**: a single stage, low and flat, with `coprocessor.succeeded: true`.
**Bad**: failures at any stage, or a duration comparable to total request latency —
the coprocessor is now the bottleneck. The `coprocessor.stage` and
`coprocessor.succeeded` attributes are the documented ones but are unverified here,
since this pack runs no coprocessor.

---

## 9. Connectors (only if Apollo Connectors are in use)

```dql
timeseries p95 = percentile(http.client.request.duration, 95),
  by: {connector.source.name}, rollup: avg,
  filter: {service.name == "apollo-router"}
```

**Good**: each REST source inside its own SLO.
**Bad**: a source with rising latency — the router is waiting on someone else's
API, and it will show up as router latency to the client.

---

## 10. Log events (Dynatrace Log Management)

Log records arrive via the separate `logging.otlp` exporter (or, in the collector
topology, a `filelog` receiver tailing the router's stdout). If this returns
nothing while spans exist, the logging pipeline is missing — the single most
common Dynatrace gap (rule `DT017`).

```dql
fetch logs
| filter service.name == "apollo-router"
| filter matchesPhrase(content, "jwt")
| sort timestamp desc
| limit 100
```

---

## 11. Trace correlation

```dql
fetch spans
| filter service.name == "apollo-router"
| filter trace.id == "$traceId"
| sort start_time asc
```

Dynatrace searches on `trace.id`, not the full `traceparent`. Hand the id to
clients via `experimental_response_trace_id` so a caller can quote it — and
remember that Rhai's `traceid()` returns the **span** id (16 hex chars), not the
root trace id.

---

## 12. Span- and log-based tiles (no metric behind them)

Everything above is a `timeseries` query against a metric. The tiles in this
section read `fetch spans` / `fetch logs` instead — the DQL equivalent of the
the parts of an APM overview that aren't backed by a metric at all: top
operations by name, a dependency list, failed-request exemplars, and error
logs. They cost more per query than the metric tiles (Grail scans raw
spans/logs), so they're filtered and `limit`-ed deliberately.

### SLA compliance % (Apdex substitute)

```dql
fetch spans
| filter service.name == "apollo-router" and request.is_root_span == true
| fieldsAdd meets_sla = if(request.is_failed == false and duration < 3s, 1, else: 0)
| summarize total = count(), compliant = sum(meets_sla)
| fieldsAdd sla_compliance_pct = (compliant * 100.0) / total
```

Dynatrace has no native Apdex. This is the honest substitute: define "satisfied"
as not-failed and under a duration threshold (3s here — adjust per SLO), then
report the percentage. **Good**: near 100%. **Bad**: a drop with no matching
p99 spike in §1 — check whether `request.is_failed` is catching something the
latency tiles aren't (e.g. a status-code-only failure with normal latency).

### Top GraphQL operations, by p99

```dql
fetch spans
| filter service.name == "apollo-router" and request.is_root_span == true
| summarize {
    requests = count(),
    failed = countIf(request.is_failed == true),
    p95 = percentile(duration, 95),
    p99 = percentile(duration, 99)
  }, by: {graphql.operation.name}
| fieldsAdd error_rate_pct = (failed * 100.0) / requests
| fields graphql.operation.name, requests, error_rate_pct, p95, p99
| sort p99 desc
| limit 20
```

The operation-ranking table most APM overviews lead with.
Deliberately span-based, not metric-based: `graphql.operation.name` stays off
on the metrics side (HIGH CARD, see `instruments.router.yaml`) but is already
captured on spans, so this reads it there instead of adding a new billable
metric dimension. **Good**: the same handful of operations at the top,
consistently. **Bad**: an operation climbing this list that wasn't here last
week — usually a client release or a missing cache key.

### Recent failed requests (exemplars)

```dql
fetch spans
| filter service.name == "apollo-router" and request.is_root_span == true and request.is_failed == true
| fields start_time, trace.id, graphql.operation.name, http.response.status_code, duration
| sort start_time desc
| limit 50
```

The dashboard-native version of "click a spike, land on a trace":
each row carries a `trace.id` a reader can paste into trace search (§11) or the
Dynatrace UI to open the PurePath directly, rather than only seeing an
aggregate failure count.

### Dependencies — subgraphs & connectors, by call volume

```dql
fetch spans
| filter service.name == "apollo-router" and span.kind == "client"
| fieldsAdd dependency = coalesce(subgraph.name, connector.source.name, server.address)
| filter isNotNull(dependency)
| summarize {
    calls = count(),
    avg_duration = avg(duration),
    p99_duration = percentile(duration, 99)
  }, by: {dependency}
| sort calls desc
| limit 20
```

A single ranked list of everything the router calls out to — subgraphs,
connectors, and any other outbound client span — closest dashboard-native
substitute for a full topology view (Smartscape is the real one in Dynatrace,
but it isn't embeddable in a dashboard tile). **Good**: call volume matches
expected fan-out per request. **Bad**: a new dependency appears, or an
existing one's `p99_duration` climbs — that's latency the client will feel as
router latency, even when the router itself is healthy.

### Router error logs

```dql
fetch logs
| filter service.name == "apollo-router" and loglevel == "ERROR"
| fields timestamp, content
| sort timestamp desc
| limit 50
```

Same query family as §10, now embedded directly in the dashboard instead of
living only in this doc. Empty when spans/metrics show errors means the
logging pipeline is missing — see `DT017` and the "Router logs do not reach
Dynatrace directly" note in the README.
