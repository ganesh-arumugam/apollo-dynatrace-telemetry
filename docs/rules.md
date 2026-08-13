# Rule definitions

Every rule the two validators enforce, with the symptom you'd see without it,
the mechanism behind it, and the fix. The README tables are the one-line index;
this file is the reference. Based on Apollo Router 2.17, the latest release —
startup-failure rules in particular can shift between major versions.

Findings link here directly: `--json` output carries a `docs` field per finding
(`docs/rules.md#dt002`), so a CI failure is one click from its fix.

**Levels.** An `error` means data loss, a router that won't start, or a
committed secret — the exit code goes to 1. A `warning` is a cost, correctness,
or observability trap that can be legitimate in some setups — exit 0 unless
`--strict`.

**Modes.** `direct` rules apply when the router talks straight to Dynatrace;
`collector` rules when it talks to an OTel Collector. The mode is inferred per
exporter from the endpoint (a Dynatrace host or `/api/v2/otlp` path means
direct) and can be forced with `--mode`. A gRPC hop to a local collector is
correct; the same hop to Dynatrace never works — so rules from one topology are
not applied to the other.

**What these rules cannot see.** They check the Dynatrace contract, not the
router's full config schema — see [DT000](#dt000) for how to close that gap.

---

## Router rules (`scripts/validate_dynatrace.py`)

<a id="dt000"></a>
### DT000 — the router itself rejects this config

- **Level:** error · **Applies to:** both · **Opt-in:** `--router-bin`

**Symptom.** A config that passes every rule in this file still stops the
router at startup — or starts it with an instrument silently misconfigured.

**Why.** This validator checks the Dynatrace contract. It does not replicate
the router's own config schema, and can't: a real bug in this repo's template —
`graphql.operation.name` on a router-scoped instrument, an attribute that only
exists at supergraph scope — passed every DT rule and was caught only by
`router config validate`. DT000 makes that boundary explicit instead of
implicit: pass `--router-bin` and the router's own validator runs first on each
file, its rejection reported as a DT000 error.

**Fix.**

```
./scripts/validate_dynatrace.py router.yaml --router-bin ./router
```

The router expands `${env.*}` at validate time, so any variables the config
references must be set (source your `.env` first). Without `--router-bin`, the
non-JSON output says the schema layer went unchecked rather than implying it
passed.

<a id="dt001"></a>
### DT001 — otlp protocol must be `http`

- **Level:** error · **Applies to:** direct

**Symptom.** No data in Dynatrace; exporter errors in the router log.

**Why.** Dynatrace's OTLP ingest speaks HTTP/protobuf only. Any other value
(`thrift`, a typo) can't connect at all. The specific case of `grpc` — the
router's default — is split out as [DT009](#dt009) because omitting the key
entirely lands there too.

**Fix.**

```yaml
telemetry:
  exporters:
    metrics:
      otlp:
        protocol: http
```

<a id="dt002"></a>
### DT002 — metrics temporality must be `delta`

- **Level:** error · **Applies to:** direct

**Symptom.** Traces and gauges arrive; counters and histograms never do. Every
export gets a 2xx, so nothing anywhere reports a failure.

**Why.** Dynatrace does not support cumulative temporality, and OTel's default
*is* cumulative. Cumulative counters are accepted with a 2xx and then dropped —
the single most invisible failure in this whole setup, which is why the rule
fires on a missing key, not just a wrong one.

**Fix.**

```yaml
telemetry:
  exporters:
    metrics:
      otlp:
        temporality: delta
```

<a id="dt003"></a>
### DT003 — endpoint path must match its signal

- **Level:** error · **Applies to:** direct

**Symptom.** One signal missing (say, metrics) while another works, with 200s
in the router log.

**Why.** Each signal posts to its own path — `/api/v2/otlp/v1/metrics`,
`/v1/traces`, `/v1/logs`. A traces path on the metrics exporter returns 200 and
discards the payload. Copy-pasting one exporter block into another is exactly
how this happens.

