# Router metrics — what's available and why you'd use it

A menu, not a prescription. Each row names a metric, the question it answers, and
what it costs to get. When a row looks useful, follow its recipe link for the
Dynatrace steps: enable it, confirm it arrived, chart it, read it.

Sections follow Apollo's own APM template grouping so this stays one list rather
than a competing one. Sections 8–11 cover ground that grouping does not.

## How to read the columns

**Get it by** — what it costs you. Every metric in this list is configured in one
file, [`instruments.router.yaml`](../templates/instruments.router.yaml). Spans and
histogram buckets are configured separately, in
[`spans.router.yaml`](../templates/spans.router.yaml) and
[`histogram-buckets.router.yaml`](../templates/histogram-buckets.router.yaml).

| Tag | Meaning |
|---|---|
| `default` | emitted with no instrument configuration at all |
| `declare` | add it to `instruments.router.yaml`. Some are standard instruments you toggle on, some are counters this pack defines — either way, one file, one line |
| `requires X` | does not exist until a router feature is enabled |
| `span` | not a metric — read it from spans |

**Verified** means observed arriving in a real Dynatrace tenant from a real router
(39 of the `apollo.router.*` family). `⚠ unverified` means it has not been seen
here — confirm it arrives (step 2 of its recipe) before building on it.

**Type** decides the aggregation. This is the one thing you cannot copy blindly
when reusing a query from another row:

| Type | Aggregate with | Reusing the wrong shape gives you |
|---|---|---|
| counter | `sum()` | — |
| histogram | `avg()` / `max()` | `sum()` of a histogram is meaningless |
| gauge | `avg()` / `max()` | `sum()` across instances double-counts |
| histogram, percentile wanted | **spans** | the average wearing a percentile's label |

## Four common pitfalls

**GraphQL errors are HTTP 200.** A failed operation returns 200 with an `errors`
array, so any error rate built on status codes reports zero while the graph is
broken. Error monitoring therefore needs a GraphQL-aware counter rather than a
status-code one: use `apollo.router.graphql_error`.

**Latency percentiles come from spans, not the duration histogram.**
`percentile()` on a metric requires a `rollup` that collapses the interval first,
so it returns the average — p50, p90 and p99 come back identical. See
[percentiles-and-buckets.md](percentiles-and-buckets.md).

**UpDownCounters are unusable on Dynatrace.** `compute_jobs.active_jobs`,
`opened.subscriptions` and `cache.redis.connections` report negative or corrupted
values under delta temporality — one dropped delta corrupts the total until
restart. Dynatrace requires delta, so these are excluded throughout.

