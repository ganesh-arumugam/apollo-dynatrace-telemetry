#!/usr/bin/env python3
"""Generate the validator fixture corpus.

Each fixture is a minimal router config that isolates exactly one rule, so a
test failure names the rule that broke rather than "something in the big file".
Regenerate with:  python3 tests/make_fixtures.py
"""
from __future__ import annotations

import os
import textwrap

HERE = os.path.dirname(os.path.abspath(__file__))

GOOD = {
    "metrics_only.yaml": """
        telemetry:
          exporters:
            metrics:
              otlp:
                enabled: true
                protocol: http
                temporality: delta
                endpoint: https://abc12345.live.dynatrace.com:443/api/v2/otlp/v1/metrics
                http:
                  headers:
                    Authorization: "Api-Token ${env.DYNATRACE_API_TOKEN}"
    """,
    "all_signals.yaml": """
        telemetry:
          exporters:
            metrics:
              common:
                service_name: apollo-router
                buckets: [0.05, 0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.5, 5.0, 10.0, 20.0]
              otlp:
                enabled: true
                protocol: http
                temporality: delta
                endpoint: ${env.DYNATRACE_ENV_URL}/api/v2/otlp/v1/metrics
                http:
                  headers:
                    Authorization: "Api-Token ${env.DYNATRACE_API_TOKEN}"
            tracing:
              common:
                sampler: 0.05
              otlp:
                enabled: true
                protocol: http
                endpoint: ${env.DYNATRACE_ENV_URL}/api/v2/otlp/v1/traces
                http:
                  headers:
                    Authorization: "Api-Token ${env.DYNATRACE_API_TOKEN}"
            logging:
              stdout:
                enabled: true
                format:
                  json:
                    display_trace_id: open_telemetry
                    display_span_id: true
          instrumentation:
            instruments:
              default_requirement_level: required
              router:
                http.server.active_requests: true
                acme.router.requests:
                  value: unit
                  type: counter
                  unit: "{request}"
                  description: "Router HTTP requests"
    """,
    "lowercase_auth_header.yaml": """
        telemetry:
          exporters:
            metrics:
              otlp:
                enabled: true
                protocol: http
                temporality: delta
                endpoint: https://abc.live.dynatrace.com:443/api/v2/otlp/v1/metrics
                http:
                  headers:
                    authorization: "Api-Token ${env.DYNATRACE_API_TOKEN}"
    """,
    "no_telemetry.yaml": """
        supergraph:
          listen: 0.0.0.0:4000
    """,
    "collector_grpc.yaml": """
        telemetry:
          exporters:
            tracing:
              common:
                sampler: 0.1
                parent_based_sampler: true
              otlp:
                enabled: true
                protocol: grpc
                endpoint: http://otel-collector:4317
            metrics:
              otlp:
                enabled: true
                protocol: grpc
                temporality: delta
                endpoint: http://otel-collector:4317
            logging:
              stdout:
                enabled: true
                format:
                  json:
                    display_trace_id: open_telemetry
    """,
    "managed_endpoint.yaml": """
        telemetry:
          exporters:
            metrics:
              otlp:
                enabled: true
                protocol: http
                temporality: delta
                endpoint: https://dynatrace.example.com:443/e/abc12345/api/v2/otlp/v1/metrics
                http:
                  headers:
                    Authorization: "Api-Token ${env.DYNATRACE_API_TOKEN}"
    """,
    "activegate_endpoint.yaml": """
        telemetry:
          exporters:
            metrics:
              otlp:
                enabled: true
                protocol: http
                temporality: delta
                endpoint: https://activegate.internal:9999/e/abc12345/api/v2/otlp/v1/metrics
                http:
                  headers:
                    Authorization: "Api-Token ${env.DYNATRACE_API_TOKEN}"
    """,
    "spans_with_error_marking.yaml": """
        telemetry:
          instrumentation:
            spans:
              mode: spec_compliant
              default_attribute_requirement_level: required
              supergraph:
                attributes:
                  otel.status_code:
                    static: ERROR
                    condition:
                      eq:
                        - true
                        - on_graphql_error: true
              subgraph:
                attributes:
                  subgraph.name: true
                  otel.status_code:
                    static: ERROR
                    condition:
                      eq:
                        - true
                        - subgraph_on_graphql_error: true
    """,
    "collector_http_paths.yaml": """
        telemetry:
          exporters:
            metrics:
              otlp:
                enabled: true
                protocol: http
                temporality: delta
                endpoint: http://otel-collector:4318/v1/metrics
    """,
    "file_ref_token.yaml": """
        telemetry:
          exporters:
            metrics:
              otlp:
                enabled: true
                protocol: http
                temporality: delta
                endpoint: https://abc.live.dynatrace.com:443/api/v2/otlp/v1/metrics
                http:
                  headers:
                    Authorization: "Api-Token ${file.dt-token.txt}"
    """,
}