**Fix.** Match the suffix to the exporter: the configs in this repo build it as
`${env.DYNATRACE_ENV_URL}/api/v2/otlp/v1/metrics` per signal.

<a id="dt004"></a>
### DT004 — endpoint host must carry an explicit port

- **Level:** error · **Applies to:** direct

**Symptom.** Exporter connection failures, or nothing at all, on an endpoint
that looks obviously correct.

**Why.** The router's OTLP/HTTP exporter does not default to 443. A bare
`https://abc12345.live.dynatrace.com/...` is a silent connection failure.

**Fix.** `https://abc12345.live.dynatrace.com:443/api/v2/otlp/v1/metrics` —
or the ActiveGate port (`:9999`) in that topology. Skipped when the whole host
comes from a variable, since the port lives in the variable's value.

<a id="dt005"></a>
### DT005 — Authorization must be present and start with `Api-Token `

- **Level:** error · **Applies to:** direct

**Symptom.** 401s from ingest — or, with `Bearer`, a failure that reads like a
platform-permissions problem instead of a header-format problem.

**Why.** OTLP ingest authenticates with `Api-Token <token>` (a `dt0c01.` token
with the ingest scopes). `Bearer` is the *dashboard/platform* API's scheme —
the two are easy to swap, and each is rejected where the other belongs. See
[dynatrace-credentials.md](dynatrace-credentials.md) for the three credential
types side by side.

**Fix.**

```yaml
http:
  headers:
    Authorization: "Api-Token ${env.DYNATRACE_API_TOKEN}"
```

<a id="dt006"></a>
### DT006 — the token must come from `${env.*}` / `${file.*}`

- **Level:** error · **Applies to:** both

**Symptom.** None at runtime — the config works. The failure is a live ingest
token in your git history.

**Why.** A literal `dt0c01....` in the YAML is a committed credential. The
router expands `${env.NAME}` and `${file.path}` at load time, so there is no
functional reason to inline it. On the collector hop, the token usually belongs
in the collector's config, not the router's.

**Fix.** `Authorization: "Api-Token ${env.DYNATRACE_API_TOKEN}"` — and rotate
any token that was ever committed.

<a id="dt007"></a>
### DT007 — the exporter must be explicitly `enabled: true`

- **Level:** error · **Applies to:** both

**Symptom.** A fully-written exporter block, endpoint and auth and all, that
exports nothing.

**Why.** The router's OTLP exporter defaults to *disabled*. A block without
`enabled: true` is inert config that reads as if it works.

**Fix.** `enabled: true` on each `otlp:` block.

<a id="dt008"></a>
### DT008 — endpoint must be a real URL

- **Level:** error · **Applies to:** both

**Symptom.** Telemetry goes to the OTLP default endpoint (localhost) or
nowhere, depending on version.

**Why.** `endpoint: default` and an empty endpoint are accepted by the config
schema but mean "not Dynatrace." Every real setup names its destination.

**Fix.** An explicit URL, normally via `${env.DYNATRACE_ENV_URL}` — see
[.env.example](../.env.example) for the SaaS / Managed / ActiveGate shapes.

<a id="dt009"></a>
### DT009 — gRPC against Dynatrace is always wrong

- **Level:** error · **Applies to:** direct

**Symptom.** No data; connection or protocol errors in the router log.

**Why.** Dynatrace has no OTLP/gRPC ingest, and gRPC is the router's *default*
protocol — so leaving `protocol:` out entirely is the same mistake as writing
`protocol: grpc`, and the rule fires on both.

**Fix.** `protocol: http` on every Dynatrace-bound exporter.

<a id="dt010"></a>
### DT010 — endpoint scheme must be https

- **Level:** error · **Applies to:** direct

**Symptom.** A plaintext hop carrying your ingest token, or a scheme mismatch
that never connects.

**Why.** Dynatrace tenants are TLS-only. The one legitimate exception is a
loopback endpoint in the test harness, which is why `--allow-loopback` exists —
and why it's opt-in, so a harness config can't ship to production unnoticed.

