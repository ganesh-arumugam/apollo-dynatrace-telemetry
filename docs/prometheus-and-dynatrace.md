# Running Prometheus and Dynatrace side by side

**Short answer: yes, both at once is supported**, and it's a common way to adopt
Dynatrace without disturbing an existing Prometheus setup.

`telemetry.exporters.metrics.prometheus` and `telemetry.exporters.metrics.otlp`
are sibling settings in `router.yaml`. The router measures each request once and
hands the result to every enabled exporter, so turning on the second one doesn't
change what the first one reports.

Ready-to-use config: [`templates/prometheus-and-dynatrace.router.yaml`](../templates/prometheus-and-dynatrace.router.yaml)

```
                    ┌──────────────────────────────┐
   Prometheus  ────▶│  router :9090/metrics        │   pull, cumulative
   (scrapes)        │                              │
                    │  Apollo Router               │
                    │                              │
                    └──────────────┬───────────────┘
                                   │ push, delta
                                   ▼
                        Dynatrace OTLP ingest
```

## What's shared and what isn't

| Shared by both exporters | Set per exporter |
|---|---|
| Which instruments exist | Endpoint |
| Attributes on each metric | Protocol |
| Resource attributes (`service.name`, `deployment.environment`) | Temporality |
| `common.buckets` — histogram boundaries | Batch settings |
| `common.views` — renames, per-metric buckets, drops | Listen address (Prometheus) |

The practical consequence: **improving bucket resolution helps both backends at
once**, and renaming a metric in `views` renames it everywhere. Test a `views`
change against both dashboards before shipping it.

## Cumulative on one side, delta on the other — both correct

The same counter appears **cumulative** on the Prometheus endpoint and **delta**
over OTLP to Dynatrace. That isn't a conflict:

- Prometheus is a *pull* model. A scrape reads a running total, and Prometheus
  derives rates by subtracting consecutive scrapes.
- Dynatrace stores *delta* metrics — each export says what happened in that
  interval.

Think of an odometer versus a trip meter. Same journey, two displays. The
`temporality` setting lives on the OTLP exporter only; it doesn't affect the
Prometheus endpoint, and it doesn't change the measurement.

This is also a handy illustration for the latency-comparison question: if
temporality changed the data, the same router couldn't feed both formats and have
both be right. See
[`docs/studio-vs-dynatrace-latency.md`](studio-vs-dynatrace-latency.md).

## Three ways to arrange it

| | Router exports | Prometheus reads from | Best when |
|---|---|---|---|
| **A. Two exporters in the router** | Prometheus endpoint + OTLP to Dynatrace | the router | quickest to try; no new components |
| **B. Collector fan-out** | OTLP once, to the collector | the collector | you want one egress path and one place to change destinations |
| **C. Collector scrapes the router** | Prometheus endpoint only | the router (directly) *and* the collector | Prometheus is already scraping the router and you'd rather not touch it |

All three are supported. **B is usually the better fit** if a collector is
already in the picture or is likely to be.

### A. Both exporters in the router

[`templates/prometheus-and-dynatrace.router.yaml`](../templates/prometheus-and-dynatrace.router.yaml)

The router serves `/metrics` for Prometheus and pushes OTLP to Dynatrace. No new
components, and each backend is independent — handy during a trial when you may
want to switch Dynatrace off again quickly.

### B. Collector fan-out — router pushes once, both backends consume

[`templates/collector/otel-collector-fanout.yml`](../templates/collector/otel-collector-fanout.yml)

```
   Router ──OTLP──▶ Collector ─┬─▶ Prometheus scrape endpoint (:8889/metrics)
                               └─▶ Dynatrace (OTLP/HTTP, delta)
```

The router exports once and knows about neither backend. Prometheus scrapes the
**collector** instead of the router; Dynatrace is fed from the same data.

The detail that makes this work: **temporality is per-branch, so the split
happens in the collector.** Prometheus wants cumulative, Dynatrace wants delta,
and the router can only choose one temporality for its OTLP exporter. So:

1. The router exports **cumulative** — the OpenTelemetry default, so you simply
   leave `temporality` unset.
2. Two metrics pipelines read the same receiver:
   - `metrics/prometheus` → the `prometheus` exporter, cumulative, untouched.
   - `metrics/dynatrace` → `cumulativetodelta` → `otlphttp/dynatrace`.

Two pipelines sharing one receiver is the normal way to apply different
processing to the same data — nothing exotic.

If your Prometheus accepts remote write (or you run Mimir/Thanos), swap the
`prometheus` exporter for `prometheusremotewrite` and push instead of being
scraped. The template has that variant commented in.

Why convert on the Dynatrace side rather than the Prometheus side:
`cumulativetodelta` is long-standing and widely used, while the reverse
(`deltatocumulative`) is newer — check its stability level for your collector
version before relying on it.

What you gain: one egress path (easier through a proxy or firewall review), one
place to add a third destination later, and the ability to sample, filter, or
redact centrally. What it costs: a component to run and monitor.

### C. Collector scrapes the router's Prometheus endpoint

[`templates/collector/otel-collector-dynatrace.yml`](../templates/collector/otel-collector-dynatrace.yml)

The router keeps its Prometheus endpoint, your existing Prometheus keeps scraping
it, and a collector scrapes the same endpoint to forward to Dynatrace. Nothing
about the current Prometheus setup changes.

A scrape is always cumulative, so this pipeline needs `cumulativetodelta` before
the Dynatrace exporter. `scripts/validate_collector.py` checks for it (`DTC004`).

## Practical notes

- **Cardinality is counted twice.** Each attribute you add multiplies series in
  Prometheus *and* data points billed by Dynatrace. Worth a look at
  `graphql.operation.name` before enabling it on a busy graph.
- **Metric names differ by convention.** Prometheus normalises dots to
  underscores and appends unit suffixes, so `http.server.request.duration` in
  Dynatrace is `http_server_request_duration_seconds` on the scrape endpoint. The
  same metric, spelled per each ecosystem's convention.
- **Expose the Prometheus port deliberately.** `listen: 0.0.0.0:9090` is
  reachable from outside the pod; use `127.0.0.1` plus a sidecar if you'd rather
  it wasn't.
- **Scrape interval and export interval are independent.** Prometheus pulls on
  its own schedule; the OTLP `batch_processor.scheduled_delay` controls the push.
  Short windows won't line up exactly between the two — expected, not a fault.
- **Validate before rollout:**

  ```bash
  python3 scripts/validate_dynatrace.py templates/prometheus-and-dynatrace.router.yaml
  ```

## Migration shape that tends to work

1. Keep Prometheus exactly as it is; add the OTLP exporter alongside.
2. Run both for a couple of weeks and compare the panels you rely on.
3. Move alerting over once the Dynatrace numbers are familiar.
4. Decide later whether to keep Prometheus for local/short-term debugging — plenty
   of teams keep both permanently, since the scrape endpoint is useful for
   in-cluster debugging without leaving the pod.

## Sources

- [Router metrics exporters overview — `common`, `prometheus`, `otlp`](https://www.apollographql.com/docs/graphos/routing/observability/router-telemetry-otel/telemetry-pipelines/metrics-exporters/overview)
- [Prometheus exporter](https://www.apollographql.com/docs/graphos/routing/observability/router-telemetry-otel/telemetry-pipelines/metrics-exporters/prometheus)
- [OTLP exporter](https://www.apollographql.com/docs/graphos/routing/observability/router-telemetry-otel/telemetry-pipelines/metrics-exporters/otlp)
