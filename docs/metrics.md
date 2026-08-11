# Router metrics — what's available and why you'd use it

A menu, not a prescription. Each row names a metric, the question it answers, and
what it costs to get. When a row looks useful, follow its recipe link for the
Dynatrace steps: enable it, confirm it arrived, chart it, read it.

> **Status: sample.** Groups 1 and 2 below are complete. Groups 3–8 (query
> planning, caching, saturation, memory, schema/uplink/licence, and feature-gated
> instruments for PQ, auth, connectors and coprocessors) are outlined at the
> bottom and not yet written.

## How to read the columns

**Get it by** — what it costs you to have this metric. Everything you configure
lives in one file, [`instruments.router.yaml`](../templates/instruments.router.yaml).

| Tag | Meaning |
|---|---|
| `default` | emitted with no instrument configuration at all |
| `declare` | add it to `instruments.router.yaml`. Some are standard instruments you toggle on, some are counters this pack defines — either way, one file, one line |
| `requires X` | does not exist until a router feature is enabled |
| `span` | not a metric — read it from spans |

Every metric marked `default` was observed emitting from a router whose config
never declares it. Anything marked `⚠ unverified` has not been seen emitting
here, so confirm it arrives (step 2 of its recipe) before building on it.

**Type** — this decides the aggregation, and it is the one thing you cannot copy
blindly when reusing a query from another row:

| Type | Aggregate with | Reusing the wrong shape gives you |
|---|---|---|
| counter | `sum()` | — |
| histogram | `avg()` / `max()` | `sum()` of a histogram is meaningless |
| gauge | `avg()` / `max()` | `sum()` across instances double-counts |
| histogram, percentile wanted | **spans** | the average wearing a percentile's label |

**UpDownCounters are unusable on Dynatrace.** `apollo.router.compute_jobs.active_jobs`,
`apollo.router.opened.subscriptions` and `apollo.router.cache.redis.connections`
report negative or corrupted values under delta temporality — one dropped delta
corrupts the total until the router restarts. Dynatrace requires delta, so these
are excluded from this list and from the dashboard.

## Two things that catch everyone

**GraphQL errors are HTTP 200.** A failed GraphQL operation returns 200 with an
`errors` array. Any error rate built on status codes reports zero while the graph
is broken. Use `apollo.router.graphql_error`.

**Latency percentiles come from spans, not from the duration histogram.**
`percentile()` on an OTel histogram in DQL requires a `rollup` that collapses the
interval before the percentile is taken, so it returns the average — p50, p90 and
p99 come back identical. See
[percentiles-and-buckets.md](percentiles-and-buckets.md).

---

## 1. Is the graph healthy right now?

The set you would put on a wall. Answers "is traffic normal, and is any of it
failing," with no interpretation required.

