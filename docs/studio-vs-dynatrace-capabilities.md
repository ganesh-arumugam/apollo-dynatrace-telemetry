# GraphOS Studio vs Dynatrace: what each holds that the other cannot

The question behind this doc: **"if we standardize on Dynatrace, what do we
lose?"** — and its mirror, what Dynatrace adds that Studio never will. This is
about capabilities, not numbers; for why the two report different latencies for
the same traffic, see
[studio-vs-dynatrace-latency.md](studio-vs-dynatrace-latency.md).

The short answer: you don't pick one. The two are on **independent pipelines**
(Apollo Usage Reporting protocol vs OTLP — protocol table in
[datadog-parity.md](datadog-parity.md#where-graphos-studio-fits--neither-delta-nor-cumulative)),
one router feeds both simultaneously, and each holds things the other has no
way to represent. The real decision is what you *build* where.

## What Studio holds that Dynatrace cannot

| Capability | Why an APM cannot replace it |
|---|---|
| Field-level usage | which clients use which fields — the input for safe deprecation. Dynatrace sees operations and spans, never per-field resolution per client |
| Schema checks against real traffic | CI gates a proposed schema change against what clients actually sent — needs the schema registry and the usage data in one place |
| Operation signature normalization | Studio groups functionally identical operations (aliases, whitespace, literals normalized); Dynatrace groups by span name, so the same operation written two ways is two rows |
| Client identity as a native dimension | `apollographql-client-name`/`-version` headers segment every Studio view. On the OTLP side client identity is just another attribute you'd have to add yourself — and leave off metrics anyway (`DT029`) |
| Variants and contracts | per-environment schema lifecycle and filtered schemas are registry concepts; the APM has no schema at all |
| The persisted-query manifest | the safelist itself lives in GraphOS. Dynatrace can count rejections (`apollo.router.operations.persisted_queries`) but holds no manifest to manage |
| Deprecation workflow | `@deprecated` + field usage answers "is this field safe to remove yet?" — a question composed entirely of things on this list |

## What Dynatrace holds that Studio cannot

| Capability | Why Studio cannot replace it |
|---|---|
| Alerting | **Studio cannot page anyone.** An external tool is mandatory for production alerting — this is the single reason this repo exists |
| Infrastructure correlation | the router as a process on a host in a cluster: CPU, memory, restarts, neighbors. Studio's world ends at the graph |
| Cross-service distributed traces | router spans join traces that start at a load balancer and end in a database, GraphQL or not; correlated with the services around the router |
| Logs correlated to traces | router stdout with `display_trace_id` lands on the trace it belongs to ([DT017](rules.md#dt017)); Studio ingests no logs |
| Unsampled operational metrics | every counter in [metrics.md](metrics.md), at full fidelity, queryable with DQL over long windows |
| One pane for the whole incident | when the graph is a symptom and the cause is a subgraph host, the investigation stays in one tool |

## What to build where

| Concern | Platform |
|---|---|
| Dashboards, on-call, alerting | Dynatrace — this repo |
| Schema lifecycle: checks, variants, contracts, deprecations | Studio |
| Per-client impact analysis ("who breaks if…") | Studio |
| Persisted-query management | Studio (manifest) + Dynatrace (rejection counts) |
| Incident investigation across services | Dynatrace |
| Verifying the two agree | [`scripts/compare_studio.py`](../scripts/compare_studio.py) |

## Two overlap traps

**Studio Insights does not consume your OTLP metrics.** It is a separate
channel (usage reporting), and it is sampled where the OTLP metrics are not —
comparing the two directly is a common source of confusion. Temporality
(`delta`/cumulative) has no effect on Studio either; it isn't on that pipeline
at all.

**Studio's traces are not these traces.** Studio *traces* can travel over OTLP
too, but to Apollo's endpoint, with their own sampler — a
Dynatrace trace and a Studio trace of the same request are separate objects.
Field-level execution detail lives in Studio; infrastructure context lives in
Dynatrace.
