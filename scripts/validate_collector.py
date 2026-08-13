#!/usr/bin/env python3
"""
Validate an OpenTelemetry Collector config that exports to Dynatrace.

The collector topology (router -> collector -> Dynatrace) moves the Dynatrace
specifics out of the router and into the collector, which means the failure modes
move too. These are the ones that produce a 2xx or a 404 and no data:

  DTC001  the Dynatrace exporter must be `otlphttp` (no gRPC ingest)
  DTC002  endpoint must NOT include a /v1/<signal> suffix (the exporter appends it)
  DTC003  Authorization must be `Api-Token <token>` from an env reference
  DTC004  a metrics pipeline fed by a cumulative source needs `cumulativetodelta`
  DTC005  the Dynatrace exporter must be wired into at least one pipeline
  DTC006  every pipeline should batch (unbatched exports hit payload limits)
  DTC007  log pipelines should trim request/response bodies (volume + PII)
  DTC008  endpoint should be https
  DTC009  retry/queue must not be disabled on the Dynatrace exporter (429s drop data)

Exit code 0 = no errors, 1 = errors (or warnings with --strict), 2 = bad input.
"""
from __future__ import annotations

import argparse
import json
import os
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from validate_dynatrace import Finding  # noqa: E402  (shared output shape)

RULES = {
    "DTC001": "the Dynatrace exporter must be `otlphttp` (no gRPC ingest)",
    "DTC002": "endpoint must not include a /v1/<signal> suffix",
    "DTC003": "Authorization must be `Api-Token <token>` from an env reference",
    "DTC004": "a metrics pipeline fed by a cumulative source needs cumulativetodelta",
    "DTC005": "the Dynatrace exporter must be wired into at least one pipeline",
    "DTC006": "every pipeline should batch",
    "DTC007": "log pipelines should trim request/response bodies",
    "DTC008": "endpoint should be https",
    "DTC009": "retry/queue must not be disabled on the Dynatrace exporter",
}

SIGNAL_SUFFIX = re.compile(r"/v1/(traces|metrics|logs)/?$")
ENV_REF = re.compile(r"\$\{env:[^}]+\}")
INLINE_TOKEN = re.compile(r"dt0c01\.")
# Receivers that emit cumulative counters and therefore need conversion.
CUMULATIVE_RECEIVERS = ("prometheus", "prometheus_simple", "hostmetrics")


