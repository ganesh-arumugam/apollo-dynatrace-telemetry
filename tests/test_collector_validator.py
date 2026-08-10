#!/usr/bin/env python3
"""Tests for the collector-config rule set (DTC###).

The collector topology is what most real deployments run, so these rules are
checked against synthetic fixtures and — optionally — against your own known-good
production configs. A rule that fires on a config that works in production is a
false positive, and that is worse than a missing rule.
"""
from __future__ import annotations

import os
import sys
import textwrap
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import yaml  # noqa: E402
from validate_collector import RULES, CollectorValidator, validate_file  # noqa: E402

# Optional false-positive guard against your own known-good collector configs:
#   DT_REAL_COLLECTOR_CONFIGS=/path/a.yml:/path/b.yml
# Skipped entirely when unset, so the suite is self-contained by default.
REAL_CONFIGS = [p for p in
                os.environ.get("DT_REAL_COLLECTOR_CONFIGS", "").split(":") if p]


def findings_for(yaml_text: str):
    return CollectorValidator(yaml.safe_load(textwrap.dedent(yaml_text))).run()


def rules(findings, level=None):
    return {f.rule for f in findings if level is None or f.level == level}


BASE_EXPORTER = """
    exporters:
      otlphttp/dynatrace:
        endpoint: ${env:DT_OTLP_ENDPOINT}
        headers:
          Authorization: "Api-Token ${env:DT_API_TOKEN}"
"""


