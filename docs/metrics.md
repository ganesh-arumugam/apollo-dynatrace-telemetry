# Router metrics — what's available and why you'd use it

A menu, not a prescription. Each row names a metric, the question it answers, and
what it costs to get. When a row looks useful, follow its recipe link for the
Dynatrace steps: enable it, confirm it arrived, chart it, read it.

Based on Apollo Router 2.17, the latest release. Instrument names and behavior
can change between major versions — check your own router's version if
something here doesn't match what you see.

Sections follow Apollo's own APM template grouping where it applies. Persisted
queries, response caching, auth, memory, connectors, subscriptions, subgraph
health and the Dynatrace-rejection list cover ground that grouping does not.

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

**Verified** means confirmed arriving in a Dynatrace tenant from a live router
(39 of the `apollo.router.*` family). Metrics tagged `requires X` do not exist
until that feature is configured — confirm each one arrives (step 2 of its
recipe) before building a chart or alert on it.

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

**"Requests served" and "requests completed" are not the same number, and
`dynatrace.router.requests` already carries the distinction.** Reproduced live
(router 2.17.0, harness with both a Prometheus scrape and OTLP to Dynatrace
enabled at once): a request whose client disconnects before the router can
send a response still increments `dynatrace.router.requests` — but under
`http.response.status_code == 499` ("client closed request", the same
convention nginx uses), not `200`. So:

```text
total requests served    = sum(dynatrace.router.requests)
total requests completed = sum(dynatrace.router.requests, filter: http.response.status_code != 499)
```

No separate metric is needed for "completed" — filter the one that already
exists. Two things worth knowing alongside this: `dynatrace.router.server.errors`
does **not** catch a 499 (its condition is `gt 499`, i.e. `>= 500`), so a run of
client-disconnects will not show up as router errors; and `http.server.active_requests`
still returns cleanly to baseline for these requests — the router does not leak
an in-flight slot just because the client walked away.

**`fetch spans | summarize count()` is a request COUNT capped by the tracing
sampler — never use it as "total requests."** Metrics are not sampled; spans
are. A `fetch spans` query — even one correctly scoped to
`request.is_root_span == true` or `span.kind == "server"` — only ever sees
the fraction of requests that got traced. Reproduced live at two sampler
settings, same 40-request batch, same router:

| `telemetry.exporters.tracing.common.sampler` | requests | root spans counted |
|---|---|---|
| 1.0 | 40 | 30 |
| 0.5 | 40 | 17 |