# fixture name -> (rule that must fire as an error, yaml)
BAD = {
    # The router has no OTLP log exporter; this key is a hard startup failure.
    "logging_otlp_exporter.yaml": ("DT022", """
        telemetry:
          exporters:
            tracing:
              otlp:
                enabled: true
                protocol: http
                endpoint: https://abc.live.dynatrace.com:443/api/v2/otlp/v1/traces
                http:
                  headers:
                    Authorization: "Api-Token ${env.DYNATRACE_API_TOKEN}"
            logging:
              stdout:
                enabled: true
                format:
                  json:
                    display_trace_id: open_telemetry
              otlp:
                enabled: true
                protocol: http
                endpoint: https://abc.live.dynatrace.com:443/api/v2/otlp/v1/logs
    """),
    # OTLP ingest lives on .live.; .apps. is the platform/UI host and 404s.
    "apps_host_endpoint.yaml": ("DT023", """
        telemetry:
          exporters:
            metrics:
              otlp:
                enabled: true
                protocol: http
                temporality: delta
                endpoint: https://abc12345.apps.dynatrace.com:443/api/v2/otlp/v1/metrics
                http:
                  headers:
                    Authorization: "Api-Token ${env.DYNATRACE_API_TOKEN}"
    """),
    # `gte` is not a router condition operator; the router refuses to start.
    "invalid_condition_operator.yaml": ("DT024", """
        telemetry:
          instrumentation:
            instruments:
              router:
                acme.router.server.errors:
                  value: unit
                  type: counter
                  unit: "{error}"
                  description: "5xx responses"
                  condition:
                    gte:
                      - response_status: code
                      - 500
    """),
    # sandbox without introspection is rejected at config load.
    "sandbox_without_introspection.yaml": ("DT025", """
        sandbox:
          enabled: true
        supergraph:
          listen: 127.0.0.1:4000
    """),
    "grpc_protocol.yaml": ("DT009", """
        telemetry:
          exporters:
            metrics:
              otlp:
                enabled: true
                protocol: grpc
                temporality: delta
                endpoint: https://abc.live.dynatrace.com:443/api/v2/otlp/v1/metrics
                http:
                  headers:
                    Authorization: "Api-Token ${env.DYNATRACE_API_TOKEN}"
    """),
    "invalid_protocol.yaml": ("DT001", """
        telemetry:
          exporters:
            metrics:
              otlp:
                enabled: true
                protocol: thrift
                temporality: delta
                endpoint: https://abc.live.dynatrace.com:443/api/v2/otlp/v1/metrics
                http:
                  headers:
                    Authorization: "Api-Token ${env.DYNATRACE_API_TOKEN}"
    """),
    "omitted_protocol.yaml": ("DT009", """
        telemetry:
          exporters:
            metrics:
              otlp:
                enabled: true
                temporality: delta
                endpoint: https://abc.live.dynatrace.com:443/api/v2/otlp/v1/metrics
                http:
                  headers:
                    Authorization: "Api-Token ${env.DYNATRACE_API_TOKEN}"
    """),
    "cumulative_temporality.yaml": ("DT002", """
        telemetry:
          exporters:
            metrics:
              otlp:
                enabled: true
                protocol: http
                temporality: cumulative
                endpoint: https://abc.live.dynatrace.com:443/api/v2/otlp/v1/metrics
                http:
                  headers:
                    Authorization: "Api-Token ${env.DYNATRACE_API_TOKEN}"
    """),
    "missing_temporality.yaml": ("DT002", """
        telemetry:
          exporters:
            metrics:
              otlp:
                enabled: true
                protocol: http
                endpoint: https://abc.live.dynatrace.com:443/api/v2/otlp/v1/metrics
                http:
                  headers:
                    Authorization: "Api-Token ${env.DYNATRACE_API_TOKEN}"
    """),
    "wrong_signal_path.yaml": ("DT003", """
        telemetry:
          exporters:
            metrics:
              otlp:
                enabled: true
                protocol: http
                temporality: delta
                endpoint: https://abc.live.dynatrace.com:443/api/v2/otlp/v1/traces
                http:
                  headers:
                    Authorization: "Api-Token ${env.DYNATRACE_API_TOKEN}"
    """),
    "missing_port.yaml": ("DT004", """
        telemetry:
          exporters:
            metrics:
              otlp:
                enabled: true
                protocol: http
                temporality: delta
                endpoint: https://abc.live.dynatrace.com/api/v2/otlp/v1/metrics
                http:
                  headers:
                    Authorization: "Api-Token ${env.DYNATRACE_API_TOKEN}"
    """),
    "bearer_auth.yaml": ("DT005", """
        telemetry:
          exporters:
            metrics:
              otlp:
                enabled: true
                protocol: http
                temporality: delta
                endpoint: https://abc.live.dynatrace.com:443/api/v2/otlp/v1/metrics
                http:
                  headers:
                    Authorization: "Bearer ${env.DYNATRACE_API_TOKEN}"
    """),
    "no_headers.yaml": ("DT005", """
        telemetry:
          exporters:
            metrics:
              otlp:
                enabled: true
                protocol: http
                temporality: delta
                endpoint: https://abc.live.dynatrace.com:443/api/v2/otlp/v1/metrics
    """),
    "inline_token.yaml": ("DT006", """
        telemetry:
          exporters:
            metrics:
              otlp:
                enabled: true
                protocol: http
                temporality: delta
                endpoint: https://abc.live.dynatrace.com:443/api/v2/otlp/v1/metrics
                http:
                  headers:
                    Authorization: "Api-Token dt0c01.ABC123.SECRETSECRETSECRET"
    """),
    "not_enabled.yaml": ("DT007", """
        telemetry:
          exporters:
            metrics:
              otlp:
                protocol: http
                temporality: delta
                endpoint: https://abc.live.dynatrace.com:443/api/v2/otlp/v1/metrics
                http:
                  headers:
                    Authorization: "Api-Token ${env.DYNATRACE_API_TOKEN}"
    """),
    "default_endpoint.yaml": ("DT008", """
        telemetry:
          exporters:
            metrics:
              otlp:
                enabled: true
                protocol: http
                temporality: delta
                endpoint: default
    """),
    "plain_http_scheme.yaml": ("DT010", """
        telemetry:
          exporters:
            metrics:
              otlp:
                enabled: true
                protocol: http
                temporality: delta
                endpoint: http://abc.live.dynatrace.com:443/api/v2/otlp/v1/metrics
                http:
                  headers:
                    Authorization: "Api-Token ${env.DYNATRACE_API_TOKEN}"
    """),
    "bad_requirement_level.yaml": ("DT011", """
        telemetry:
          instrumentation:
            instruments:
              default_requirement_level: all
    """),
    "reserved_instrument_name.yaml": ("DT012", """
        telemetry:
          instrumentation:
            instruments:
              router:
                http.server.custom.thing:
                  value: unit
                  type: counter
                  unit: "{request}"
                  description: "collides with the standard http.* namespace"
    """),
    "instrument_name_total.yaml": ("DT012", """
        telemetry:
          instrumentation:
            instruments:
              router:
                acme.router.requests_total:
                  value: unit
                  type: counter
                  unit: "{request}"
                  description: "has a _total suffix"
    """),
    "instrument_unit_in_name.yaml": ("DT012", """
        telemetry:
          instrumentation:
            instruments:
              router:
                acme.router.body.size.kb:
                  value: unit
                  type: histogram
                  unit: kb
                  description: "unit baked into the name"
    """),
    "collector_bad_protocol.yaml": ("DT101", """
        telemetry:
          exporters:
            metrics:
              otlp:
                enabled: true
                protocol: thrift
                temporality: delta
                endpoint: http://otel-collector:4318/v1/metrics
    """),
    "collector_inline_token.yaml": ("DT006", """
        telemetry:
          exporters:
            metrics:
              otlp:
                enabled: true
                protocol: http
                temporality: delta
                endpoint: http://otel-collector:4318/v1/metrics
                http:
                  headers:
                    Authorization: "Api-Token dt0c01.ABC.SECRET"
    """),
    "bad_instrument_type.yaml": ("DT013", """
        telemetry:
          instrumentation:
            instruments:
              router:
                acme.router.requests:
                  value: unit
                  type: gauge
                  unit: "{request}"
                  description: "gauge is not a supported instrument kind"
    """),
    # Safelisting with APQ still on is rejected at startup:
    # "apqs must be disabled to enable safelisting".
    "safelist_without_apq_off.yaml": ("DT030", """
        persisted_queries:
          enabled: true
          local_manifests:
            - ./persisted-query-manifest.json
          safelist:
            enabled: true
            require_id: true
    """),
}