class TestCollectorRules(unittest.TestCase):
    def test_clean_config_passes(self):
        findings = findings_for("""
            receivers:
              otlp:
                protocols:
                  grpc:
                    endpoint: 0.0.0.0:4317
            processors:
              batch: {}
            exporters:
              otlphttp/dynatrace:
                endpoint: ${env:DT_OTLP_ENDPOINT}
                headers:
                  Authorization: "Api-Token ${env:DT_API_TOKEN}"
            service:
              pipelines:
                traces:
                  receivers: [otlp]
                  processors: [batch]
                  exporters: [otlphttp/dynatrace]
        """)
        self.assertEqual([str(f) for f in findings], [])

    def test_grpc_exporter_to_dynatrace_is_an_error(self):
        findings = findings_for("""
            processors:
              batch: {}
            exporters:
              otlp/dynatrace:
                endpoint: ${env:DT_OTLP_ENDPOINT}
                headers:
                  Authorization: "Api-Token ${env:DT_API_TOKEN}"
            service:
              pipelines:
                traces:
                  receivers: [otlp]
                  processors: [batch]
                  exporters: [otlp/dynatrace]
        """)
        self.assertIn("DTC001", rules(findings, "error"))

    def test_signal_suffix_on_endpoint_is_an_error(self):
        """The exporter appends /v1/traces itself; leaving it on gives 404s."""
        for suffix in ("/v1/traces", "/v1/metrics", "/v1/logs/"):
            with self.subTest(suffix=suffix):
                findings = findings_for(f"""
                    processors:
                      batch: {{}}
                    exporters:
                      otlphttp/dynatrace:
                        endpoint: https://abc.live.dynatrace.com/api/v2/otlp{suffix}
                        headers:
                          Authorization: "Api-Token ${{env:DT_API_TOKEN}}"
                    service:
                      pipelines:
                        traces:
                          receivers: [otlp]
                          processors: [batch]
                          exporters: [otlphttp/dynatrace]
                """)
                self.assertIn("DTC002", rules(findings, "error"))

    def test_bearer_and_inline_token_are_errors(self):
        bearer = findings_for("""
            processors:
              batch: {}
            exporters:
              otlphttp/dynatrace:
                endpoint: ${env:DT_OTLP_ENDPOINT}
                headers:
                  Authorization: "Bearer ${env:DT_API_TOKEN}"
            service:
              pipelines:
                traces: {receivers: [otlp], processors: [batch], exporters: [otlphttp/dynatrace]}
        """)
        self.assertIn("DTC003", rules(bearer, "error"))

        inline = findings_for("""
            processors:
              batch: {}
            exporters:
              otlphttp/dynatrace:
                endpoint: https://abc.live.dynatrace.com/api/v2/otlp
                headers:
                  Authorization: "Api-Token dt0c01.REAL.SECRET"
            service:
              pipelines:
                traces: {receivers: [otlp], processors: [batch], exporters: [otlphttp/dynatrace]}
        """)
        self.assertIn("DTC003", rules(inline, "error"))

    def test_prometheus_metrics_without_cumulativetodelta_is_an_error(self):
        findings = findings_for("""
            receivers:
              prometheus:
                config: {}
            processors:
              batch: {}
            exporters:
              otlphttp/dynatrace:
                endpoint: ${env:DT_OTLP_ENDPOINT}
                headers:
                  Authorization: "Api-Token ${env:DT_API_TOKEN}"
            service:
              pipelines:
                metrics:
                  receivers: [prometheus]
                  processors: [batch]
                  exporters: [otlphttp/dynatrace]
        """)
        self.assertIn("DTC004", rules(findings, "error"))

    def test_cumulativetodelta_satisfies_the_metrics_rule(self):
        findings = findings_for("""
            receivers:
              prometheus:
                config: {}
            processors:
              batch: {}
              cumulativetodelta: {}
            exporters:
              otlphttp/dynatrace:
                endpoint: ${env:DT_OTLP_ENDPOINT}
                headers:
                  Authorization: "Api-Token ${env:DT_API_TOKEN}"
            service:
              pipelines:
                metrics:
                  receivers: [prometheus]
                  processors: [cumulativetodelta, batch]
                  exporters: [otlphttp/dynatrace]
        """)
        self.assertNotIn("DTC004", rules(findings, "error"))

    def test_otlp_receiver_metrics_does_not_require_conversion(self):
        """Delta comes from the router in this case, so no processor is needed."""
        findings = findings_for("""
            receivers:
              otlp:
                protocols: {grpc: {endpoint: 0.0.0.0:4317}}
            processors:
              batch: {}
            exporters:
              otlphttp/dynatrace:
                endpoint: ${env:DT_OTLP_ENDPOINT}
                headers:
                  Authorization: "Api-Token ${env:DT_API_TOKEN}"
            service:
              pipelines:
                metrics:
                  receivers: [otlp]
                  processors: [batch]
                  exporters: [otlphttp/dynatrace]
        """)
        self.assertNotIn("DTC004", rules(findings, "error"))

    def test_exporter_not_wired_into_a_pipeline_is_an_error(self):
        findings = findings_for("""
            processors:
              batch: {}
            exporters:
              otlphttp/dynatrace:
                endpoint: ${env:DT_OTLP_ENDPOINT}
                headers:
                  Authorization: "Api-Token ${env:DT_API_TOKEN}"
            service:
              pipelines:
                traces:
                  receivers: [otlp]
                  processors: [batch]
                  exporters: [otlp/jaeger]
        """)
        self.assertIn("DTC005", rules(findings, "error"))

    def test_undefined_processor_reference_is_an_error(self):
        findings = findings_for("""
            processors:
              batch: {}
            exporters:
              otlphttp/dynatrace:
                endpoint: ${env:DT_OTLP_ENDPOINT}
                headers:
                  Authorization: "Api-Token ${env:DT_API_TOKEN}"
            service:
              pipelines:
                traces:
                  receivers: [otlp]
                  processors: [batch, resource/nope]
                  exporters: [otlphttp/dynatrace]
        """)
        self.assertIn("DTC006", rules(findings, "error"))

    def test_missing_batch_warns(self):
        findings = findings_for("""
            processors: {}
            exporters:
              otlphttp/dynatrace:
                endpoint: ${env:DT_OTLP_ENDPOINT}
                headers:
                  Authorization: "Api-Token ${env:DT_API_TOKEN}"
            service:
              pipelines:
                traces:
                  receivers: [otlp]
                  exporters: [otlphttp/dynatrace]
        """)
        self.assertIn("DTC006", rules(findings, "warning"))

    def test_log_pipeline_without_trim_warns(self):
        findings = findings_for("""
            processors:
              batch: {}
            exporters:
              otlphttp/dynatrace:
                endpoint: ${env:DT_OTLP_ENDPOINT}
                headers:
                  Authorization: "Api-Token ${env:DT_API_TOKEN}"
            service:
              pipelines:
                logs:
                  receivers: [filelog]
                  processors: [batch]
                  exporters: [otlphttp/dynatrace]
        """)
        self.assertIn("DTC007", rules(findings, "warning"))

    def test_plain_http_endpoint_warns(self):
        findings = findings_for("""
            processors:
              batch: {}
            exporters:
              otlphttp/dynatrace:
                endpoint: http://abc.live.dynatrace.com/api/v2/otlp
                headers:
                  Authorization: "Api-Token ${env:DT_API_TOKEN}"
            service:
              pipelines:
                traces: {receivers: [otlp], processors: [batch], exporters: [otlphttp/dynatrace]}
        """)
        self.assertIn("DTC008", rules(findings, "warning"))

    def test_no_dynatrace_exporter_warns_rather_than_crashing(self):
        findings = findings_for("""
            exporters:
              otlp/jaeger:
                endpoint: jaeger:4317
            service:
              pipelines:
                traces: {receivers: [otlp], exporters: [otlp/jaeger]}
        """)
        self.assertIn("DTC005", rules(findings, "warning"))

    def test_every_rule_is_exercised_and_documented(self):
        with open(__file__) as fh:
            tests_body = fh.read()
        with open(os.path.join(ROOT, "README.md")) as fh:
            readme = fh.read()
        for rule in RULES:
            with self.subTest(rule=rule):
                self.assertIn(rule, tests_body, "rule has no test")
                self.assertIn(rule, readme, "rule is not documented")


class TestShippedCollectorTemplate(unittest.TestCase):
    def test_template_is_clean(self):
        path = os.path.join(ROOT, "templates", "collector",
                            "otel-collector-dynatrace.yml")
        findings = validate_file(path)
        self.assertEqual([str(f) for f in findings], [])

    def test_template_has_a_metrics_pipeline_with_conversion(self):
        """The whole point of this template vs. the ones it came from."""
        path = os.path.join(ROOT, "templates", "collector",
                            "otel-collector-dynatrace.yml")
        with open(path) as fh:
            cfg = yaml.safe_load(fh)
        metrics = cfg["service"]["pipelines"]["metrics"]
        self.assertIn("cumulativetodelta", metrics["processors"])
        self.assertIn("otlphttp/dynatrace", metrics["exporters"])


class TestRealWorldConfigs(unittest.TestCase):
    def test_known_good_configs_produce_no_errors(self):
        checked = 0
        for path in REAL_CONFIGS:
            if not os.path.exists(path):
                continue
            checked += 1
            with self.subTest(config=os.path.basename(path)):
                errors = [str(f) for f in validate_file(path) if f.level == "error"]
                self.assertEqual(errors, [])
        if checked == 0:
            self.skipTest("no reference collector configs available")


if __name__ == "__main__":
    unittest.main(verbosity=2)