class CollectorValidator:
    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        self.findings: list[Finding] = []

    def err(self, rule, path, msg):
        self.findings.append(Finding(rule, "error", path, msg))

    def warn(self, rule, path, msg):
        self.findings.append(Finding(rule, "warning", path, msg))

    # -- helpers ----------------------------------------------------------
    def _section(self, name) -> dict:
        value = self.cfg.get(name)
        return value if isinstance(value, dict) else {}

    def _dynatrace_exporters(self) -> dict:
        """Exporter entries whose endpoint or auth header points at Dynatrace."""
        found = {}
        for name, spec in self._section("exporters").items():
            if not isinstance(spec, dict):
                continue
            blob = json.dumps(spec)
            if ("dynatrace" in name.lower() or "dynatrace" in blob
                    or "Api-Token" in blob or "DT_OTLP_ENDPOINT" in blob):
                found[name] = spec
        return found

    def _pipelines(self) -> dict:
        pipelines = self._section("service").get("pipelines")
        return pipelines if isinstance(pipelines, dict) else {}

    # -- rules ------------------------------------------------------------
    def run(self) -> list[Finding]:
        exporters = self._dynatrace_exporters()
        if not exporters:
            self.warn("DTC005", "exporters",
                      "no Dynatrace exporter found. Expected an `otlphttp/...` "
                      "entry with an `Authorization: Api-Token ...` header.")
            return self.findings

        for name, spec in exporters.items():
            self._check_exporter(name, spec)

        used = {
            exporter
            for pipeline in self._pipelines().values()
            if isinstance(pipeline, dict)
            for exporter in (pipeline.get("exporters") or [])
        }
        for name in exporters:
            if name not in used:
                self.err("DTC005", f"exporters.{name}",
                         "defined but not referenced by any pipeline, so nothing "
                         "is exported. Add it to service.pipelines.<signal>.exporters.")

        self._check_pipelines(set(exporters))
        return self.findings

    def _check_exporter(self, name: str, spec: dict):
        base = f"exporters.{name}"

        if not name.split("/")[0] == "otlphttp":
            self.err("DTC001", base,
                     f"{name!r} exports to Dynatrace but is not an `otlphttp` "
                     "exporter. Dynatrace's OTLP ingest is HTTP/protobuf only - "
                     "the `otlp` exporter defaults to gRPC and will fail.")

        endpoint = spec.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint:
            self.err("DTC002", f"{base}.endpoint", "missing.")
        else:
            if SIGNAL_SUFFIX.search(endpoint):
                self.err("DTC002", f"{base}.endpoint",
                         f"remove the signal suffix from {endpoint!r}. The "
                         "exporter appends /v1/traces, /v1/metrics or /v1/logs "
                         "itself; leaving it in produces 404s.")
            probe = re.sub(r"\$\{[^}]+\}", "https://placeholder.live.dynatrace.com", endpoint)
            if urlparse(probe).scheme not in ("https", ""):
                self.warn("DTC008", f"{base}.endpoint",
                          "should be https for a Dynatrace tenant.")

        # Dynatrace rate-limits per token (429 with Retry-After). The exporter
        # helper's retry and queue are on by default and honor it; explicitly
        # disabling either turns every throttled batch into dropped data.
        for knob in ("retry_on_failure", "sending_queue"):
            block = spec.get(knob)
            if isinstance(block, dict) and block.get("enabled") is False:
                self.warn("DTC009", f"{base}.{knob}.enabled",
                          f"{knob} is explicitly disabled. Dynatrace rate-limits "
                          "per token; without it a 429 means the batch is "
                          "dropped, not retried. The default is enabled - "
                          "remove `enabled: false` unless data loss under "
                          "throttling is acceptable.")

        headers = spec.get("headers")
        headers = headers if isinstance(headers, dict) else {}
        auth = next((v for k, v in headers.items()
                     if isinstance(k, str) and k.lower() == "authorization"), None)
        if not isinstance(auth, str) or not auth:
            self.err("DTC003", f"{base}.headers.Authorization", "missing.")
            return
        if not auth.startswith("Api-Token "):
            self.err("DTC003", f"{base}.headers.Authorization",
                     f"must start with `Api-Token ` (got {auth[:20]!r}...).")
        if INLINE_TOKEN.search(auth):
            self.err("DTC003", f"{base}.headers.Authorization",
                     "a literal token is inlined. Use `${env:DT_API_TOKEN}`.")
        elif not ENV_REF.search(auth):
            self.warn("DTC003", f"{base}.headers.Authorization",
                      "prefer an env reference such as `${env:DT_API_TOKEN}`.")

    def _check_pipelines(self, dynatrace_exporters: set):
        processors_defined = set(self._section("processors").keys())

        for signal, pipeline in self._pipelines().items():
            if not isinstance(pipeline, dict):
                continue
            exporters = set(pipeline.get("exporters") or [])
            if not exporters & dynatrace_exporters:
                continue  # this pipeline doesn't reach Dynatrace

            base = f"service.pipelines.{signal}"
            processors = list(pipeline.get("processors") or [])
            receivers = list(pipeline.get("receivers") or [])
            families = {p.split("/")[0] for p in processors}

            if signal.startswith("metrics"):
                cumulative = [r for r in receivers
                              if r.split("/")[0] in CUMULATIVE_RECEIVERS]
                if cumulative and "cumulativetodelta" not in families:
                    self.err("DTC004", f"{base}.processors",
                             f"receiver(s) {cumulative} emit cumulative metrics and "
                             "Dynatrace only accepts delta. Add a "
                             "`cumulativetodelta` processor or the counters will be "
                             "accepted with a 2xx and silently dropped.")
                # An OTLP receiver can carry either temporality — the collector
                # config alone can't tell. Flag it so the sender gets checked.
                otlp_fed = [r for r in receivers if r.split("/")[0] == "otlp"]
                if otlp_fed and "cumulativetodelta" not in families:
                    self.warn("DTC004", f"{base}.processors",
                              "this pipeline forwards OTLP metrics to Dynatrace "
                              "with no `cumulativetodelta`. That is correct only if "
                              "the sender already exports delta (the router needs "
                              "`temporality: delta`). If the sender uses the OTel "
                              "default (cumulative), add the processor or the "
                              "counters are accepted with a 2xx and dropped.")
                if "cumulativetodelta" in families and \
                        "cumulativetodelta" not in processors_defined and \
                        not any(p.startswith("cumulativetodelta") for p in processors_defined):
                    self.err("DTC004", "processors",
                             "the pipeline references `cumulativetodelta` but no "
                             "such processor is defined.")

            if "batch" not in families:
                self.warn("DTC006", f"{base}.processors",
                          "no batch processor. Unbatched exports are per-item HTTP "
                          "calls and can exceed Dynatrace's payload/rate limits.")

            if signal.startswith("logs") and "attributes" not in families:
                self.warn("DTC007", f"{base}.processors",
                          "no `attributes` processor. Router log records carry full "
                          "request/response bodies and headers when events are "
                          "enabled - drop them before shipping to Dynatrace "
                          "(volume, and possible PII).")

            for processor in processors:
                if processor not in processors_defined:
                    self.err("DTC006", f"{base}.processors",
                             f"references undefined processor {processor!r}.")


def validate_file(filename: str):
    with open(filename) as fh:
        cfg = yaml.safe_load(fh)
    if not isinstance(cfg, dict):
        raise ValueError(f"{filename}: top level YAML is not a mapping")
    return CollectorValidator(cfg).run()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("configs", nargs="+", help="collector config file(s)")
    ap.add_argument("--strict", action="store_true", help="treat warnings as errors")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    report, exit_code = {}, 0
    for filename in args.configs:
        try:
            findings = validate_file(filename)
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR parse  {filename}: {exc}", file=sys.stderr)
            exit_code = 2
            continue

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
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