| Metric | Answers | Type | Get it by | Where |
|---|---|---|---|---|
| `dynatrace.router.requests` | How much traffic, split by status class? | counter | `declare` | metric · [recipe §1](../dashboards/dql-queries.md#1-golden-signals) |
| `apollo.router.graphql_error` | Are operations failing? **The one status codes miss.** | counter | `default` | metric · [recipe §3](../dashboards/dql-queries.md#3-graphql-level-view) |
| `dynatrace.router.server.errors` | Did any request fail to produce a GraphQL response at all? | counter | `declare` | metric · [recipe §1](../dashboards/dql-queries.md#1-golden-signals) |
| `dynatrace.graphql.operations` | How many GraphQL operations, by type? Diverges from request count under batching. | counter | `declare` | metric · [recipe §3](../dashboards/dql-queries.md#3-graphql-level-view) |
| `http.server.active_requests` | How many requests are in flight? A climbing floor means they are piling up. | gauge | `declare` | metric · [recipe §1](../dashboards/dql-queries.md#1-golden-signals) |
| root span `duration` | What latency do clients actually experience? | span | `span` | **span** · [recipe §1](../dashboards/dql-queries.md#1-golden-signals) |
| `http.server.request.body.size` · `http.server.response.body.size` | Are payloads growing? Large responses are usually an over-fetching client. | histogram | `declare` | metric · [recipe §6](../dashboards/dql-queries.md#6-payload-sizes) |

**Why this group first:** three different numbers here count three different
things — HTTP requests, GraphQL operations, and reported operations in Studio.
They will not match, and that is not a bug. Batching, subscriptions and sampling
drive them apart.

---

## 2. Is it the router or a subgraph?

The question every incident starts with. `apollo.router.overhead` is the one
metric that answers it directly, because it excludes time spent waiting on
anything downstream.

| Metric | Answers | Type | Get it by | Where |
|---|---|---|---|---|
| `apollo.router.overhead` | Is the router the bottleneck, or is it waiting on someone else? | histogram | `declare` | metric · [recipe §2](../dashboards/dql-queries.md#2-is-it-the-router-or-the-subgraphs) |
| `http.client.request.duration` by `subgraph.name` | Which subgraph is slow? | histogram | `declare` | metric · [recipe §2](../dashboards/dql-queries.md#2-is-it-the-router-or-the-subgraphs) |
| `dynatrace.subgraph.errors` by `subgraph.name` | Which subgraph is returning errors? | counter | `declare` | metric · [recipe §2](../dashboards/dql-queries.md#2-is-it-the-router-or-the-subgraphs) |
| `subgraph` span `duration` | Subgraph latency percentiles you can trust | span | `span` | **span** · [recipe §2](../dashboards/dql-queries.md#2-is-it-the-router-or-the-subgraphs) |
| `http.client.request.body.size` · `http.client.response.body.size` | Is a subgraph returning far more data than the client asked for? | histogram | `declare` | metric · [recipe §6](../dashboards/dql-queries.md#6-payload-sizes) |

**Why `subgraph.name` matters more than the metric.** Without it these are graph
-wide averages nobody can act on. With it, an alert or a chart names the team that
owns the problem. Most router incidents are subgraph incidents, and this attribute
is the difference between routing a page correctly and making the platform team a
triage desk.

**Compare only within one router version.** Overhead shifts between releases as
work moves in and out of the router's own critical path.

---

## Attributes are what cost you, not metrics

Dynatrace bills per data point, and series count is the product of attribute
cardinalities. The metric is free; the attribute is the bill.

| Attribute | Cost | Recommendation |
|---|---|---|
| `http.response.status_code` | ~5 values | on. Cheap and you need it. |
| `subgraph.name` | one per subgraph | on. This is the attribution that makes the data actionable. |
| `graphql.operation.type` | 3 values | on. |
| `graphql.operation.name` | one per distinct operation | **leave off.** See below. |
| `graphql.document` | unbounded, and may contain PII | never. Rule `DT016` flags it. |

**There is a hard cardinality ceiling, and it is not configurable.** The OTel Rust
SDK enforces **2,000 datapoints per metric stream** (SDK 0.24.0, shipped in Router
v2.10.0). On overflow the router does not error — it **strips the attributes**,
keeps the values, sets `otel.metric.overflow=true`, and increments
`apollo.router.telemetry.metrics.cardinality_overflow`. The limit applies per
export batch. Attributes on **histograms** are the worst case, because you pay per
bucket.

The router's own defaults do not emit high-cardinality attributes, so overflow is
almost always caused by attributes someone added. That is the argument for leaving
`graphql.operation.name` off: persisted queries do bound the operation set, but a
PQ manifest can comfortably exceed 2,000 entries, so PQs do not make the attribute
safe — they only make the ceiling easier to estimate.

**And the remedy is currently broken.** The documented lever for pruning metrics —
`views` / metric dropping — **does not accept wildcards** despite the docs showing
them (TSH-24017). `*`, `.*`, `apollo_*` and `apollo.*` all silently no-op; only
exact metric names work, and at least one metric could not be dropped in either
spelling. `cardinality_overflow` is also a single global counter, so it will tell
you that *something* overflowed but not which stream (TSH-21610).

Practical consequence: budget by **not enabling** attributes rather than by pruning
later. If you must prune, enumerate exact metric names one at a time and verify
each drop actually took effect.

---

## Groups still to write

3. **Is query planning hurting us?** — plan duration, warmup, evaluated plans,
   planner memory.
4. **Is caching working?** — query plan / APQ / introspection caches by `kind`
   (verified), and entity caching (`requires entity caching`, unverified here).
5. **Is the router saturated?** — compute jobs, open connections, pipelines,
   sessions.
6. **Memory** — jemalloc set, request and planner memory. Marked as internals:
   what they are and the narrow cases where you would look.
7. **Schema, uplink and licence** — the silent ones. A failed uplink fetch means
   the router runs on a stale schema indefinitely with every dashboard green.
8. **Feature-gated** — PQ, auth, connectors, coprocessors, subscriptions. Each
   marked with what has to be enabled first and whether it is verified here.
