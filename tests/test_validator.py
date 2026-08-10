#!/usr/bin/env python3
"""Tests for the DT### rule set.

Every rule is covered from both sides: a fixture that must trip it, and the
shipped templates which must not. The bad/warn expectations live next to the
fixture definitions in make_fixtures.py, so a fixture can't drift away from the
rule it is supposed to prove.
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, HERE)

import make_fixtures  # noqa: E402
from validate_dynatrace import (RULES, Validator, main as validator_main,  # noqa: E402
                                validate_file)

ALL_RULES = set(RULES)

# Known-good production router configs, as a false-positive guard: a rule that
# errors on a config that demonstrably works in production is a bad rule. Supply
# your own with DT_REAL_ROUTER_CONFIGS=/path/a.yaml:/path/b.yaml — the checks skip
# entirely when it is unset, so the suite is self-contained by default.
REAL_ROUTER_CONFIGS = [p for p in
                       os.environ.get("DT_REAL_ROUTER_CONFIGS", "").split(":") if p]


def fixture(subdir: str, name: str) -> str:
    return os.path.join(HERE, "fixtures", subdir, name)


def rules(findings, level=None):
    return {f.rule for f in findings if level is None or f.level == level}


class TestGoodFixtures(unittest.TestCase):
    def test_good_fixtures_have_no_errors(self):
        for name in make_fixtures.GOOD:
            with self.subTest(fixture=name):
                findings = validate_file(fixture("good", name))
                errors = [f for f in findings if f.level == "error"]
                self.assertEqual(errors, [], f"{name}: {[str(e) for e in errors]}")

    def test_all_signals_fixture_is_completely_clean(self):
        findings = validate_file(fixture("good", "all_signals.yaml"))
        self.assertEqual([str(f) for f in findings], [])

    def test_authorization_header_is_case_insensitive(self):
        findings = validate_file(fixture("good", "lowercase_auth_header.yaml"))
        self.assertNotIn("DT005", rules(findings))

    def test_config_without_telemetry_is_not_flagged(self):
        self.assertEqual(validate_file(fixture("good", "no_telemetry.yaml")), [])


class TestBadFixtures(unittest.TestCase):
    def test_each_bad_fixture_trips_its_rule(self):
        for name, (rule, _body) in make_fixtures.BAD.items():
            with self.subTest(fixture=name, rule=rule):
                findings = validate_file(fixture("bad", name))
                self.assertIn(rule, rules(findings, "error"),
                              f"{name} should raise {rule}; got "
                              f"{sorted(rules(findings, 'error'))}")


class TestWarnFixtures(unittest.TestCase):
    def test_each_warn_fixture_trips_its_rule_as_a_warning(self):
        for name, (rule, _body) in make_fixtures.WARN.items():
            with self.subTest(fixture=name, rule=rule):
                findings = validate_file(fixture("warn", name))
                self.assertIn(rule, rules(findings, "warning"),
                              f"{name} should warn {rule}; got "
                              f"{sorted(rules(findings, 'warning'))}")

    def test_warnings_alone_do_not_fail_validation(self):
        code = validator_main([fixture("warn", "full_sampler.yaml")])
        self.assertEqual(code, 0)

    def test_strict_mode_turns_warnings_into_failure(self):
        code = validator_main(["--strict", fixture("warn", "full_sampler.yaml")])
        self.assertEqual(code, 1)


class TestShippedTemplates(unittest.TestCase):
    def test_templates_pass(self):
        tmpl_dir = os.path.join(ROOT, "templates")
        files = sorted(f for f in os.listdir(tmpl_dir) if f.endswith(".yaml"))
        self.assertTrue(files, "no templates found")
        for name in files:
            with self.subTest(template=name):
                findings = validate_file(os.path.join(tmpl_dir, name))
                errors = [str(f) for f in findings if f.level == "error"]
                self.assertEqual(errors, [])

    def test_harness_config_passes_in_loopback_mode(self):
        path = os.path.join(ROOT, "harness", "harness.router.yaml")
        findings = validate_file(path, allow_loopback=True)
        errors = [str(f) for f in findings if f.level == "error"]
        self.assertEqual(errors, [])

    def test_loopback_allowance_is_opt_in(self):
        """A literal http://127.0.0.1 endpoint must fail by default, or a
        harness config could ship to production unnoticed."""
        cfg = {"telemetry": {"exporters": {"metrics": {"otlp": {
            "enabled": True, "protocol": "http", "temporality": "delta",
            "endpoint": "http://127.0.0.1:4318/api/v2/otlp/v1/metrics",
            "http": {"headers": {"Authorization": "Api-Token ${env.T}"}}}}}}}
        self.assertIn("DT010", rules(Validator(cfg).run(), "error"))
        self.assertNotIn("DT010",
                         rules(Validator(cfg, allow_loopback=True).run(), "error"))

    def test_instruments_template_matches_dql_pack(self):
        """Every custom instrument in the template must appear in the DQL doc,
        so the dashboard and the config cannot drift apart."""
        import yaml
        tmpl = os.path.join(ROOT, "templates", "instruments.router.yaml")
        with open(tmpl) as fh:
            cfg = yaml.safe_load(fh)
        instruments = cfg["telemetry"]["instrumentation"]["instruments"]
        custom = []
        for service, body in instruments.items():
            if not isinstance(body, dict):
                continue
            for name, spec in body.items():
                if isinstance(spec, dict) and "value" in spec:
                    custom.append(name)
        self.assertTrue(custom, "template defines no custom instruments")

        with open(os.path.join(ROOT, "dashboards", "dql-queries.md")) as fh:
            dql = fh.read()
        for name in custom:
            with self.subTest(instrument=name):
                self.assertIn(name, dql,
                              f"{name} has no DQL query in dashboards/dql-queries.md")


class TestRealWorldRouterConfigs(unittest.TestCase):
    """Collector-topology router configs must not trip the direct-mode rules.

    These configs point the router at an OTel Collector over gRPC, which is
    correct; before mode inference existed, every one of them produced six
    bogus errors.
    """

    def test_no_false_positives_on_known_good_configs(self):
        checked = 0
        for path in REAL_ROUTER_CONFIGS:
            if not os.path.exists(path):
                continue
            checked += 1
            with self.subTest(config=os.path.basename(path)):
                errors = [str(f) for f in validate_file(path) if f.level == "error"]
                self.assertEqual(errors, [])
        if checked == 0:
            self.skipTest("no reference router configs available")

    def test_collector_endpoints_are_detected_as_collector_mode(self):
        for endpoint in ("http://otel-collector:4317", "http://localhost:4327",
                         "${env.OTEL_COLLECTOR_GRPC}", "http://collector.obs:4318"):
            with self.subTest(endpoint=endpoint):
                v = Validator({})
                self.assertEqual(v._resolve_mode(endpoint), "collector")

    def test_dynatrace_endpoints_are_detected_as_direct_mode(self):
        for endpoint in ("https://abc.live.dynatrace.com:443/api/v2/otlp/v1/metrics",
                         "${env.DYNATRACE_ENV_URL}/api/v2/otlp/v1/metrics",
                         "${env.DT_OTLP_ENDPOINT}",
                         "https://gw.internal:443/api/v2/otlp/v1/metrics"):
            with self.subTest(endpoint=endpoint):
                v = Validator({})
                self.assertEqual(v._resolve_mode(endpoint), "direct")

    def test_mode_can_be_forced(self):
        cfg = {"telemetry": {"exporters": {"metrics": {"otlp": {
            "enabled": True, "protocol": "grpc",
            "endpoint": "https://abc.live.dynatrace.com:443/api/v2/otlp/v1/metrics",
            "http": {"headers": {"Authorization": "Api-Token ${env.T}"}}}}}}}
        forced = rules(Validator(cfg, mode="collector").run(), "error")
        self.assertNotIn("DT009", forced)
        inferred = rules(Validator(cfg).run(), "error")
        self.assertIn("DT009", inferred)


class TestRuleCoverage(unittest.TestCase):
    def test_every_rule_has_a_fixture(self):
        covered = ({rule for rule, _ in make_fixtures.BAD.values()} |
                   {rule for rule, _ in make_fixtures.WARN.values()})
        self.assertEqual(ALL_RULES - covered, set(),
                         "rules with no fixture coverage")

    def test_rules_are_documented_in_readme(self):
        with open(os.path.join(ROOT, "README.md")) as fh:
            readme = fh.read()
        for rule in sorted(ALL_RULES):
            with self.subTest(rule=rule):
                self.assertIn(rule, readme)


class TestValidatorCLI(unittest.TestCase):
    def _run(self, *args):
        return subprocess.run(
            [sys.executable, os.path.join(ROOT, "scripts", "validate_dynatrace.py"), *args],
            capture_output=True, text=True)

    def test_cli_exit_codes(self):
        self.assertEqual(self._run(fixture("good", "metrics_only.yaml")).returncode, 0)
        self.assertEqual(self._run(fixture("bad", "grpc_protocol.yaml")).returncode, 1)
        self.assertEqual(self._run("/nonexistent/router.yaml").returncode, 2)

    def test_cli_json_output_is_parseable(self):
        import json
        result = self._run("--json", fixture("bad", "cumulative_temporality.yaml"))
        payload = json.loads(result.stdout)
        rule_ids = [f["rule"] for findings in payload.values() for f in findings]
        self.assertIn("DT002", rule_ids)

    def test_cli_reports_multiple_files_independently(self):
        result = self._run(fixture("good", "metrics_only.yaml"),
                           fixture("bad", "missing_port.yaml"))
        self.assertIn("PASS", result.stdout)
        self.assertIn("FAIL", result.stdout)
        self.assertEqual(result.returncode, 1)


class TestValidatorUnits(unittest.TestCase):
    def test_env_var_endpoint_skips_port_check(self):
        cfg = {"telemetry": {"exporters": {"metrics": {"otlp": {
            "enabled": True, "protocol": "http", "temporality": "delta",
            "endpoint": "${env.DYNATRACE_ENV_URL}/api/v2/otlp/v1/metrics",
            "http": {"headers": {"Authorization": "Api-Token ${env.T}"}}}}}}}
        self.assertNotIn("DT004", rules(Validator(cfg).run(), "error"))

    def test_protocol_rules_split_by_mode_and_value(self):
        """gRPC to Dynatrace is DT009; an outright invalid protocol is DT001;
        gRPC to a collector is correct and must produce nothing."""
        def cfg_for(endpoint, protocol):
            otlp = {"enabled": True, "temporality": "delta", "endpoint": endpoint,
                    "http": {"headers": {"Authorization": "Api-Token ${env.T}"}}}
            if protocol is not None:
                otlp["protocol"] = protocol
            return {"telemetry": {"exporters": {"metrics": {"otlp": otlp}}}}

        dt = "https://abc.live.dynatrace.com:443/api/v2/otlp/v1/metrics"
        self.assertIn("DT009", rules(Validator(cfg_for(dt, "grpc")).run(), "error"))
        self.assertIn("DT009", rules(Validator(cfg_for(dt, None)).run(), "error"))
        self.assertIn("DT001", rules(Validator(cfg_for(dt, "thrift")).run(), "error"))

        collector = "http://otel-collector:4317"
        found = rules(Validator(cfg_for(collector, "grpc")).run(), "error")
        self.assertEqual(found & {"DT001", "DT009", "DT101"}, set())

    def test_attribute_only_override_is_not_treated_as_custom_instrument(self):
        cfg = {"telemetry": {"instrumentation": {"instruments": {"router": {
            "http.server.request.duration": {"attributes": {"http.response.status_code": True}}}}}}}
        self.assertEqual(rules(Validator(cfg).run(), "error"), set())

    def test_malformed_telemetry_block_does_not_crash(self):
        for junk in (None, "telemetry", 42, [], {"exporters": "nope"}):
            with self.subTest(junk=junk):
                Validator({"telemetry": junk}).run()  # must not raise


if __name__ == "__main__":
    unittest.main(verbosity=2)