**Fix.** `https://` — or `--allow-loopback` for `127.0.0.1`/`localhost` in
harness runs only.

<a id="dt011"></a>
### DT011 — `default_requirement_level` must be a valid value

- **Level:** error · **Applies to:** both

**Symptom.** The router refuses to start.

**Why.** The only values are `required`, `recommended`, and `none`. Anything
else (`all`, `full`) fails schema validation.

**Fix.** Use `required` — see [DT016](#dt016) for why not `recommended`.

<a id="dt012"></a>
### DT012 — custom instrument names must follow OTel conventions

- **Level:** error/warn · **Applies to:** both

**Symptom.** Metrics that collide with standard ones, double-suffixed names
(`_total_total`) in Dynatrace, or metrics you can't find later.

**Why.** Four checks: `http.*` / `apollo.*` are reserved standard-instrument
namespaces (error); OTel appends `_total` at export so a name that already has
it doubles up (error); the unit belongs in `unit:`, not the name (error); and
dot-namespacing (`acme.router.requests`) is the convention everything else in
Dynatrace follows (warn).

**Fix.** `<org>.<component>.<thing>`: `acme.router.requests`, unit in `unit:`,
no `_total`.

<a id="dt013"></a>
### DT013 — custom instruments must declare `value` and a valid `type`

- **Level:** error/warn · **Applies to:** both

**Symptom.** The router won't start (bad type), or a metric lands in Dynatrace
with no description or unit for the next on-call to read.

**Why.** Custom instrument types are `counter` and `histogram` — the router
supports nothing else, so `gauge` or `up_down_counter` is a startup failure
(error). Missing `description`/`unit` export fine but strand whoever finds the
metric later (warn): the description becomes the metric description in
Dynatrace.

**Fix.**

```yaml
acme.router.requests:
  value: unit
  type: counter
  unit: "{request}"
  description: "Router HTTP requests, by response status code"
```

<a id="dt014"></a>
### DT014 — batch_processor must be sane for a rate-limited SaaS endpoint

- **Level:** warn · **Applies to:** both

**Symptom.** 429s / dropped batches under load; or exports rejected for size.

**Why.** Dynatrace rate-limits per token. A `scheduled_delay` under 1s hammers
the endpoint (5s is the documented default and usually right outside local
testing); a `max_export_batch_size` above 2048 risks the OTLP HTTP message size
limit.

**Fix.** Leave the defaults alone unless you have a measured reason not to.

<a id="dt015"></a>
### DT015 — a trace sampler of 1.0 is flagged

- **Level:** warn · **Applies to:** both

**Symptom.** Rate-limiting on a busy router, and a DDU bill that dominates the
Dynatrace spend.

**Why.** Sampling every request is fine on a laptop and expensive at
production traffic. Cost scales linearly with the sampler.

**Fix.** `sampler: 0.05`-ish in production; tune to traffic. Keep
`parent_based_sampler: true` so upstream decisions are honored
([DT018](#dt018)).

<a id="dt016"></a>
### DT016 — `graphql.document`-class attributes are flagged

- **Level:** warn · **Applies to:** both

**Symptom.** Unbounded metric cardinality — every distinct query text is a new
series — and possibly PII in your telemetry backend.

**Why.** `recommended` requirement level attaches development-status GraphQL
conventions including `graphql.document`, the full operation text. Defensible
on sampled spans; never on metrics. Apollo recommends `required`.

**Fix.** `default_requirement_level: required`, and no
`graphql.document: true` on any instrument. For per-operation analysis, use
spans.

<a id="dt017"></a>
### DT017 — traces without a trace id in the logs

- **Level:** warn · **Applies to:** both

**Symptom.** The Logs tab on every Dynatrace trace is empty; nothing correlates
a log line to a span.

**Why.** Log/trace correlation needs the trace id printed on each log line in a
format Dynatrace recognizes. The router does this only when asked.

**Fix.**

```yaml
telemetry:
  exporters:
    logging:
      stdout:
        format:
          json:
            display_trace_id: open_telemetry
```

<a id="dt018"></a>
### DT018 — `parent_based_sampler: false` splits distributed traces

- **Level:** warn · **Applies to:** both

**Symptom.** Traces that stop at the router, or router spans orphaned from the
caller's trace.

**Why.** With the parent-based sampler off, the router ignores the upstream
sampling decision. Anything in front of it that already sampled — OneAgent, a
gateway — gets its trace cut in half.

**Fix.** `parent_based_sampler: true` unless the router is genuinely the trace
origin.

<a id="dt019"></a>
### DT019 — `spans.mode: deprecated`

- **Level:** warn · **Applies to:** both

**Symptom.** Span names that group badly in Dynatrace's service and request
analysis views.

**Why.** `deprecated` is the legacy Router v1 span shape. `spec_compliant`
follows OTel semantic conventions, which is what Dynatrace's views key on.

**Fix.** `spans: { mode: spec_compliant }`.

<a id="dt020"></a>
### DT020 — spans `default_attribute_requirement_level: recommended`

- **Level:** warn · **Applies to:** both

**Symptom.** Full GraphQL documents on every span.

**Why.** Same mechanism as [DT016](#dt016), on the spans side: `recommended`
pulls in `graphql.document`. On sampled spans this is a deliberate trade-off at
best — and not acceptable at all if operations can carry PII.

**Fix.** Keep the level `required`; enable specific attributes by name where
needed.

<a id="dt021"></a>
### DT021 — no `otel.status_code` error marking

- **Level:** warn · **Applies to:** both

**Symptom.** A subgraph starts failing and Dynatrace's failure rate stays flat
at zero.

**Why.** GraphQL errors return HTTP 200. Unless the span is explicitly marked
`ERROR` on `on_graphql_error` / `subgraph_on_graphql_error`, Dynatrace counts
every failed operation as a success. This is the single most important span
setting in the pack — see [templates/spans.router.yaml](../templates/spans.router.yaml).

**Fix.**

```yaml
supergraph:
  attributes:
    otel.status_code:
      static: ERROR
      condition:
        eq: [true, {on_graphql_error: true}]
```

<a id="dt022"></a>
### DT022 — `telemetry.exporters.logging.otlp` does not exist

- **Level:** error · **Applies to:** both

**Symptom.** The router refuses to start.

**Why.** The router has no OTLP log exporter — `logging:` accepts only
`common` and `stdout`, and rejects unknown keys. Two variants fire: an `otlp:`
block under logging, and a `logging:` key left behind empty after deleting one
(the router rejects the null block too). Logs reach Dynatrace by shipping
stdout — see [templates/dynatrace-logs.router.yaml](../templates/dynatrace-logs.router.yaml).

**Fix.** Remove the `otlp:` block (and the `logging:` key too, or give it a
`stdout:` block).

<a id="dt023"></a>
### DT023 — OTLP ingest is on the `.live.` host, not `.apps.`

- **Level:** error · **Applies to:** direct

**Symptom.** An endpoint that answers — so everything looks wired up — while
ingest 404s (Api-Token) or 403s (Bearer) and nothing is ever stored.

**Why.** `<env>.apps.dynatrace.com` is the platform/UI host; you'll have it in
your clipboard because it's the one in your browser bar. Ingest is served by
`<env>.live.dynatrace.com` only.

**Fix.** Swap `.apps.` for `.live.`. The dashboard-import script is the
opposite case: *it* wants `.apps.`.

<a id="dt024"></a>
### DT024 — unknown condition operator

- **Level:** error · **Applies to:** both

**Symptom.** The router refuses to start.

**Why.** The router's condition operators are `eq`, `gt`, `lt`, `exists`,
`all`, `any`, `not`. `gte`/`lte` look plausible and don't exist — the config
fails schema validation at load.

**Fix.** Express `>= 500` as `gt` 499, `<= N` as `lt` N+1.

<a id="dt025"></a>
### DT025 — sandbox requires introspection

- **Level:** error · **Applies to:** both

**Symptom.** The router refuses to start ("sandbox requires introspection"),
so no telemetry is exported at all.

**Why.** `sandbox.enabled: true` without `supergraph.introspection: true` is
rejected as a pair at config load.

**Fix.** Set `supergraph: { introspection: true }` — or turn sandbox off
outside local development, which is where it belongs anyway.

<a id="dt026"></a>
### DT026 — histogram buckets too coarse

- **Level:** warn · **Applies to:** both

**Symptom.** A p95 of "2.3s" that's really "somewhere between 1s and 5s", and
OTel percentiles that will not match GraphOS Studio's.

**Why.** Percentiles are interpolated inside histogram buckets, so bucket width
*is* the error bar. The router's default boundaries jump 0.5 → 1.0 → 5.0 → 10.0:
a percentile landing in the 1–5s gap is interpolated inside a bucket 400% wide.
Three variants fire: no buckets configured (defaults apply), a gap wider than
3x, and a top boundary below 10s (slower requests all land in +Inf, making
timeouts invisible). Full mechanism: [percentiles-and-buckets.md](percentiles-and-buckets.md).

**Fix.** [templates/histogram-buckets.router.yaml](../templates/histogram-buckets.router.yaml)
— no gap in your p95/p99 region wider than ~1.5x, top boundary at or above your
request timeout.

<a id="dt027"></a>
### DT027 — a `views` entry with a wildcard silently matches nothing

- **Level:** warn · **Applies to:** both

**Symptom.** You "dropped" a noisy metric with a view and it keeps arriving —
or a rename/re-bucket never takes effect — with no error anywhere.

**Why.** Views match exact instrument names only. `*`, `apollo.*`, `apollo_*`
all parse fine and then match nothing, silently — despite wildcard examples
circulating in documentation. The operator believes the cardinality problem is
handled; the bill says otherwise.

**Fix.** Enumerate exact metric names, one view each, and verify each drop
actually took effect in the tenant. Better: budget by not enabling attributes
in the first place — see the cardinality section of [metrics.md](metrics.md#attributes-are-what-cost-you-not-metrics).

<a id="dt028"></a>
### DT028 — no `service_name` on a Dynatrace-bound metrics exporter

- **Level:** warn · **Applies to:** direct

**Symptom.** Ingest works, `verify_ingest.sh` finds the metrics — and every
tile on the imported dashboard is blank.

**Why.** Without `service_name`, the router exports as OTel's fallback
`unknown_service:router`. Every tile in the generated dashboard and every query
in the DQL pack filters `service.name == "apollo-router"`, so the data and the
dashboard never meet. This is the most-asked "why is my dashboard empty"
question in the README, promoted to a rule.

**Fix.**

```yaml
telemetry:
  exporters:
    metrics:
      common:
        service_name: apollo-router
```

Or rebuild the dashboard against your name:
`python3 scripts/build_dashboard.py --service-name my-router`.

<a id="dt029"></a>
### DT029 — operation-name attributes on metrics

- **Level:** warn · **Applies to:** both

**Symptom.** Metrics fine for weeks, then attributes silently vanish from new
datapoints and `otel.metric.overflow` appears; the Dynatrace bill climbs with
schema growth.

**Why.** `graphql.operation.name: true` on a metric means one series per
distinct operation name. The OTel SDK hard-caps a metric stream at 2,000
datapoints and silently strips attributes past it, and Dynatrace bills per
ingested series. And the remedy people reach for afterwards — views — has its
own trap ([DT027](#dt027)).

**Fix.** Keep it off on metrics (the template ships it `false`, marked HIGH
CARD); use spans for per-operation analysis. Turning it on deliberately for a
small, bounded graph is a legitimate call — that's why this is a warning.

<a id="dt030"></a>
### DT030 — safelisting requires `apq.enabled: false`

- **Level:** error · **Applies to:** both

**Symptom.** The router refuses to start: "apqs must be disabled to enable
safelisting". Nothing runs, nothing is exported.

**Why.** Persisted-query safelisting and automatic persisted queries are
mutually exclusive by design — APQ registers unknown operations by hash on
first sight, which is exactly what a safelist exists to prevent. The router
rejects the pair at startup; APQ is on by default, so enabling safelisting
without explicitly disabling APQ always hits this.

**Fix.**

```yaml
apq:
  enabled: false

persisted_queries:
  enabled: true
  safelist:
    enabled: true
```

<a id="dt101"></a>
### DT101 — collector-bound protocol must be `grpc` or `http`

- **Level:** error · **Applies to:** collector hop

**Symptom.** The router can't connect to the collector.

**Why.** The hop to a collector is a normal OTLP connection — both protocols
work, anything else doesn't. This is the loose counterpart of
[DT001](#dt001)/[DT009](#dt009): what's an error against Dynatrace is fine
against a collector, which is exactly why the validator infers the topology
before applying rules.

**Fix.** `protocol: grpc` (collector receiver on 4317) or `http` (4318).

<a id="dt102"></a>
### DT102 — collector-bound metrics still need delta somewhere

- **Level:** warn · **Applies to:** collector hop

**Symptom.** Same as [DT002](#dt002) — counters accepted with a 2xx at the far
end and dropped — but the mistake hides one hop earlier.

**Why.** Whatever ultimately reaches Dynatrace must be delta. Either the router
exports delta, or the collector converts with `cumulativetodelta`. Neither is a
warning on its own; both missing is silent data loss, and this validator can
only see the router's half — hence a warning here and the collector-side check
in [DTC004](#dtc004).

**Fix.** `temporality: delta` at the router (simplest), or add the processor to
the collector's metrics pipeline.

<a id="dt103"></a>
### DT103 — collector-bound OTLP/HTTP path should be `/v1/{signal}`

- **Level:** warn · **Applies to:** collector hop

**Symptom.** 404s from the collector's OTLP receiver.

**Why.** OTLP/HTTP posts to `/v1/metrics`, `/v1/traces`, `/v1/logs`. A
Dynatrace-style `/api/v2/otlp/...` path pasted onto a collector endpoint is the
usual way this appears. Leaving the path off entirely lets the exporter append
the standard one — that's the safest form.

**Fix.** `endpoint: http://otel-collector:4318` and let the exporter do the
path.

---

## Collector rules (`scripts/validate_collector.py`)

<a id="dtc001"></a>
### DTC001 — the Dynatrace exporter must be `otlphttp`

- **Level:** error

**Symptom.** The collector starts and exports nothing to Dynatrace;
connection/protocol errors in collector logs.

**Why.** Dynatrace's OTLP ingest is HTTP/protobuf only, and the collector's
plain `otlp` exporter defaults to gRPC. The exporter *type* is the first
segment of its name — `otlp/dynatrace` is a gRPC exporter no matter what it's
called.

**Fix.** `otlphttp/dynatrace:` as the exporter key.

<a id="dtc002"></a>
### DTC002 — endpoint must not include a `/v1/<signal>` suffix

- **Level:** error

**Symptom.** 404s on export.

**Why.** The mirror image of the router-side [DT003](#dt003): the `otlphttp`
exporter *appends* `/v1/metrics`, `/v1/traces`, `/v1/logs` itself. An endpoint
already carrying the suffix posts to `/v1/metrics/v1/metrics`.

**Fix.** `endpoint: https://abc12345.live.dynatrace.com/api/v2/otlp` — stop
there.

<a id="dtc003"></a>
### DTC003 — Authorization must be `Api-Token` from an env reference

- **Level:** error/warn

**Symptom.** 401s from ingest, or a live token in git history.

**Why.** Same contract as [DT005](#dt005)/[DT006](#dt006), collector-side:
`Api-Token` scheme (`Bearer` is rejected), token via `${env:DT_API_TOKEN}` —
note the collector's `${env:NAME}` syntax differs from the router's
`${env.NAME}`. An inlined literal token is an error; a non-env reference that
isn't obviously a literal is a warning.

**Fix.**

```yaml
headers:
  Authorization: "Api-Token ${env:DT_API_TOKEN}"
```

<a id="dtc004"></a>
### DTC004 — cumulative sources need `cumulativetodelta`

- **Level:** error/warn

**Symptom.** [DT002](#dt002)'s symptom — counters 2xx'd and dropped — caused in
the collector.

**Why.** Prometheus-family and hostmetrics receivers emit cumulative counters;
Dynatrace only accepts delta. A metrics pipeline feeding Dynatrace from one of
those without a `cumulativetodelta` processor is guaranteed data loss (error).
An OTLP-fed pipeline is ambiguous — the sender may already export delta — so
that's a warning pointing back at the router config ([DT102](#dt102)). A
pipeline that *references* `cumulativetodelta` without defining it is an error.

**Fix.** Define the processor and put it in the pipeline, before `batch`:

```yaml
processors:
  cumulativetodelta: {}
service:
  pipelines:
    metrics:
      processors: [cumulativetodelta, batch]
```

<a id="dtc005"></a>
### DTC005 — the Dynatrace exporter must be wired into a pipeline

- **Level:** error/warn

**Symptom.** A perfect exporter definition and zero data, with nothing in the
logs — the collector doesn't run exporters no pipeline references.

**Why.** Defining an exporter and wiring it are separate steps, and the second
is the one that gets forgotten in a config merge. Defined-but-unused is an
error; no Dynatrace exporter found at all is a warning (this may simply not be
a Dynatrace config).

**Fix.** `service.pipelines.<signal>.exporters: [otlphttp/dynatrace]` for each
signal that should reach Dynatrace.

<a id="dtc006"></a>
### DTC006 — pipelines must batch, and reference only defined processors

- **Level:** error/warn

**Symptom.** Per-item HTTP calls that hit payload/rate limits (no batch), or a
collector that won't start (undefined processor reference).

**Why.** Unbatched exports send one request per item — Dynatrace's limits are
sized for batches. And a pipeline naming a processor that isn't defined in
`processors:` is a startup failure, the collector-side cousin of
[DT024](#dt024).

**Fix.** `batch: {}` in processors, referenced in every Dynatrace-bound
pipeline; define everything you reference.

<a id="dtc007"></a>
### DTC007 — log pipelines should trim request/response bodies

- **Level:** warn

**Symptom.** Log ingest volume (and DDU cost) far beyond what the log *lines*
justify — and possibly PII shipped to Dynatrace.

**Why.** With router events enabled, log records carry full request/response
bodies and headers. An `attributes` processor that deletes them before export
is the difference between shipping logs and shipping payloads.

**Fix.** See the `attributes/trim` processor in
[templates/collector/otel-collector-dynatrace.yml](../templates/collector/otel-collector-dynatrace.yml).

<a id="dtc008"></a>
### DTC008 — endpoint should be https

- **Level:** warn

**Symptom.** An ingest token on a plaintext hop.

**Why.** Same reasoning as [DT010](#dt010). A warning rather than an error here
because collector-to-ActiveGate hops inside a private network are sometimes
deliberately plain HTTP.

**Fix.** `https://` unless you've made that call explicitly.

<a id="dtc009"></a>
### DTC009 — retry/queue must not be disabled on the Dynatrace exporter

- **Level:** warn

**Symptom.** Gaps in Dynatrace during traffic spikes — exactly when you look at
the dashboard hardest — with `send_failed` counts in the collector's own
metrics.

**Why.** Dynatrace rate-limits per token and answers 429 with a `Retry-After`.
The exporter helper's `retry_on_failure` and `sending_queue` are enabled by
default and honor it — so this rule fires only on an explicit `enabled: false`,
which turns every throttled batch into dropped data. People disable these
chasing memory or latency; the cost is silent loss under throttling.

**Fix.** Remove the `enabled: false`. If memory is the concern, size
`sending_queue.queue_size` down instead of turning the queue off — the shipped
template uses 1000.