`dynatrace.router.requests` (the metric) reported `40` in both runs — metrics
export on every request regardless of the tracing sampler. Production
samplers are typically 0.01–0.1 (see
[`templates/prometheus-and-dynatrace.router.yaml`](../templates/prometheus-and-dynatrace.router.yaml)'s
`0.05` and this repo's own recommendation against `1.0` outside a demo), so a
span-count query in production can undercount the true request total by one
to two orders of magnitude — easily explaining a "huge difference" against
any metric-based count (Prometheus/Grafana or Dynatrace) that someone
assumes should agree with it. Use `sum(dynatrace.router.requests)` for a
request total; reserve `fetch spans` for percentiles and trace-level detail,
where sampling is an accepted, documented tradeoff, not a silent undercount.

The `30`-out-of-`40` figure at `sampler: 1.0` above is itself informative: it
is not sampling loss, it is one root span per request landing under a name
your filter may or may not be excluding on purpose — see the next pitfall.

**A root span's name depends on how the client sent the request, not just on
what kind of operation it was.** Three requests, three outcomes, confirmed
live: a named query gets `query <OperationName>`; a request that fails
GraphQL validation (bad field, etc.) gets the generic `GraphQL Operation`,
with no operation name to give it; and an *anonymous* query — no
`query <Name> { }`, just `{ }` — also gets a **generic, unnamed** span
(`query`, the operation type with no name), indistinguishable by name from
any other unnamed operation. A DQL filter written to exclude introspection
by name (`not(matchesValue(span.name, "query IntrospectionQuery"))`) only
works if the client actually sends introspection as a **named** operation
(`query IntrospectionQuery { __schema { ... } }`) — most tooling does, but an
anonymous introspection call (`{ __schema { ... } }`, no name prefix) slips
through unnamed and is not excluded by that filter. Confirm which shape your
actual introspection traffic uses before trusting the exclusion.

**Latency percentiles come from spans, not the duration histogram.**
`percentile()` on a metric requires a `rollup` that collapses the interval first,
so it returns the average — p50, p90 and p99 come back identical. See
[percentiles-and-buckets.md](percentiles-and-buckets.md).

**UpDownCounters are unusable on Dynatrace.** `compute_jobs.active_jobs` and
`opened.subscriptions` report negative or corrupted values under delta
temporality — one dropped delta corrupts the total until restart. Dynatrace
requires delta, so these are excluded throughout.

**Some metrics Dynatrace refuses outright** — see [section 17](#17-metrics-dynatrace-rejects).

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
| `apollo.router.operations.recursion` | Query parser recursion depth. Sudden growth is often an abusive or generated query. | histogram | `default` | metric |

Three numbers here count three different things — HTTP requests, GraphQL
operations, and Studio's reported operations. They will not match, and that is not
a bug: batching, subscriptions and sampling drive them apart.

**`apollo.router.graphql_error` carries a `code` attribute** — the same
mechanism GraphQL uses in its `errors[].extensions.code` field. Filtering on it
is how you separate a schema-validation failure from an authorization rejection
from a genuine resolver error, and it is what makes the authorization signal in
[section 8](#8-auth) work without a dedicated counter.

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
| `apollo.router.cache.hit.time` / `.miss.time` by `kind` | Hit rate for query-plan, automatic persisted query and introspection caches | histogram | `default` | metric · [how to read it](../dashboards/dql-queries.md#saturation-planning-and-cache) |
| `apollo.router.cache.size` by `kind` | How full is each cache? | gauge | `default` | metric · [how to read it](../dashboards/dql-queries.md#saturation-planning-and-cache) |
| `apollo.router.cache.storage.estimated_size` | Bytes held **for the in-memory query-planner cache specifically** — not response/entity caching, which has its own storage. | gauge | `default` | metric |
| `apollo.router.cache.redis.clients` | Is Redis reachable? A gauge, not the UpDownCounter this section used to warn about — that was the old name, `apollo.router.cache.redis.connections`, before it was renamed. | gauge | `requires Redis-backed cache` | metric |

`kind` is the dimension that makes this section useful — without it you cannot tell
a cold query-plan cache from an APQ miss. A live tenant, re-checked directly for
this note, consistently returns `query planner`, `APQ`, `introspection` —
uppercase `APQ`. Apollo's own documentation gives it lowercase, `apq`. DQL string
filters are case-sensitive, so a query written against the documented casing will
silently return nothing against the observed one. Check your own tenant's actual
value with a `by: {kind}` query before filtering on either spelling, or match
case-insensitively. **Automatic persisted queries and
query-plan caching are both covered by this section already** — neither needs a
metric of its own. (Persisted queries with an enforced manifest — safelisting —
are a different mechanism from APQ and are covered separately in
[section 7](#7-persisted-queries).)

A cold cache after a deploy is normal and self-heals; **not** recovering means
warm-up is misconfigured.

---

## 6. Response and entity caching

A distinct feature from the caches in section 5: this caches subgraph
**responses**, whole or per-field via `@cacheControl`, rather than query plans.
Requires GraphOS Router Enterprise and a Redis-backed cache store.

| Metric | Answers | Type | Get it by | Where |
|---|---|---|---|---|
| `apollo.router.response.cache` by `subgraph.name`, `graphql.type.name`, `cache.hit` | Is entity caching earning its keep, and for which subgraph and type? | counter | `requires entity caching` | metric |
| `apollo.router.cache.redis.errors` / `.reconnection` / `.unresponsive` | Is Redis itself the problem? | counter | `requires Redis-backed cache` | metric |
| `apollo.router.operations.response_cache.fetch.error` / `.insert.error` | Is the cache read or write path failing, specifically? | counter | `requires entity caching` | metric |

`subgraph.name` and `graphql.type.name` (the entity type — not `entity.type`,
which is not a valid attribute name on this instrument) are declared attributes.
Add them in [`instruments.router.yaml`](../templates/instruments.router.yaml),
the same file as every other metric here — `subgraph.name` needs the selector
form (`subgraph.name: { subgraph_name: true }`) on this particular instrument, a
plain `true` is rejected. `cache.hit` is attached by the router automatically.

**Two failure modes worth knowing:**

- **Redis availability has real counters**, not just an inference from subgraph
  latency. `cache.redis.errors`, `.reconnection` and `.unresponsive` are plain
  counters and safe under delta temporality — they're the direct signal for a
  Redis incident. Whether an outage then shows up as *latency* or as *errors* on
  the response-cache path still depends on whether the cache is configured
  fail-open or fail-closed, so read those two response_cache.*.error counters
  alongside the Redis ones to tell which.
- **Cache-key correctness.** A misconfigured cache key can serve one tenant's
  data to another. No metric detects this; it is a config-review concern, not a
  monitoring one.

---

## 7. Persisted queries

Two different features are both called "persisted queries," and they fail
differently:

- **Automatic persisted queries (APQ)** — the router caches operations by hash on
  first sight, no registration required. Already covered in section 5 under
  `kind: APQ`.
- **Persisted queries with an enforced manifest (safelisting)** — a pre-registered
  operation list. This is the feature described below.

**Verified**, by turning safelisting on (`require_id: true`) against a local
manifest and sending a request with an operation ID that isn't in it:

| Metric | Answers | Type | Get it by | Where |
|---|---|---|---|---|
| `apollo.router.operations.persisted_queries` by `persisted_queries.not_found` | Was an operation rejected because its ID isn't in the manifest? | counter | `requires persisted queries` | metric |
| `apollo.router.uplink.fetch.count.total` / `.duration.seconds` | Is the manifest being delivered at all? | counter / histogram | `default` | metric · [section 12](#12-diagnostic-sentinels--the-silent-failures) |

`persisted_queries.not_found = true` arrived in Dynatrace exactly as expected
from that test. This is the counter to use for "how many requests are being
rejected for an unrecognized operation ID" — it exists, and it charts.

**A second, narrower gap remains — attribution, not existence.** The same
counter also carries `persisted_queries.safelist.rejected.unknown`,
`.safelist.enforcement_skipped` and `.logged` for a related but different
scenario (a freeform request evaluated under audit/logging mode, rather than
an ID lookup that failed) — confirmed in the router's own source, not
independently tested here, since triggering it needs a different
configuration than the one above. None of these attributes carry which
*operation* or *subgraph* was involved — only that a rejection happened. That
gap is tracked upstream: [router#6864](https://github.com/apollographql/router/issues/6864)
is a still-open request for exactly `operation_name` and `subgraph` on this
signal, and the requester's own description — "we currently have a dumber
visualization that simply shows the count of unknown operations" — confirms
the count-level signal already exists; it's the per-operation detail that
doesn't. If attribution matters more than the count, that issue is worth
reading before building a Rhai/coprocessor workaround for it.

**The silent failure is still upstream of the rejection.** Uplink stops
delivering the manifest → the router keeps serving a stale list indefinitely
→ a new client release's operation IDs are rejected, while overall request
volume and error rate barely move because the failure is scoped to one
release. Uplink fetch success is the earlier signal, and worth watching
alongside the rejection counter rather than instead of it.

---

## 8. Auth

Authentication (is this caller who they claim to be) and authorization (are they
allowed to see this field) both have a real signal on default configuration —
one from a dedicated counter, one from `graphql_error`'s `code` attribute.

**Authentication.**

| Metric | Answers | Type | Get it by | Where |
|---|---|---|---|---|
| `apollo.router.operations.authentication.jwt` | Are JWT validations failing, and why? | counter | `requires JWT auth` | metric |

**Verified**, by actually enabling JWT auth against an unreachable JWKS
endpoint and sending a request: `authentication.jwt.failed = true` arrived in
Dynatrace as expected. `authentication.jwt.failure_code` did not — every one
of five test failures (all `CANNOT_FIND_KID`, since the JWKS was unreachable)
came back with no value for that attribute in this setup. Documentation lists
`CANNOT_DECODE_JWT`, `INVALID_AUDIENCE` and `CANNOT_FIND_KID` as the failure
codes this attribute should carry; treat that as accurate for the attribute's
existence and unverified for whether it actually populates in your
configuration until you check.

Either way, `.failed = true` alone already covers the case this section cares
about: a run of JWT failures distinguishes "the identity provider is having a
problem" from "this caller sent a bad token," which the `401` status code
alone cannot.

The older name, `apollo.router.operations.jwt`, is superseded by the one above.

**Authorization directives** (`@authenticated`, `@requiresScopes`). On default
configuration, a filtered field is **not** silent: the router adds an entry to
the GraphQL `errors` array with code `UNAUTHORIZED_FIELD_OR_TYPE`
(`authorization.directives.errors.response` defaults to `errors`), so it is
visible through the metric already in this list:

| Metric | Answers | Type | Get it by | Where |
|---|---|---|---|---|
| `apollo.router.graphql_error` filtered to `code == "UNAUTHORIZED_FIELD_OR_TYPE"` | Is authorization removing fields from responses? | counter | `default` | metric · [how to read it](../dashboards/dql-queries.md#traffic-and-health) |

The silent case is narrower than it first looks: it only happens if
`authorization.directives.errors.response` has been explicitly set to
`disabled`. On that configuration, nothing increments and a chart cannot see
it — worth confirming which setting is in place before assuming either way.

---

## 9. Coprocessor

| Metric | Answers | Type | Get it by | Where |
|---|---|---|---|---|
| `apollo.router.operations.coprocessor` by `coprocessor.stage` | How often is the coprocessor called, and does it succeed? | counter | `requires coprocessor.url` | metric |
| `apollo.router.operations.coprocessor.duration` by `coprocessor.stage` | How much latency does it add per stage? | histogram | `requires coprocessor.url` | metric |

A coprocessor adds a network hop to every request it handles, and **its time is
counted inside `apollo.router.overhead`** — so a slow coprocessor looks like router
regression. Published measurements put Rhai at approximately 100 µs p95 and coprocessors at
approximately 350 µs per stage; treat those as a budget, or measure Rhai directly
via [section 10](#10-rhai). Coprocessors are invoked **per
subgraph** at subgraph stages, so co-locate them.

**Known limitation:** the GraphQL operation name is not propagated into the
Router→coprocessor span, so Dynatrace shows only `/`. Coprocessor cost cannot be
attributed to an operation, and these metric tiles are the only coprocessor view
available until that is addressed upstream.

**Scope is a cost multiplier, not a fixed tax.** A coprocessor configured with
`body: true` and `context: all` at the router stage, and `body: true`,
`context: all`, `headers: true` and `uri: true` at every subgraph stage, means
one client operation triggers a coprocessor round trip at the router stage
plus one more per subgraph fetch in the query plan — each call serializing the
full body and the entire context to JSON and waiting on a synchronous reply
before the request continues. That cost is added serially, on every request,
and it lands inside `apollo.router.overhead` above, so a broadly-scoped
coprocessor shows up as router regression rather than as its own line item —
and it multiplies the effect of anything else on the request path that slows
down. Before enabling full body/context scope on every subgraph, confirm it's
actually consumed at that stage; narrowing scope per-subgraph is supported and
is the first lever to pull if overhead is climbing.

---

## 10. Rhai

| Metric | Answers | Type | Get it by | Where |
|---|---|---|---|---|
| `apollo.router.operations.rhai.duration` by `rhai.stage`, `rhai.succeeded` | How long does each Rhai callback take, and does it fail? | histogram | `requires rhai.scripts` | metric |

Rhai runs **synchronously in the request path**, same as a coprocessor, but
in-process rather than over a network hop — which is why it's roughly 3-4x
cheaper per call (the ~100 µs p95 vs. ~350 µs per stage referenced in
[section 9](#9-coprocessor)). Its time is counted inside
`apollo.router.overhead` for the same reason a coprocessor's is: both are
plugin execution, and the router doesn't distinguish the two in that
histogram. A Rhai regression and a router regression look identical there —
this metric is what separates them.

`rhai.stage` names the lifecycle hook the callback ran in —
`RouterRequest`, `RouterResponse`, `SupergraphRequest`, `SupergraphResponse`,
`ExecutionRequest`, `ExecutionResponse`, `SubgraphRequest`, `SubgraphResponse`
— so a script with hooks at multiple stages doesn't collapse into one
"Rhai is slow" number; you can see which specific hook is adding time. As
with the coprocessor in the previous section, a hook defined at
`SubgraphRequest` or `SubgraphResponse` runs **once per subgraph fetch in the
query plan**, not once per client operation — the same fan-out multiplier
applies.

`rhai.succeeded` is a boolean, and worth alerting on separately from
duration: a script that throws terminates the request with an error, so a
rise in `rhai.succeeded: false` is a correctness problem (a bad script
deploy, an unexpected null in `context`), not a performance one, even though
both surface through this same instrument.

---

## 11. Traffic shaping — timeout hierarchy

Not a metric, but the config that decides which error code a subgraph timeout
turns into — which then does show up in the metrics above, either as a
`apollo.router.graphql_error` on a partial response or as a straight 504 on
the whole operation, depending on which timeout fires first.

With no `traffic_shaping` section configured, the router applies its preset
default: **30 seconds for every timeout**, client-to-router and
router-to-subgraph alike. [Apollo's own guidance](https://www.apollographql.com/docs/graphos/routing/performance/traffic-shaping)
is explicit that these should not be equal:

> Set your router timeout to be greater than your subgraph timeouts. This
> difference provides a processing buffer, which enables the router to
> properly handle subgraph timeouts and return appropriate error responses to
> clients.

With both at 30s, an unresponsive subgraph and the router's own timeout expire
at effectively the same moment. The router has no buffer left to handle the
subgraph timeout on its own terms, so instead of returning HTTP 200 with
partial data and a scoped `SUBREQUEST_HTTP_ERROR`, it hits its own timeout
first and returns HTTP 504 `GATEWAY_TIMEOUT` for the **entire** operation —
turning one slow subgraph into a full-operation failure.

Baseline configuration:

```yaml
traffic_shaping:
  router:
    timeout: 60s          # must be greater than every subgraph timeout below
  all:
    timeout: 30s           # default for any subgraph not named explicitly
  subgraphs:
    <subgraph-name>:
      timeout: 10s          # fail fast on a subgraph known to be unreliable
```

Keep the router timeout below whatever ceiling exists at the edge/ingress and
any sidecar proxy (Envoy, etc.) in front of it — otherwise the outer layer
times out first and the router's own handling never gets a chance to run.

**This treats the symptom, not the cause.** It stops one unresponsive
subgraph from cancelling an entire operation, but a subgraph that is
consistently timing out is still worth root-causing on its own — this config
only changes what the client sees while that happens. It's also worth
confirming this section hasn't silently regressed: a `traffic_shaping.router.timeout`
that used to be set well above the preset can revert to the 30s default if the
section is dropped from `router.yaml` (or moved to a config source that no
longer applies) without a corresponding change entry — a ceiling that quietly
drops raises the 504 rate without ever registering as a "recent change."

---

## 12. Diagnostic sentinels — the silent failures

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

## 13. Memory (internals)

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

## 14. Connectors and subscriptions

Neither exists until the corresponding feature is configured. Confirm each one
arrives before building a chart or alert on it.

| Metric | Answers | Type | Get it by | Where |
|---|---|---|---|---|
| `http.client.request.duration` by `connector.source.name` | Is a REST source degrading? Surfaces as router latency if not split out. | histogram | `requires Apollo Connectors` | metric |
| `apollo.router.operations.subscriptions.terminated.client` | How often do clients terminate subscriptions? | counter | `requires subscriptions` | metric |
| `apollo.router.skipped.event.count` | Are subscription events being dropped because a client can't keep up? | counter | `requires subscriptions` | metric |
| `apollo.router.opened.subscriptions` | — | UpDownCounter | **excluded** | unusable under delta |

`apollo.router.skipped.event.count` fires when the subgraph produces events
faster than a client consumes them — a client-side backpressure signal, not a
subgraph outage. Confirmed against Apollo's own router-instrument reference
(previous revisions of this doc could not confirm the name; it is
`apollo.router.skipped.event.count`, not a `skipped.event.count`-style
custom instrument).

---

## 15. Container and host

**Not router-emitted.** Dynatrace collects these itself via OneAgent or the
Kubernetes operator, so there is nothing to configure in the router — but the data
has to come from somewhere, and a router dashboard without CPU is missing the first
thing you check.

What matters: **CPU under ~50%**. Above that, `apollo.router.overhead` stops
meaning anything, because you are measuring scheduling delay rather than router
work. Apollo's own deployment autoscales at 75% CPU; their sizing guidance suggests
no CPU limit at all, to avoid throttling.

**A hard CPU limit can hide behind a healthy-looking utilization graph.** When
a container runs against `resources.limits.cpu`, the kernel throttles it in
short bursts once it hits that ceiling, while the *averaged* utilization line
can still read comfortably low — utilization and throttling are different
signals, and only one of them shows a limit being hit. The one that reveals it
is the container's CPU throttled time (the cgroup's throttling metric,
collected the same way as the utilization metrics above), cross-checked
against `kubectl describe pod` to confirm the limit is what you think it is. A
tightly-limited pod running query planning, Rhai scripts, coprocessor calls,
and Redis-backed caching under one ceiling is a credible way to get bursty
latency that a CPU-usage-only dashboard never shows.

**Graceful shutdown matters more once autoscaling is in play.** A pod without
a `preStop` delay can start terminating mid scale-down while the Service (and
any proxy in front of it, e.g. Envoy) is still routing connections to it —
producing a burst of edge-visible 504s that has nothing to do with router
health. If error spikes line up with HPA scaling or pod restart events, rule
that in or out before chasing the router itself.

Memory: prefer `jemalloc.resident` from section 13 over container RSS, since it
attributes to the router process rather than the pod.

---

## 16. Subgraph health — the gap

**The most-requested signal that does not exist.** `/health` tells you the router
pod is alive. It says nothing about whether subgraphs are reachable, so a router
can report healthy while the graph is unusable.

This is a recurring request from teams operating federated graphs at scale. It is
logged with Apollo, with no current implementation timeline; circuit breaking is
on the router's longer-term roadmap.

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

## 17. Metrics Dynatrace rejects

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
SDK enforces **2,000 datapoints per metric stream**. On overflow the router does
not error — it **strips the attributes**,
keeps the values, sets `otel.metric.overflow=true`, and increments
`apollo.router.telemetry.metrics.cardinality_overflow` (created only once an
overflow actually happens, so its absence does not confirm cardinality is
within bounds). The limit applies per export
batch. Attributes on **histograms** are the worst case, because you pay per bucket.

The router's own defaults do not emit high-cardinality attributes, so overflow is
almost always caused by attributes someone added. That is the argument for leaving
`graphql.operation.name` off: persisted queries ([section 7](#7-persisted-queries))
do bound the operation set, but a PQ manifest can comfortably exceed 2,000
entries, so PQs do not make the attribute safe — they only make the ceiling
easier to estimate.

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

## Where GraphOS Studio fits

Not a metrics question, but it decides what you build where — so it has its own
doc: [studio-vs-dynatrace-capabilities.md](studio-vs-dynatrace-capabilities.md),
what Studio holds that Dynatrace cannot (field-level usage, schema checks,
signature normalization, the PQ manifest) and the converse — starting with the
fact that Studio cannot alert, which is why this repo exists.