# fixture name -> (rule that must fire as a warning, yaml)
WARN = {
    "coarse_default_buckets.yaml": ("DT026", """
        telemetry:
          exporters:
            metrics:
              otlp:
                enabled: true
                protocol: http
                temporality: delta
                endpoint: https://abc.live.dynatrace.com:443/api/v2/otlp/v1/metrics
                http:
                  headers:
                    Authorization: "Api-Token ${env.DYNATRACE_API_TOKEN}"
    """),
    "wide_bucket_gap.yaml": ("DT026", """
        telemetry:
          exporters:
            metrics:
              common:
                buckets: [0.1, 0.5, 1.0, 10.0]
              otlp:
                enabled: true
                protocol: http
                temporality: delta
                endpoint: https://abc.live.dynatrace.com:443/api/v2/otlp/v1/metrics
                http:
                  headers:
                    Authorization: "Api-Token ${env.DYNATRACE_API_TOKEN}"
    """),
    "aggressive_batch.yaml": ("DT014", """
        telemetry:
          exporters:
            metrics:
              otlp:
                enabled: true
                protocol: http
                temporality: delta
                endpoint: https://abc.live.dynatrace.com:443/api/v2/otlp/v1/metrics
                http:
                  headers:
                    Authorization: "Api-Token ${env.DYNATRACE_API_TOKEN}"
                batch_processor:
                  scheduled_delay: 100ms
    """),
    "full_sampler.yaml": ("DT015", """
        telemetry:
          exporters:
            tracing:
              common:
                sampler: 1.0
              otlp:
                enabled: true
                protocol: http
                endpoint: https://abc.live.dynatrace.com:443/api/v2/otlp/v1/traces
                http:
                  headers:
                    Authorization: "Api-Token ${env.DYNATRACE_API_TOKEN}"
            logging:
              stdout:
                enabled: true
                format:
                  json:
                    display_trace_id: open_telemetry
    """),
    "recommended_level.yaml": ("DT016", """
        telemetry:
          instrumentation:
            instruments:
              default_requirement_level: recommended
    """),
    "graphql_document_attribute.yaml": ("DT016", """
        telemetry:
          instrumentation:
            instruments:
              router:
                http.server.request.duration:
                  attributes:
                    graphql.document: true
    """),
    "parent_based_sampler_off.yaml": ("DT018", """
        telemetry:
          exporters:
            tracing:
              common:
                sampler: 0.1
                parent_based_sampler: false
              otlp:
                enabled: true
                protocol: grpc
                endpoint: http://otel-collector:4317
            logging:
              stdout:
                enabled: true
                format:
                  json:
                    display_trace_id: open_telemetry
    """),
    "spans_deprecated_mode.yaml": ("DT019", """
        telemetry:
          instrumentation:
            spans:
              mode: deprecated
    """),
    "spans_recommended_level.yaml": ("DT020", """
        telemetry:
          instrumentation:
            spans:
              mode: spec_compliant
              default_attribute_requirement_level: recommended
    """),
    "spans_without_error_marking.yaml": ("DT021", """
        telemetry:
          instrumentation:
            spans:
              mode: spec_compliant
              supergraph:
                attributes:
                  graphql.operation.name: true
              subgraph:
                attributes:
                  subgraph.name: true
    """),
    "collector_cumulative_metrics.yaml": ("DT102", """
        telemetry:
          exporters:
            metrics:
              otlp:
                enabled: true
                protocol: grpc
                endpoint: http://otel-collector:4317
    """),
    "collector_wrong_http_path.yaml": ("DT103", """
        telemetry:
          exporters:
            metrics:
              otlp:
                enabled: true
                protocol: http
                temporality: delta
                endpoint: http://otel-collector:4318/v1/traces
    """),
    "tracing_without_logging.yaml": ("DT017", """
        telemetry:
          exporters:
            tracing:
              common:
                sampler: 0.05
              otlp:
                enabled: true
                protocol: http
                endpoint: https://abc.live.dynatrace.com:443/api/v2/otlp/v1/traces
                http:
                  headers:
                    Authorization: "Api-Token ${env.DYNATRACE_API_TOKEN}"
    """),
    # Views match exact instrument names only; a wildcard silently matches
    # nothing, so the drop/rename this view was written for never happens.
    "wildcard_view.yaml": ("DT027", """
        telemetry:
          exporters:
            metrics:
              common:
                service_name: apollo-router
                buckets: [0.05, 0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.5, 5.0, 10.0, 20.0]
                views:
                  - name: apollo.router.*
                    aggregation:
                      histogram:
                        buckets: [0.1, 0.5, 1.0]
              otlp:
                enabled: true
                protocol: http
                temporality: delta
                endpoint: https://abc.live.dynatrace.com:443/api/v2/otlp/v1/metrics
                http:
                  headers:
                    Authorization: "Api-Token ${env.DYNATRACE_API_TOKEN}"
    """),
    # Without service_name the router reports as unknown_service:router and
    # every dashboard tile (all filter service.name) stays blank.
    "no_service_name.yaml": ("DT028", """
        telemetry:
          exporters:
            metrics:
              common:
                buckets: [0.05, 0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.5, 5.0, 10.0, 20.0]
              otlp:
                enabled: true
                protocol: http
                temporality: delta
                endpoint: https://abc.live.dynatrace.com:443/api/v2/otlp/v1/metrics
                http:
                  headers:
                    Authorization: "Api-Token ${env.DYNATRACE_API_TOKEN}"
    """),
    # One series per operation name walks into the OTel SDK's 2,000-datapoint
    # cardinality ceiling and Dynatrace's per-series billing.
    "operation_name_attribute.yaml": ("DT029", """
        telemetry:
          instrumentation:
            instruments:
              supergraph:
                acme.graphql.operations:
                  value: unit
                  type: counter
                  unit: "{operation}"
                  description: "operations by name"
                  attributes:
                    graphql.operation.name: true
    """),
}


def write(subdir: str, name: str, body: str):
    path = os.path.join(HERE, "fixtures", subdir)
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, name), "w") as fh:
        fh.write(textwrap.dedent(body).lstrip("\n"))


def main():
    for name, body in GOOD.items():
        write("good", name, body)
    for name, (_rule, body) in BAD.items():
        write("bad", name, body)
    for name, (_rule, body) in WARN.items():
        write("warn", name, body)
    total = len(GOOD) + len(BAD) + len(WARN)
    print(f"wrote {total} fixtures "
          f"({len(GOOD)} good, {len(BAD)} bad, {len(WARN)} warn)")


if __name__ == "__main__":
    main()
