#!/usr/bin/env python3
"""
Validate an Apollo Router config for Dynatrace telemetry export.

Two topologies are supported, because both are in real use:

  direct     router --OTLP/HTTP--> https://<env>.live.dynatrace.com/api/v2/otlp/...
  collector  router --OTLP--> OTel Collector --otlphttp--> Dynatrace

The mode is inferred per exporter from the endpoint host (a Dynatrace host means
direct, anything else means collector) and can be forced with --mode. Rules that
only make sense for one topology are not applied to the other -- a gRPC hop to a
local collector is correct, the same gRPC hop to Dynatrace never works.

Use scripts/validate_collector.py for the collector's own config.

Exit code 0 = no errors (warnings allowed), 1 = at least one error,
2 = usage/parse failure.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from urllib.parse import urlparse

try:
    import yaml
except ImportError:  # pragma: no cover
    print("PyYAML is required.\n"
          "  pip3 install pyyaml\n"
          "If that fails with 'externally-managed-environment' (Homebrew or\n"
          "Debian Python), use a virtualenv:\n"
          "  python3 -m venv .venv && .venv/bin/pip install pyyaml\n"
          "  .venv/bin/python <this script>", file=sys.stderr)
    sys.exit(2)

RULES = {
    # --- the router's own schema check (opt-in, needs the binary) ------------
    "DT000": "the router itself rejects this config (`router config validate`)",
    # --- direct-to-Dynatrace -------------------------------------------------
    "DT001": "otlp protocol must be `http` for Dynatrace (gRPC unsupported)",
    "DT002": "metrics otlp temporality must be `delta` (cumulative is dropped)",
    "DT003": "endpoint path must match the signal (/api/v2/otlp/v1/{metrics,traces,logs})",
    "DT004": "endpoint host must carry an explicit port",
    "DT005": "Authorization header must be present and start with `Api-Token `",
    "DT006": "the token must come from ${env.*} / ${file.*}, never be inlined",
    "DT007": "exporter must be explicitly `enabled: true`",
    "DT008": "endpoint must not be `default` / empty",
    "DT009": "a Dynatrace host with protocol grpc is always wrong",
    "DT010": "endpoint scheme must be https (http allowed only for loopback harness)",
    # --- instruments (topology independent) ----------------------------------
    "DT011": "instruments.default_requirement_level must be a valid value",
    "DT012": "custom instrument names must follow OTel conventions",
    "DT013": "custom instruments must declare value + a valid type",
    "DT014": "batch_processor values must be sane for a rate-limited SaaS endpoint",
    "DT015": "trace sampler of 1.0 is flagged (cost / rate limits)",
    "DT016": "graphql.document-style attributes flagged (PII + cardinality)",
    "DT017": "logs are not correlatable - stdout JSON without a trace id",
    "DT022": "telemetry.exporters.logging.otlp does not exist; the router won't start",
    "DT023": "OTLP ingest is on the .live. host; .apps. is the platform/UI host",
    "DT024": "condition operator is not one of the router's (eq/gt/lt/exists/all/any/not)",
    "DT025": "sandbox requires supergraph.introspection: true or the router won't start",
    "DT018": "parent_based_sampler: false breaks distributed traces behind a trusted caller",
    "DT019": "spans.mode should be spec_compliant (legacy naming groups badly in Dynatrace)",
    "DT020": "spans.default_attribute_requirement_level: recommended adds graphql.document",
    "DT021": "GraphQL errors are HTTP 200 - without otel.status_code they look successful",
    "DT026": "histogram buckets too coarse - percentiles above 500ms are guesses",
    "DT027": "a `views` entry with a wildcard in its name silently matches nothing",
    "DT028": "no service_name - the dashboard filters on service.name and shows nothing",
    "DT029": "operation-name attributes on metrics walk into the cardinality ceiling",
    "DT030": "persisted-query safelisting requires apq.enabled: false or the router won't start",
    # --- router -> collector -------------------------------------------------
    "DT101": "collector-bound otlp protocol must be `grpc` or `http`",
    "DT102": "collector-bound metrics need delta at the router or a "
             "cumulativetodelta processor in the collector",
    "DT103": "collector-bound OTLP/HTTP endpoint path must be /v1/{signal} if present",
}

SIGNAL_PATHS = {
    "metrics": "/api/v2/otlp/v1/metrics",
    "tracing": "/api/v2/otlp/v1/traces",
    "logging": "/api/v2/otlp/v1/logs",
}
COLLECTOR_SIGNAL_PATHS = {
    "metrics": "/v1/metrics",
    "tracing": "/v1/traces",
    "logging": "/v1/logs",
}
INLINE_TOKEN = re.compile(r"dt0c01\.")
STANDARD_PREFIXES = ("http.", "apollo.", "apollo_")
VALID_TYPES = {"counter", "histogram"}
VALID_LEVELS = {"required", "recommended", "none"}
# The router's condition operators. `gte`/`lte` look plausible and do not exist;
# the config is rejected at load, so this is a startup failure, not a data gap.
VALID_CONDITION_OPS = {"eq", "gt", "lt", "exists", "all", "any", "not"}
UNIT_IN_NAME = re.compile(r"[._](kb|mb|gb|ms|us|ns|bytes|seconds|secs|count)$")
ENV_REF = re.compile(r"\$\{(env|file)\.[^}]+\}")
HAS_EXPANSION = re.compile(r"\$\{[^}]+\}")


class Finding:
    def __init__(self, rule: str, level: str, path: str, message: str):
        self.rule, self.level, self.path, self.message = rule, level, path, message

    def as_dict(self):
        return {"rule": self.rule, "level": self.level, "path": self.path,
                "message": self.message,
                "docs": f"docs/rules.md#{self.rule.lower()}"}

    def __str__(self):
        tag = "ERROR" if self.level == "error" else "WARN "
        return f"{tag} {self.rule}  {self.path}: {self.message}"


class Validator:
    def __init__(self, cfg: dict, *, allow_loopback: bool = False,
                 mode: str = "auto"):
        self.cfg = cfg or {}
        self.allow_loopback = allow_loopback
        if mode not in ("auto", "direct", "collector"):
            raise ValueError(f"unknown mode {mode!r}")
        self.mode = mode
        self.findings: list[Finding] = []

    # -- helpers ----------------------------------------------------------
    def err(self, rule, path, msg):
        self.findings.append(Finding(rule, "error", path, msg))

    def warn(self, rule, path, msg):
        self.findings.append(Finding(rule, "warning", path, msg))

    @property
    def telemetry(self) -> dict:
        t = self.cfg.get("telemetry")
        return t if isinstance(t, dict) else {}

    @property
    def exporters(self) -> dict:
        e = self.telemetry.get("exporters")
        return e if isinstance(e, dict) else {}

    # -- rules ------------------------------------------------------------
    def run(self) -> list[Finding]:
        exporters = self.exporters
        configured = []
        for signal in ("metrics", "tracing"):
            block = exporters.get(signal)
            if not isinstance(block, dict):
                continue
            otlp = block.get("otlp")
            if not isinstance(otlp, dict):
                continue
            configured.append(signal)
            self._check_otlp(signal, otlp)
            self._check_batch(signal, otlp.get("batch_processor"))

        self._check_logging(configured)
        self._check_buckets(configured)
        self._check_views()
        self._check_sampler()
        self._check_instruments()
        self._check_spans()
        self._check_sandbox()
        self._check_persisted_queries()
        return self.findings

    def _check_views(self):
        """The documented lever for pruning metrics — `views` — matches exact
        instrument names only. Wildcards (`*`, `apollo.*`, regex) parse fine and
        then silently match nothing, so the drop or rename never takes effect
        and the operator believes the cardinality problem is handled."""
        metrics = self.exporters.get("metrics")
        common = metrics.get("common") if isinstance(metrics, dict) else None
        views = common.get("views") if isinstance(common, dict) else None
        if not isinstance(views, list):
            return
        for i, view in enumerate(views):
            if not isinstance(view, dict):
                continue
            name = view.get("name")
            if isinstance(name, str) and "*" in name:
                self.warn("DT027",
                          f"telemetry.exporters.metrics.common.views[{i}].name",
                          f"{name!r} contains a wildcard. Views match exact "
                          "instrument names only - a wildcard matches nothing, "
                          "silently, so this view is a no-op. Enumerate exact "
                          "metric names and verify each one took effect.")

    def _check_persisted_queries(self):
        """`persisted_queries.safelist.enabled: true` while APQ is still on is
        rejected at startup ("apqs must be disabled to enable safelisting"), so
        the router never starts and no telemetry is exported at all."""
        pq = self.cfg.get("persisted_queries")
        safelist = pq.get("safelist") if isinstance(pq, dict) else None
        if not (isinstance(safelist, dict) and safelist.get("enabled") is True):
            return
        apq = self.cfg.get("apq")
        apq_enabled = apq.get("enabled") if isinstance(apq, dict) else None
        if apq_enabled is not False:
            self.err("DT030", "persisted_queries.safelist.enabled",
                     "safelisting is on but apq.enabled is not false. The router "
                     "rejects this pair at startup ('apqs must be disabled to "
                     "enable safelisting'), so nothing runs and nothing is "
                     "exported. Add `apq: { enabled: false }`.")

    def _check_sandbox(self):
        """`sandbox.enabled: true` without `supergraph.introspection: true` is
        rejected at config load ("sandbox requires introspection"), so the router
        never starts and no telemetry is exported at all."""
        sandbox = self.cfg.get("sandbox")
        if not (isinstance(sandbox, dict) and sandbox.get("enabled") is True):
            return
        supergraph = self.cfg.get("supergraph")
        introspection = (supergraph.get("introspection")
                         if isinstance(supergraph, dict) else None)
        if introspection is not True:
            self.err("DT025", "sandbox.enabled",
                     "sandbox is enabled but supergraph.introspection is not "
                     "true. The router rejects this pair at startup, so nothing "
                     "is exported. Set supergraph.introspection: true, or turn "
                     "sandbox off outside local development.")

    def _check_condition(self, path: str, condition):
        """Recursively verify condition operators. An unknown operator such as
        `gte` fails schema validation and the router refuses to start."""
        if not isinstance(condition, dict):
            return
        for op, operand in condition.items():
            if op not in VALID_CONDITION_OPS:
                hint = ""
                if op == "gte":
                    hint = " Express >= N as `gt` with N-1."
                elif op == "lte":
                    hint = " Express <= N as `lt` with N+1."
                self.err("DT024", f"{path}.{op}",
                         f"{op!r} is not a router condition operator (valid: "
                         f"{sorted(VALID_CONDITION_OPS)}). The router will not "
                         f"start.{hint}")
                continue
            # all/any/not nest further conditions; comparisons hold selectors.
            if op in ("all", "any") and isinstance(operand, list):
                for i, sub in enumerate(operand):
                    self._check_condition(f"{path}.{op}[{i}]", sub)
            elif op == "not":
                self._check_condition(f"{path}.not", operand)

    def _check_spans(self):
        instr = self.telemetry.get("instrumentation")
        spans = instr.get("spans") if isinstance(instr, dict) else None
        if not isinstance(spans, dict):
            return
        base = "telemetry.instrumentation.spans"

        if spans.get("mode") == "deprecated":
            self.warn("DT019", f"{base}.mode",
                      "`deprecated` is the legacy Router v1 span shape; Dynatrace "
                      "groups those span names poorly in service/request analysis. "
                      "Use `spec_compliant`.")

        if spans.get("default_attribute_requirement_level") == "recommended":
            self.warn("DT020", f"{base}.default_attribute_requirement_level",
                      "`recommended` attaches OTel development-status attributes "
                      "including graphql.document. Defensible on sampled spans, "
                      "never on metrics - and not at all if operations can carry "
                      "PII.")

        # A GraphQL error is returned with HTTP 200. Unless the span is marked
        # ERROR, Dynatrace counts it as a success and failure rate stays flat.
        for stage in ("supergraph", "subgraph"):
            stage_cfg = spans.get(stage)
            attributes = stage_cfg.get("attributes") if isinstance(stage_cfg, dict) else None
            if not isinstance(attributes, dict):
                continue
            if "otel.status_code" not in attributes:
                self.warn("DT021", f"{base}.{stage}.attributes.otel.status_code",
                          "no error marking on this stage. GraphQL errors return "
                          "HTTP 200, so Dynatrace will record them as successful "
                          "requests. Set otel.status_code to ERROR conditioned on "
                          f"{'on_graphql_error' if stage == 'supergraph' else 'subgraph_on_graphql_error'}.")

    DEFAULT_BUCKETS = [0.001, 0.005, 0.015, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5,
                       1.0, 5.0, 10.0]

    def _check_buckets(self, configured: list[str]):
        """Percentiles are interpolated inside histogram buckets, so bucket width
        *is* the error bar. The router's default boundaries jump 0.5 -> 1.0 -> 5.0
        -> 10.0: a p95 anywhere in that range is interpolated inside a bucket up
        to 400% wide, which is why an OTel-derived p95 will not match GraphOS
        Studio (whose usage-reporting histogram is log-scale and ~10% granular)."""
        if "metrics" not in configured:
            return
        metrics = self.exporters.get("metrics")
        common = metrics.get("common") if isinstance(metrics, dict) else None
        common = common if isinstance(common, dict) else {}

        buckets = common.get("buckets")
        views = common.get("views")
        has_view_buckets = False
        if isinstance(views, list):
            for view in views:
                if not isinstance(view, dict):
                    continue
                agg = view.get("aggregation")
                if isinstance(agg, dict) and isinstance(agg.get("histogram"), dict) \
                        and agg["histogram"].get("buckets"):
                    has_view_buckets = True

        if not isinstance(buckets, list) and not has_view_buckets:
            self.warn("DT026", "telemetry.exporters.metrics.common.buckets",
                      "not set, so the router's coarse defaults apply "
                      "(0.5, 1.0, 5.0, 10.0 at the top end). A percentile that "
                      "lands between 1s and 5s is interpolated inside a bucket "
                      "400% wide and will not match GraphOS Studio's. See "
                      "templates/histogram-buckets.router.yaml.")
            return

        if isinstance(buckets, list) and buckets:
            numeric = [b for b in buckets if isinstance(b, (int, float))]
            widest = 0.0
            worst = None
            for low, high in zip(numeric, numeric[1:]):
                if low > 0 and (high / low) > widest:
                    widest, worst = high / low, (low, high)
            if widest >= 3 and worst:
                self.warn("DT026", "telemetry.exporters.metrics.common.buckets",
                          f"the {worst[0]}s -> {worst[1]}s boundary spans "
                          f"{widest:.0f}x. Any percentile falling there carries "
                          "that much interpolation error. Add boundaries so no "
                          "gap in your p95/p99 region exceeds ~1.5x.")
            if numeric and max(numeric) < 10:
                self.warn("DT026", "telemetry.exporters.metrics.common.buckets",
                          f"highest boundary is {max(numeric)}s. Requests slower "
                          "than that all land in +Inf, so timeouts are invisible. "
                          "Keep the top boundary at or above your request timeout.")

    def _check_logging(self, configured: list[str]):
        """The router has no OTLP log exporter. `telemetry.exporters.logging`
        accepts only `common` and `stdout` (additionalProperties: false), so an
        `otlp:` block there is a startup failure, not a Dynatrace problem. Logs
        reach Dynatrace by shipping the router's stdout — usually a collector
        `filelog` receiver — which only works if the lines carry a trace id."""
        logging_cfg = self.exporters.get("logging")

        # Deleting an `otlp:` block but leaving the `logging:` key behind yields
        # null, and the router rejects it ('null is not of type "object"'). The
        # key must be populated or removed outright.
        if "logging" in self.exporters and logging_cfg is None:
            self.err("DT022", "telemetry.exporters.logging",
                     "present but empty. The router rejects a null block "
                     "('null is not of type \"object\"') and will not start. "
                     "Give it a `stdout:` block or remove the key.")

        if isinstance(logging_cfg, dict) and "otlp" in logging_cfg:
            self.err("DT022", "telemetry.exporters.logging.otlp",
                     "the router has no OTLP log exporter. This key fails config "
                     "validation and the router will not start. Ship the router's "
                     "stdout instead (collector `filelog` receiver, or your "
                     "existing log forwarder) - see templates/dynatrace-logs.router.yaml.")

        stdout = logging_cfg.get("stdout") if isinstance(logging_cfg, dict) else None
        fmt = stdout.get("format") if isinstance(stdout, dict) else None
        json_fmt = fmt.get("json") if isinstance(fmt, dict) else None

        if "tracing" not in configured:
            return
        if not isinstance(json_fmt, dict):
            self.warn("DT017", "telemetry.exporters.logging.stdout.format.json",
                      "traces are exported but the router is not logging JSON with "
                      "trace ids, so nothing can correlate a log line to a span in "
                      "Dynatrace. Set format.json.display_trace_id: open_telemetry.")
        elif not json_fmt.get("display_trace_id"):
            self.warn("DT017",
                      "telemetry.exporters.logging.stdout.format.json.display_trace_id",
                      "missing. Without a trace id on each line, the Logs tab on a "
                      "Dynatrace trace stays empty. Use `open_telemetry`.")

    def _resolve_mode(self, endpoint: str) -> str:
        """direct = the router talks to Dynatrace; collector = it talks to an OTel
        Collector that forwards. Inferred unless forced with --mode.

        Endpoints are usually half variable (`${env.X}/api/v2/otlp/v1/metrics`,
        or just `${env.OTEL_COLLECTOR_GRPC}`), so the inference looks at three
        things in order of reliability: a literal Dynatrace host, the Dynatrace
        API path, then the variable names themselves.
        """
        if self.mode != "auto":
            return self.mode

        host = urlparse(HAS_EXPANSION.sub("https://placeholder:4318", endpoint)).hostname or ""
        if "dynatrace" in host.lower():
            return "direct"
        if "/api/v2/otlp" in endpoint:
            return "direct"

        var_names = " ".join(HAS_EXPANSION.findall(endpoint) or []) + " " + endpoint
        var_names = var_names.lower()
        if "dynatrace" in var_names or "dt_" in var_names:
            return "direct"
        # Anything else - a service name, an in-cluster host, a bare variable - is
        # a collector hop. Direct-to-Dynatrace always carries one of the markers
        # above, so this default cannot silently skip the strict rule set.
        return "collector"

    def _check_otlp(self, signal: str, otlp: dict):
        base = f"telemetry.exporters.{signal}.otlp"

        if otlp.get("enabled") is not True:
            self.err("DT007", f"{base}.enabled",
                     "must be explicitly `true`; the OTLP exporter defaults to false.")

        endpoint = otlp.get("endpoint")
        endpoint_s = endpoint if isinstance(endpoint, str) else ""

        if not endpoint_s or endpoint_s == "default":
            self.err("DT008", f"{base}.endpoint",
                     "must be an explicit URL, not `default`/empty.")
            return

        if self._resolve_mode(endpoint_s) == "collector":
            self._check_collector_hop(signal, base, otlp, endpoint_s)
        else:
            self._check_direct(signal, base, otlp, endpoint_s)

    # -- direct-to-Dynatrace ----------------------------------------------
    def _check_direct(self, signal: str, base: str, otlp: dict, endpoint_s: str):
        protocol = otlp.get("protocol")
        if protocol != "http":
            if protocol in (None, "grpc"):
                # The router defaults to grpc, so an omitted protocol is the same
                # mistake as writing it out.
                got = "grpc (the default)" if protocol is None else "grpc"
                self.err("DT009", f"{base}.protocol",
                         f"is {got}; Dynatrace's OTLP endpoint does not accept "
                         "gRPC. Set `protocol: http`.")
            else:
                self.err("DT001", f"{base}.protocol",
                         f"must be `http` for Dynatrace (got {protocol!r}).")

        if signal == "metrics" and otlp.get("temporality") != "delta":
            self.err("DT002", f"{base}.temporality",
                     f"must be `delta` (got {otlp.get('temporality')!r}). "
                     "Dynatrace does not support cumulative temporality - "
                     "counters are accepted with a 2xx and then dropped.")

        if signal == "metrics":
            self._check_service_name()

        self._check_endpoint(signal, f"{base}.endpoint", endpoint_s)
        self._check_auth(signal, base, otlp)

    def _check_service_name(self):
        """Without a service_name the router reports as OTel's fallback
        (`unknown_service:router`). Every tile in the generated dashboard — and
        every DQL query in the pack — filters `service.name == "apollo-router"`,
        so the data arrives and every chart stays blank anyway."""
        metrics = self.exporters.get("metrics")
        common = metrics.get("common") if isinstance(metrics, dict) else None
        common = common if isinstance(common, dict) else {}
        resource = common.get("resource")
        resource = resource if isinstance(resource, dict) else {}
        if not common.get("service_name") and "service.name" not in resource:
            self.warn("DT028", "telemetry.exporters.metrics.common.service_name",
                      "not set, so the router exports as `unknown_service:router`. "
                      "The generated dashboard and the DQL pack filter on "
                      "service.name == \"apollo-router\" - data will ingest and "
                      "every chart will stay blank. Set service_name (or "
                      "resource: {service.name: ...}) to match your dashboard "
                      "filter.")

    # -- router -> collector ----------------------------------------------
    def _check_collector_hop(self, signal: str, base: str, otlp: dict,
                             endpoint_s: str):
        """The hop to a collector has its own (looser) contract: gRPC is fine,
        no Api-Token, no https requirement in-cluster. What still matters is
        temporality, because whatever reaches Dynatrace must be delta."""
        protocol = otlp.get("protocol", "grpc")
        if protocol not in ("grpc", "http"):
            self.err("DT101", f"{base}.protocol",
                     f"must be `grpc` or `http` (got {protocol!r}).")

        if signal == "metrics" and otlp.get("temporality") != "delta":
            self.warn("DT102", f"{base}.temporality",
                      "not set to `delta`. Whatever the collector forwards to "
                      "Dynatrace must be delta, so either set it here or add a "
                      "`cumulativetodelta` processor to the collector's metrics "
                      "pipeline. Cumulative counters are silently dropped.")

        if protocol == "http":
            probe = HAS_EXPANSION.sub("https://placeholder:4318", endpoint_s)
            path = urlparse(probe).path.rstrip("/")
            expected = COLLECTOR_SIGNAL_PATHS[signal]
            if path and path != expected:
                self.warn("DT103", f"{base}.endpoint",
                          f"OTLP/HTTP to a collector posts to {expected}; the "
                          f"path {path!r} looks wrong. Leaving the path off "
                          "lets the exporter append the standard one.")

        http_cfg = otlp.get("http")
        headers = http_cfg.get("headers") if isinstance(http_cfg, dict) else {}
        for key, value in (headers or {}).items():
            if isinstance(value, str) and INLINE_TOKEN.search(value):
                self.err("DT006", f"{base}.http.headers.{key}",
                         "a literal Dynatrace token is inlined. Reference it "
                         "with `${env....}` instead - and note that on the "
                         "collector hop the token usually belongs in the "
                         "collector config, not here.")

    def _check_endpoint(self, signal: str, path: str, endpoint: str):
        # Strip variable expansions before parsing so ${env.X}/api/... still
        # yields a checkable path suffix.
        probe = HAS_EXPANSION.sub("https://placeholder.live.dynatrace.com:443", endpoint)
        parsed = urlparse(probe)

        expected = SIGNAL_PATHS[signal]
        if not parsed.path.endswith(expected):
            self.err("DT003", path,
                     f"must end with `{expected}` for the {signal} signal "
                     f"(got path {parsed.path!r}). A traces path on the metrics "
                     "exporter returns 200 and discards the payload.")

        host = parsed.hostname or ""

        # OTLP ingest is served by <env>.live.dynatrace.com. The .apps. host is the
        # platform/UI host: it answers, so this looks like a working endpoint, but
        # ingest 404s (Api-Token) or 403s (Bearer) and no data is ever stored.
        if host.endswith(".apps.dynatrace.com"):
            self.err("DT023", path,
                     f"{host} is the Dynatrace platform/UI host, which does not "
                     "serve OTLP ingest - it returns 404/403 and stores nothing. "
                     "Use the .live. host, e.g. "
                     f"{host.replace('.apps.', '.live.')}:443{SIGNAL_PATHS[signal]}")

        is_loopback = host in ("127.0.0.1", "localhost", "::1", "host.docker.internal")
        if parsed.scheme != "https" and not (self.allow_loopback and is_loopback):
            self.err("DT010", path, f"scheme must be https (got {parsed.scheme!r}).")

        # Explicit port required unless the whole host came from a variable.
        host_from_var = bool(HAS_EXPANSION.search(endpoint)) and endpoint.strip().startswith("${")
        if parsed.port is None and not host_from_var:
            self.err("DT004", path,
                     "endpoint has no explicit port. Apollo requires `:443` "
                     "(or the ActiveGate port) when the host omits it, e.g. "
                     "https://abc12345.live.dynatrace.com:443" + SIGNAL_PATHS[signal])

    def _check_auth(self, signal: str, base: str, otlp: dict):
        http_cfg = otlp.get("http")
        headers = http_cfg.get("headers") if isinstance(http_cfg, dict) else None
        path = f"{base}.http.headers.Authorization"

        if not isinstance(headers, dict):
            self.err("DT005", f"{base}.http.headers",
                     "missing. Dynatrace OTLP ingest requires an Authorization header.")
            return

        auth = None
        for key, value in headers.items():
            if isinstance(key, str) and key.lower() == "authorization":
                auth = value
                break

        if not isinstance(auth, str) or not auth:
            self.err("DT005", path, "missing Authorization header.")
            return

        if not auth.startswith("Api-Token "):
            self.err("DT005", path,
                     f"must start with `Api-Token ` (got {auth[:24]!r}...). "
                     "`Bearer` is rejected by Dynatrace OTLP ingest.")
            token = auth
        else:
            token = auth[len("Api-Token "):].strip()

        if token and not ENV_REF.search(token):
            self.err("DT006", path,
                     "token appears to be inlined. Reference it with "
                     "`${env.DYNATRACE_API_TOKEN}` or `${file....}` so the config "
                     "is safe to commit.")

    def _check_batch(self, signal: str, batch):
        if not isinstance(batch, dict):
            return
        base = f"telemetry.exporters.{signal}.otlp.batch_processor"
        delay = str(batch.get("scheduled_delay", "")).strip()
        m = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s)", delay)
        if m:
            millis = float(m.group(1)) * (1 if m.group(2) == "ms" else 1000)
            if millis < 1000:
                self.warn("DT014", f"{base}.scheduled_delay",
                          f"{delay} is aggressive for a SaaS ingest endpoint; "
                          "Dynatrace rate limits per-token. 5s is the documented "
                          "default and is usually right outside of local testing.")
        size = batch.get("max_export_batch_size")
        if isinstance(size, int) and size > 2048:
            self.warn("DT014", f"{base}.max_export_batch_size",
                      f"{size} risks exceeding the OTLP HTTP message size limit; "
                      "keep it at or below 2048.")

    def _check_sampler(self):
        tracing = self.exporters.get("tracing")
        common = tracing.get("common") if isinstance(tracing, dict) else None
        if not isinstance(common, dict):
            return
        if common.get("sampler") in (1, 1.0, "always_on"):
            self.warn("DT015", "telemetry.exporters.tracing.common.sampler",
                      "sampling every request will rate-limit you on Dynatrace and "
                      "inflate DDU spend. Use a fraction (0.01-0.1) in production.")
        if common.get("parent_based_sampler") is False:
            self.warn("DT018", "telemetry.exporters.tracing.common.parent_based_sampler",
                      "the router will ignore an upstream sampling decision, which "
                      "splits distributed traces when something in front of it "
                      "(OneAgent, a gateway, another service) already sampled. "
                      "Set true unless the router is the trace origin.")

    def _check_instruments(self):
        instr = self.telemetry.get("instrumentation")
        instr = instr.get("instruments") if isinstance(instr, dict) else None
        if not isinstance(instr, dict):
            return

        level = instr.get("default_requirement_level")
        if level is not None and level not in VALID_LEVELS:
            self.err("DT011",
                     "telemetry.instrumentation.instruments.default_requirement_level",
                     f"{level!r} is not one of {sorted(VALID_LEVELS)}.")
        if level == "recommended":
            self.warn("DT016",
                      "telemetry.instrumentation.instruments.default_requirement_level",
                      "`recommended` attaches development-status GraphQL attributes "
                      "such as graphql.document - high cardinality and may contain "
                      "PII. Apollo recommends `required`.")

        for service, body in instr.items():
            if service == "default_requirement_level" or not isinstance(body, dict):
                continue
            for name, spec in body.items():
                path = f"telemetry.instrumentation.instruments.{service}.{name}"
                self._check_attributes(path, spec)
                if isinstance(spec, dict) and "condition" in spec:
                    self._check_condition(f"{path}.condition", spec["condition"])
                if not isinstance(spec, dict) or "value" not in spec:
                    continue  # standard instrument toggle / attribute-only override
                self._check_custom_instrument(service, name, spec)

    def _check_custom_instrument(self, service: str, name: str, spec: dict):
        path = f"telemetry.instrumentation.instruments.{service}.{name}"
        if name.startswith(STANDARD_PREFIXES):
            self.err("DT012", path,
                     f"{name!r} is a reserved standard-instrument namespace; "
                     "custom instruments must use their own prefix.")
        if name.endswith("_total"):
            self.err("DT012", path,
                     "drop the `_total` suffix - OTel adds it at export time.")
        if UNIT_IN_NAME.search(name):
            self.err("DT012", path,
                     "the unit belongs in `unit:`, not the instrument name.")
        if "." not in name:
            self.warn("DT012", path,
                      "use dot notation to namespace the instrument "
                      "(e.g. `acme.router.requests`).")
        elif "_" in name.split(".")[-1]:
            self.warn("DT012", path,
                      "prefer dots over underscores between namespace segments.")

        itype = spec.get("type")
        if itype not in VALID_TYPES:
            self.err("DT013", f"{path}.type",
                     f"must be one of {sorted(VALID_TYPES)} (got {itype!r}).")
        if not spec.get("description"):
            self.warn("DT013", f"{path}.description",
                      "add a description - it becomes the metric description in "
                      "Dynatrace and is what the next on-call reads.")
        if not spec.get("unit"):
            self.warn("DT013", f"{path}.unit", "add a unit.")

    def _check_attributes(self, path: str, spec):
        if not isinstance(spec, dict):
            return
        attrs = spec.get("attributes")
        if not isinstance(attrs, dict):
            return
        for attr, enabled in attrs.items():
            # An attribute can be a dict carrying its own condition, e.g.
            # otel.status_code: {static: ERROR, condition: {eq: [...]}}.
            if isinstance(enabled, dict) and "condition" in enabled:
                self._check_condition(f"{path}.attributes.{attr}.condition",
                                      enabled["condition"])
            if enabled is True and attr in ("graphql.document",
                                            "subgraph.graphql.document"):
                self.warn("DT016", f"{path}.attributes.{attr}",
                          "full documents as a metric attribute create unbounded "
                          "cardinality and may carry PII. Keep this off.")
            if enabled is True and attr in ("graphql.operation.name",
                                            "subgraph.graphql.operation.name"):
                self.warn("DT029", f"{path}.attributes.{attr}",
                          "one series per operation name. The OTel SDK hard-caps "
                          "a metric stream at 2,000 datapoints and silently "
                          "strips attributes past it (otel.metric.overflow), and "
                          "Dynatrace bills per ingested series. Defensible on a "
                          "small graph; on a busy one, keep it off and use spans "
                          "for per-operation analysis.")


def validate_file(filename: str, *, allow_loopback: bool = False,
                  mode: str = "auto"):
    with open(filename) as fh:
        cfg = yaml.safe_load(fh)
    if not isinstance(cfg, dict):
        raise ValueError(f"{filename}: top level YAML is not a mapping")
    return Validator(cfg, allow_loopback=allow_loopback, mode=mode).run()


def router_precheck(router_bin: str, filename: str) -> Finding | None:
    """DT000: hand the file to the router's own validator, which knows the full
    config schema this script cannot replicate (a real template bug — an invalid
    attribute on a scoped instrument — once passed every DT rule here and was
    caught only by `router config validate`). Requires the binary, and any
    ${env.*} the config references must be set or expansion fails first."""
    import subprocess
    try:
        proc = subprocess.run([router_bin, "config", "validate", filename],
                              capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        raise SystemExit(f"--router-bin: {router_bin!r} not found or not executable")
    except subprocess.TimeoutExpired:
        return Finding("DT000", "error", filename,
                       f"`{router_bin} config validate` timed out after 120s.")
    if proc.returncode == 0:
        return None
    detail = (proc.stderr or proc.stdout or "").strip()
    # The router logs a JSON WARN line before the human-readable error; keep the
    # readable part and cap the size so one finding stays one finding.
    lines = [l for l in detail.splitlines() if not l.startswith('{"timestamp"')]
    detail = " / ".join(l.strip() for l in lines if l.strip())[:600]
    return Finding("DT000", "error", filename,
                   f"the router itself rejects this config: {detail}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("configs", nargs="+", help="router.yaml file(s) to validate")
    ap.add_argument("--allow-loopback", action="store_true",
                    help="permit http:// loopback endpoints (harness mode)")
    ap.add_argument("--mode", choices=("auto", "direct", "collector"), default="auto",
                    help="topology: direct to Dynatrace, via an OTel Collector, "
                         "or inferred per exporter from the endpoint host (default)")
    ap.add_argument("--strict", action="store_true", help="treat warnings as errors")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--router-bin", metavar="PATH",
                    help="also run `PATH config validate` on each file (DT000). "
                         "This is the router's own schema check and catches "
                         "whole classes of mistake these rules cannot; any "
                         "${env.*} the config references must be set.")
    args = ap.parse_args(argv)

    report, exit_code = {}, 0
    for filename in args.configs:
        try:
            findings = validate_file(filename, allow_loopback=args.allow_loopback,
                                     mode=args.mode)
        except Exception as exc:  # noqa: BLE001 - surface parse errors plainly
            print(f"ERROR parse  {filename}: {exc}", file=sys.stderr)
            exit_code = 2
            continue

        if args.router_bin:
            precheck = router_precheck(args.router_bin, filename)
            if precheck:
                findings.insert(0, precheck)

        report[filename] = [f.as_dict() for f in findings]
        errors = [f for f in findings if f.level == "error"]
        warnings = [f for f in findings if f.level == "warning"]
        if errors or (warnings and args.strict):
            exit_code = max(exit_code, 1)

        if not args.json:
            print(f"\n=== {filename}")
            for finding in findings:
                print(f"  {finding}")
            if errors:
                verdict = "FAIL"
            elif warnings and args.strict:
                verdict = "FAIL (strict)"
            else:
                verdict = "PASS"
            print(f"  {verdict}: {len(errors)} error(s), {len(warnings)} warning(s)")

    if args.json:
        print(json.dumps(report, indent=2))
    elif not args.router_bin:
        print("\nnote: the router's own schema check was not run. These rules "
              "cover the Dynatrace contract, not the router's full config "
              "schema - pass --router-bin ./router to catch configs the router "
              "itself rejects (DT000).")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