**Some metrics Dynatrace refuses outright** — see [section 12](#12-metrics-dynatrace-rejects).

---

## 1. Incoming HTTP — is the graph healthy right now?

| Metric | Answers | Type | Get it by | Where |
|---|---|---|---|---|
| `dynatrace.router.requests` | How much traffic, by status class? | counter | `declare` | metric · [how to read it](../dashboards/dql-queries.md#traffic-and-health) |
| `dynatrace.router.server.errors` | Did a request fail to produce a GraphQL response at all? | counter | `declare` | metric · [how to read it](../dashboards/dql-queries.md#traffic-and-health) |
| `http.server.active_requests` | How many requests are in flight? A climbing floor means they are piling up. | gauge | `declare` | metric · [how to read it](../dashboards/dql-queries.md#traffic-and-health) |
| root span `duration` | What latency do clients actually experience? | span | `span` | **span** · [how to read it](../dashboards/dql-queries.md#traffic-and-health) |
| `http.server.request.body.size` · `http.server.response.body.size` | Are payloads growing? Large responses usually mean an over-fetching client. | histogram | `declare` | metric · [how to read it](../dashboards/dql-queries.md#traffic-and-health) |
| `apollo.router.session.count.active` | Active sessions — **being removed in 3.0**, use `http.server.active_requests` | gauge | `default` | metric · avoid |

---

## 2. GraphQL layer

Apollo's grouping folds these into golden signals. They are separated here because
the first row is the single most cross-validated insight in this document.

| Metric | Answers | Type | Get it by | Where |
|---|---|---|---|---|
| `apollo.router.graphql_error` | Are operations failing? **The one status codes miss.** | counter | `default` | metric · [how to read it](../dashboards/dql-queries.md#traffic-and-health) |
| `dynatrace.graphql.operations` | How many GraphQL operations, by type? Diverges from request count under batching. | counter | `declare` | metric · [how to read it](../dashboards/dql-queries.md#traffic-and-health) |
| `apollo.router.operations` | Total operations processed | counter | `default` | metric |
| `apollo.router.operations.lexical_tokens` | Token count per operation — a proxy for query size | histogram | `default` | metric |
| `apollo.router.operations.recursion` | Query nesting depth. Sudden growth is often an abusive or generated query. | histogram | `default` | metric |

Three numbers here count three different things — HTTP requests, GraphQL
operations, and Studio's reported operations. They will not match, and that is not
a bug: batching, subscriptions and sampling drive them apart.

---

## 3. Subgraph (outgoing) HTTP — is it the router or a subgraph?

| Metric | Answers | Type | Get it by | Where |
|---|---|---|---|---|
| `apollo.router.overhead` | Is the router the bottleneck, or waiting on someone else? **Three pitfalls — read the recipe.** | histogram | `declare` | metric · [how to read it](../dashboards/dql-queries.md#router-versus-subgraph) |
| `http.client.request.duration` by `subgraph.name` | Which subgraph is slow? | histogram | `declare` | metric · [how to read it](../dashboards/dql-queries.md#router-versus-subgraph) |
| `dynatrace.subgraph.errors` by `subgraph.name` | Which subgraph is returning errors? | counter | `declare` | metric · [how to read it](../dashboards/dql-queries.md#router-versus-subgraph) |
| `subgraph` span `duration` | Subgraph latency percentiles you can trust | span | `span` | **span** · [how to read it](../dashboards/dql-queries.md#router-versus-subgraph) |
| `http.client.request.body.size` · `http.client.response.body.size` | Is a subgraph returning far more than the client asked for? | histogram | `declare` | metric · [how to read it](../dashboards/dql-queries.md#traffic-and-health) |
| `apollo.router.connection.acquire.duration` | Time to get a connection to a subgraph. Rising = pool exhaustion, not subgraph slowness. | histogram | `default` | metric |

**`subgraph.name` matters more than any metric here.** Without it these are
graph-wide averages that cannot be acted on. With it, a chart names the team that owns
the problem. Most router incidents originate in a subgraph, so in a federated graph
the question of which team owns a failure is answered by the schema.

**Compare overhead only within one router version.** Work moves in and out of the
router's critical path between releases.

---

## 4. Query planning and compute jobs

The router's own work queue, and **a saturation domain that CPU-based autoscaling
does not detect.** Published load testing shows query-planning p99 degrading from
milliseconds to seconds across a narrow increase in throughput while CPU
utilisation stays flat.

| Metric | Answers | Type | Get it by | Where |
|---|---|---|---|---|
| `apollo.router.compute_jobs.queued` | Is work waiting for a worker? The leading indicator. | gauge | `default` | metric · [how to read it](../dashboards/dql-queries.md#saturation-planning-and-cache) |
| `apollo.router.compute_jobs.queue.wait.duration` | How long does work wait before starting? | histogram | `default` | metric · [how to read it](../dashboards/dql-queries.md#saturation-planning-and-cache) |
| `apollo.router.compute_jobs.execution.duration` | How long does the work itself take? Compare against wait. | histogram | `default` | metric · [how to read it](../dashboards/dql-queries.md#saturation-planning-and-cache) |
| `apollo.router.compute_jobs.duration` by `job.type` | Which job type is growing — `query_parsing` or `query_planning`? | histogram | `default` | metric · [how to read it](../dashboards/dql-queries.md#saturation-planning-and-cache) |
| `apollo.router.query_planning.plan.duration` | How long to build a plan? | histogram | `default` | metric · [how to read it](../dashboards/dql-queries.md#saturation-planning-and-cache) |
| `apollo.router.query_planning.total.duration` | Plan time **including queue wait** — the number a client feels | histogram | `default` | metric · [how to read it](../dashboards/dql-queries.md#saturation-planning-and-cache) |
| `apollo.router.query_planning.warmup.duration` | Is cache warm-up working after a deploy? | histogram | `default` | metric · [how to read it](../dashboards/dql-queries.md#saturation-planning-and-cache) |
| `apollo.router.query_planning.plan.evaluated_plans` | Plan-space explosion. High counts mean an expensive schema shape. | histogram | `default` | metric · [how to read it](../dashboards/dql-queries.md#saturation-planning-and-cache) |
| `apollo.router.query_planning.plan.evaluated_paths` | Same, at path granularity | histogram | `default` | metric |
| `apollo.router.query_planner.memory` | Planner memory. **Unix + global-allocator feature only.** | gauge | `default` | metric |
| `apollo.router.compute_jobs.active_jobs` | — | UpDownCounter | **excluded** | unusable under delta |

---

## 5. Cache

| Metric | Answers | Type | Get it by | Where |
|---|---|---|---|---|
| `apollo.router.cache.hit.time` / `.miss.time` by `kind` | Hit rate for query-plan, APQ and introspection caches | histogram | `default` | metric · [how to read it](../dashboards/dql-queries.md#saturation-planning-and-cache) |
| `apollo.router.cache.size` by `kind` | How full is each cache? | gauge | `default` | metric · [how to read it](../dashboards/dql-queries.md#saturation-planning-and-cache) |
| `apollo.router.cache.storage.estimated_size` | Bytes held. Pair with hit rate before resizing. | gauge | `default` | metric |
| `apollo.router.response.cache` by `subgraph.name`, `cache.hit` | Is entity caching earning its keep? | counter | `requires entity caching` | metric · ⚠ unverified |
| `apollo.router.cache.redis.connections` | — | UpDownCounter | **excluded** | unusable under delta |

`kind` is the dimension that makes this section useful — without it you cannot tell
a cold query-plan cache from an APQ miss. A cold cache after a deploy is normal and
self-heals; **not** recovering means warm-up is misconfigured.

---

## 6. Coprocessor

| Metric | Answers | Type | Get it by | Where |
|---|---|---|---|---|
| `apollo.router.operations.coprocessor` by `coprocessor.stage` | How often is the coprocessor called, and does it succeed? | counter | `requires coprocessor.url` | metric · ⚠ unverified |
| `apollo.router.operations.coprocessor.duration` by `coprocessor.stage` | How much latency does it add per stage? | histogram | `requires coprocessor.url` | metric · ⚠ unverified |

A coprocessor adds a network hop to every request it handles, and **its time is
counted inside `apollo.router.overhead`** — so a slow coprocessor looks like router
regression. Published measurements put Rhai at approximately 100 µs p95 and coprocessors at
approximately 350 µs per stage; treat those as a budget. Coprocessors are invoked **per
subgraph** at subgraph stages, so co-locate them.

**Known limitation:** the GraphQL operation name is not propagated into the
Router→coprocessor span, so Dynatrace shows only `/`. Coprocessor cost cannot be
attributed to an operation, and these metric tiles are the only coprocessor view
available until that is addressed upstream.

---

## 7. Diagnostic sentinels — the silent failures

Nothing in this section shows up in RED metrics. Every one of them fails quietly
while dashboards stay green.

| Metric | Answers | Type | Get it by | Where |
|---|---|---|---|---|
| `apollo.router.uplink.fetch.count.total` | Is schema/PQ delivery working? **A failed fetch means the router runs on a stale schema indefinitely.** | counter | `default` | metric |
| `apollo.router.uplink.fetch.duration.seconds` | How long does uplink take? Rising = delivery degrading before it fails. | histogram | `default` | metric |
| `apollo.router.lifecycle.license` | Licence state. Expiry is a scheduled outage that is rarely dashboarded. | gauge | `default` | metric |
| `apollo.router.lifecycle.query_planner.init` | Planner init time at startup | histogram | `default` | metric |
| `apollo.router.state.change.total` | Router state transitions — restarts and reloads | counter | `default` | metric |
| `apollo.router.supergraph.federation` | Federation version in use. Useful as a deploy marker. | gauge | `default` | metric |
| `apollo.router.open_connections` | Connections held open | gauge | `default` | metric · [how to read it](../dashboards/dql-queries.md#saturation-planning-and-cache) |
| `apollo.router.pipelines` | Active pipelines. Expect **1 per instance** — 10 replicas means 10. | gauge | `default` | metric |
| `apollo.router.telemetry.studio.reports` | Is usage reporting to Studio working? | counter | `default` | metric |

**Schema delivery is a runtime dependency, not a formality.** A published account
describes a large gateway fleet polling every 10 seconds, where the observed fetch
failure rate was roughly fifty times higher than expected while the platform status
dashboard reported normal uptime. Stale instances could not be identified, because
the runtime does not expose the schema version it has loaded. Watch uplink fetch
success and duration.

**A caution on `pipelines`**: a stable value of 1 does **not** rule out a memory
leak — resident memory can grow substantially while this metric stays pinned at 1.
The name also differs by exporter: `apollo_router_pipelines` on Prometheus.

---

## 8. Memory (internals)

You will not look at these until you are chasing a leak or sizing a pod. Included
for completeness; skip on a first pass.

| Metric | Answers | Type | Get it by |
|---|---|---|---|
| `apollo.router.jemalloc.resident` | Actual RSS-equivalent. **Start here** — the one to alert on if you alert on memory. | gauge | `default` |
| `apollo.router.jemalloc.allocated` | Bytes the application asked for | gauge | `default` |
| `apollo.router.jemalloc.active` | Bytes in active pages | gauge | `default` |
| `apollo.router.jemalloc.mapped` | Bytes mapped from the OS | gauge | `default` |
| `apollo.router.jemalloc.metadata` · `.retained` | Allocator bookkeeping and unreturned pages | gauge | `default` |
| `apollo.router.request.memory` | Per-request allocation. **Unix + global-allocator feature only.** | gauge | `default` |

`resident` growing while `allocated` stays flat is allocator fragmentation, not a
leak in your graph. Both growing together is the real thing.

---

## 9. Feature-gated

None of these exist until the feature is on, and **none are verified here** — this
pack runs no PQ list, auth, connectors or subscriptions. Confirm arrival before
building on any of them.

| Area | What to watch | Silent failure to guard against |
|---|---|---|
| **Persisted queries** | PQ rejection rate; uplink manifest fetch success | Uplink stops delivering the manifest → the router serves a stale PQ list → a new client release is rejected while every dashboard stays green. With safelisting enforced, a miss is a blocked request |
| **Auth** | Auth failure rate; JWKS fetch health | JWKS unreachable → no token validates → everything 401s, and it looks like a client problem |
| **Authorization directives** | Filtered-field counts | `@authenticated`/`@requiresScopes` remove fields **silently** — users get `null`, not an error, so error rates stay flat while the product is broken |
| **Connectors** | `http.client.request.duration` by `connector.source.name` | A REST source degrading surfaces as router latency |
| **Subscriptions** | `opened.subscriptions`, `skipped.event.count` | `opened.subscriptions` is an UpDownCounter — unusable under delta |

Note `apollo.router.operations.jwt` is deprecated in favour of
`apollo.router.operations.authentication.jwt`, and the old name is removed at 3.0.

---

## 10. Container and host

**Not router-emitted.** Dynatrace collects these itself via OneAgent or the
Kubernetes operator, so there is nothing to configure in the router — but the data
has to come from somewhere, and a router dashboard without CPU is missing the first
thing you check.

What matters: **CPU under ~50%**. Above that, `apollo.router.overhead` stops
meaning anything, because you are measuring scheduling delay rather than router
work. Apollo's own deployment autoscales at 75% CPU; their sizing guidance suggests
no CPU limit at all, to avoid throttling.

Memory: prefer `jemalloc.resident` from section 8 over container RSS, since it
attributes to the router process rather than the pod.

---

## 11. Subgraph health — the gap

**The most-requested signal that does not exist.** `/health` tells you the router
pod is alive. It says nothing about whether subgraphs are reachable, so a router
can report healthy while the graph is unusable.

This is a recurring request from teams operating federated graphs at scale. It is
logged with Apollo, with no current implementation timeline; circuit breaking in
Router v3 is the longer-term direction.

The gap this leaves is not theoretical. Edge health checks that resolve addresses
rather than names will report healthy pods while DNS routing fails intermittently,
which can sustain an elevated error rate for hours without any signal from the
router itself.

What you can do today:

| Approach | How | Trade-off |
|---|---|---|
| Derive it from telemetry | `dynatrace.subgraph.errors` and `http.client.request.duration` by `subgraph.name`, evaluated on a short interval | Reactive — it tells you a subgraph is failing, not that it is unreachable before traffic hits it |
| Rhai script | Probe subgraphs and emit a custom signal | Runs in the request path; adds ~100 µs p95 |
| Coprocessor | Same, out of process | Adds a network hop; its time lands inside `apollo.router.overhead` |
| Synthetic monitor | Dynatrace HTTP monitor per subgraph, outside the router | Honest about reachability, but not what the router experiences |

Deriving it from `subgraph.name`-attributed telemetry is the cheapest useful
version, and it is why that attribute is worth enabling even though it costs
cardinality.

---

## 12. Metrics Dynatrace rejects

Dynatrace does not accept every OTLP metric type the router emits. Rejected metrics
fail **at ingest**, not in the router, so the router logs look clean and the metric
simply never appears. This affects a substantial number of router metrics in a
single environment, not one or two.

Two confirmed by name:

| Metric | Rejection reason | How this was confirmed |
|---|---|---|
| `apollo.router.schema.load.duration` | `UNSUPPORTED_METRIC_TYPE_HISTOGRAM` | absent from a tenant where 39 other `apollo.router.*` metrics arrived, despite firing at every startup |
| `apollo.router.operations.validation` | `UNSUPPORTED_METRIC_TYPE_MONOTONIC_CUMULATIVE_SUM` | same |

The complete set is not published, so treat this as a class of problem rather than
a closed list: **if a metric you expect never appears and the router shows no export
errors, suspect ingest rejection** before you suspect your config. Check the
Dynatrace ingest response rather than the router log.

This is also why step 2 of every recipe — confirm it arrived — exists.

---

## Attributes are what cost you, not metrics

Dynatrace bills per data point, and series count is the product of attribute
cardinalities. The metric is free; the attribute is the bill.

| Attribute | Cost | Recommendation |
|---|---|---|
| `http.response.status_code` | ~5 values | on. Cheap and you need it. |
| `subgraph.name` | one per subgraph | on. This is the attribution that makes the data actionable. |
| `graphql.operation.type` | 3 values | on. |
| `kind` (cache), `job.type` (compute jobs) | a handful | on. Both sections are useless without them. |
| `graphql.operation.name` | one per distinct operation | **leave off.** See below. |
| `graphql.document` | unbounded, and may contain PII | never. Rule `DT016` flags it. |

**There is a hard cardinality ceiling, and it is not configurable.** The OTel Rust
SDK enforces **2,000 datapoints per metric stream** (SDK 0.24.0, shipped in Router
v2.10.0). On overflow the router does not error — it **strips the attributes**,
keeps the values, sets `otel.metric.overflow=true`, and increments
`apollo.router.telemetry.metrics.cardinality_overflow` (⚠ unverified here — it is
only created once an overflow actually happens). The limit applies per export
batch. Attributes on **histograms** are the worst case, because you pay per bucket.

The router's own defaults do not emit high-cardinality attributes, so overflow is
almost always caused by attributes someone added. That is the argument for leaving
`graphql.operation.name` off: persisted queries do bound the operation set, but a PQ
manifest can comfortably exceed 2,000 entries, so PQs do not make the attribute
safe — they only make the ceiling easier to estimate.

**And the remedy is currently broken.** The documented lever for pruning metrics —
`views` / metric dropping — **does not accept wildcards** despite the docs showing
them. `*`, `.*`, `apollo_*` and `apollo.*` all silently no-op; only exact metric
names work, and some metrics cannot be dropped in either spelling.
`cardinality_overflow` is also a single global counter, so it reports that
*something* overflowed without identifying which stream.

Practical consequence: budget by **not enabling** attributes rather than by pruning
later. If you must prune, enumerate exact metric names one at a time and verify each
drop actually took effect.

---

## What Studio holds that Dynatrace cannot

Not a metrics question, but it decides what you build where.

| Capability | Why an APM cannot replace it |
|---|---|
| Field-level usage | which fields each client uses — needed for safe deprecation |
| Schema checks against real traffic | CI gating on breaking changes |
| Operation signature normalization | Studio groups functionally identical operations; Dynatrace groups by span name |
| PQ manifest | the list lives in GraphOS |

And the converse: **Studio cannot alert.** An external tool is mandatory for
production alerting. Studio Insights also does not consume your OTLP metrics — it
is a separate channel, and usage reporting to it is sampled. This is a common source of
confusion when the two are compared directly.
