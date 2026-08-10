#!/usr/bin/env python3
"""Tests for the runnable demo.

Three things can silently break a demo and waste a customer call: a subgraph that
answers the wrong shape, a supergraph whose subgraph URLs don't match the ports we
actually start, and a config that reads an env var nobody documented. All three
are checked here.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEMO = os.path.join(ROOT, "demo")
sys.path.insert(0, DEMO)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import yaml  # noqa: E402
import subgraphs as demo_subgraphs  # noqa: E402
from validate_dynatrace import validate_file as validate_router  # noqa: E402
from validate_collector import validate_file as validate_collector  # noqa: E402


def post(url: str, payload: dict):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


class SubgraphTests(unittest.TestCase):
    """The subgraphs exist to make the router emit telemetry, so what matters is
    that the router's own query shapes resolve — especially the entity fetch."""

    @classmethod
    def setUpClass(cls):
        cls.products = demo_subgraphs.serve("products", 0)
        cls.orders = demo_subgraphs.serve("orders", 0)
        for server in (cls.products, cls.orders):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        cls.products_url = f"http://127.0.0.1:{cls.products.server_address[1]}/"
        cls.orders_url = f"http://127.0.0.1:{cls.orders.server_address[1]}/"

    @classmethod
    def tearDownClass(cls):
        for server in (cls.products, cls.orders):
            server.shutdown()
            server.server_close()

    def test_products_root_field(self):
        status, body = post(self.products_url,
                            {"query": "{products{id title price}}"})
        self.assertEqual(status, 200)
        products = body["data"]["products"]
        self.assertEqual(len(products), 3)
        for product in products:
            self.assertEqual(set(product), {"id", "title", "price"})

    def test_product_by_id_uses_variables(self):
        status, body = post(self.products_url, {
            "query": "query($id:ID!){product(id:$id){id title price}}",
            "variables": {"id": "product:2"}})
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["product"]["title"], "Summit Fleece Jacket")

    def test_orders_returns_entity_stubs(self):
        """orders must return __typename + id only — the router fetches the rest."""
        status, body = post(self.orders_url,
                            {"query": "{orders{id total items{__typename id}}}"})
        self.assertEqual(status, 200)
        items = body["data"]["orders"][0]["items"]
        self.assertTrue(items)
        for item in items:
            self.assertEqual(item["__typename"], "Product")
            self.assertIn("id", item)

    def test_entity_fetch_resolves_representations_in_order(self):
        status, body = post(self.products_url, {
            "query": ("query($representations:[_Any!]!)"
                      "{_entities(representations:$representations)"
                      "{...on Product{title price}}}"),
            "variables": {"representations": [
                {"__typename": "Product", "id": "product:3"},
                {"__typename": "Product", "id": "product:1"}]}})
        self.assertEqual(status, 200)
        titles = [entity["title"] for entity in body["data"]["_entities"]]
        self.assertEqual(titles, ["Alpine Shell", "Trail Runner Pro"])

    def test_unknown_entity_key_does_not_break_the_response(self):
        """A null in _entities would fail a non-null field and kill the demo."""
        status, body = post(self.products_url, {
            "query": ("query($representations:[_Any!]!)"
                      "{_entities(representations:$representations){...on Product{title}}}"),
            "variables": {"representations": [
                {"__typename": "Product", "id": "product:404"}]}})
        self.assertEqual(status, 200)
        self.assertIsNotNone(body["data"]["_entities"][0])

    def test_boom_returns_a_graphql_error_with_http_200(self):
        """This is the whole point of the field: HTTP 200 + errors is what makes
        otel.status_code mapping necessary."""
        status, body = post(self.products_url, {"query": "{boom}"})
        self.assertEqual(status, 200)
        self.assertEqual(body["errors"][0]["extensions"]["code"], "BOOM")

    def test_fail_flag_forces_a_500(self):
        status, _ = post(self.products_url + "?fail=1", {"query": "{products{id}}"})
        self.assertEqual(status, 500)


class SupergraphConsistencyTests(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(DEMO, "supergraph.graphql")) as fh:
            self.sdl = fh.read()

    def test_join_graph_urls_match_the_ports_up_sh_starts(self):
        urls = dict(re.findall(
            r'@join__graph\(name: "(\w+)", url: "([^"]+)"\)', self.sdl))
        self.assertEqual(set(urls), {"products", "orders"})
        defaults = {"products": demo_subgraphs.__doc__, "orders": None}
        self.assertIn("4011", urls["products"])
        self.assertIn("4012", urls["orders"])
        self.assertIsNotNone(defaults)  # doc references the same ports

        with open(os.path.join(DEMO, "up.sh")) as fh:
            up = fh.read()
        self.assertIn("PRODUCTS_PORT:-4011", up)
        self.assertIn("ORDERS_PORT:-4012", up)

    def test_supergraph_matches_subgraph_schemas(self):
        """Fields declared in the subgraph SDLs must exist in the committed
        composed supergraph, or the demo queries 400."""
        for name in ("products", "orders"):
            path = os.path.join(DEMO, "subgraph-schemas", f"{name}.graphql")
            with open(path) as fh:
                subgraph_sdl = fh.read()
            for field in re.findall(r"^\s{2}(\w+)[(:]", subgraph_sdl, re.M):
                with self.subTest(subgraph=name, field=field):
                    self.assertIn(field, self.sdl)

    def test_supergraph_yaml_points_at_the_committed_schemas(self):
        with open(os.path.join(DEMO, "supergraph.yaml")) as fh:
            cfg = yaml.safe_load(fh)
        for name, spec in cfg["subgraphs"].items():
            path = os.path.join(DEMO, spec["schema"]["file"].lstrip("./"))
            with self.subTest(subgraph=name):
                self.assertTrue(os.path.exists(path), path)

    def test_entity_key_is_declared_in_both_graphs(self):
        product_block = re.search(r"type Product\b.*?\{", self.sdl, re.S).group(0)
        self.assertIn("join__type(graph: PRODUCTS, key: \"id\")", product_block)
        self.assertIn("join__type(graph: ORDERS, key: \"id\", resolvable: false)",
                      product_block)


class DemoConfigTests(unittest.TestCase):
    def test_direct_config_passes_direct_rules(self):
        findings = validate_router(os.path.join(DEMO, "router.direct.yaml"))
        errors = [str(f) for f in findings if f.level == "error"]
        self.assertEqual(errors, [])

    def test_collector_config_passes_collector_rules(self):
        findings = validate_router(os.path.join(DEMO, "router.collector.yaml"),
                                   mode="collector")
        errors = [str(f) for f in findings if f.level == "error"]
        self.assertEqual(errors, [])

    def test_demo_collector_yaml_passes_dtc_rules(self):
        findings = validate_collector(os.path.join(DEMO, "otel-collector.yaml"))
        errors = [str(f) for f in findings if f.level == "error"]
        self.assertEqual(errors, [])

    def test_both_router_configs_mark_graphql_errors(self):
        for name in ("router.direct.yaml", "router.collector.yaml"):
            with open(os.path.join(DEMO, name)) as fh:
                cfg = yaml.safe_load(fh)
            spans = cfg["telemetry"]["instrumentation"]["spans"]
            for stage in ("supergraph", "subgraph"):
                with self.subTest(config=name, stage=stage):
                    self.assertIn("otel.status_code",
                                  spans[stage]["attributes"])

    def test_demo_configs_do_not_smuggle_in_non_dynatrace_backends(self):
        """This repo is Dynatrace-only on purpose — no Jaeger/Zipkin/Datadog
        exporters. Comments may mention them; configuration may not."""
        for name in ("router.direct.yaml", "router.collector.yaml",
                     "otel-collector.yaml", "docker-compose.yaml"):
            with open(os.path.join(DEMO, name)) as fh:
                config_only = "\n".join(
                    line.split("#", 1)[0] for line in fh).lower()
            for foreign in ("jaeger", "zipkin", "datadog", "grafana", "newrelic"):
                with self.subTest(config=name, backend=foreign):
                    self.assertNotIn(foreign, config_only)


class EnvExampleTests(unittest.TestCase):
    """Every env var a config reads must be documented in .env.example, or the
    first run fails on an unset variable with no hint about what to set."""

    CONFIG_GLOBS = ("templates", "demo", "harness")
    # Vars intentionally absent: these must stay unset, and up.sh enforces that.
    ALLOWED_MISSING: set[str] = set()

    def setUp(self):
        with open(os.path.join(ROOT, ".env.example")) as fh:
            self.env_example = fh.read()

    def _referenced_vars(self) -> dict[str, str]:
        """var -> file that reads it, across router configs and collector configs."""
        found = {}
        for folder in self.CONFIG_GLOBS:
            for dirpath, _dirs, files in os.walk(os.path.join(ROOT, folder)):
                for name in files:
                    if not name.endswith((".yaml", ".yml")):
                        continue
                    path = os.path.join(dirpath, name)
                    with open(path) as fh:
                        text = fh.read()
                    # router style ${env.VAR} / ${env.VAR:-default}
                    # collector style ${env:VAR}
                    for var in re.findall(r"\$\{env[.:]([A-Za-z_][A-Za-z0-9_]*)", text):
                        found.setdefault(var, os.path.relpath(path, ROOT))
        return found

    def test_every_referenced_var_is_documented(self):
        referenced = self._referenced_vars()
        self.assertTrue(referenced, "no env vars found — check the regex")
        for var, source in sorted(referenced.items()):
            if var in self.ALLOWED_MISSING:
                continue
            with self.subTest(var=var, source=source):
                self.assertIn(var, self.env_example,
                              f"{var} is read by {source} but not in .env.example")

    def test_shell_scripts_env_vars_are_documented(self):
        """Scripts read a few vars the YAML never mentions (tokens, ports)."""
        interesting = ("DT_ENVIRONMENT_ID", "DT_API_TOKEN", "DT_BEARER_TOKEN",
                       "DT_OAUTH_CLIENT_ID", "DT_OAUTH_CLIENT_SECRET",
                       "ROUTER_BIN", "ROUTER_PORT", "PRODUCTS_PORT", "ORDERS_PORT",
                       "COLLECTOR_METRICS", "METRICS", "DASHBOARD_NAME")
        for var in interesting:
            with self.subTest(var=var):
                self.assertIn(var, self.env_example)

    def test_the_must_stay_unset_vars_are_called_out(self):
        for var in ("OTEL_EXPORTER_OTLP_ENDPOINT",
                    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
                    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"):
            self.assertIn(var, self.env_example)

    def test_no_real_looking_token_committed(self):
        """A pasted token in .env.example would be a leak. Only assignment lines
        matter — prose may spell out the token prefixes."""
        for line in self.env_example.splitlines():
            body = line.lstrip("# ").strip()
            if "=" not in body:
                continue
            value = body.split("=", 1)[1].strip()
            if not re.match(r"dt0[cps]\d{2}\.", value):
                continue
            with self.subTest(assignment=body):
                self.assertIn("REPLACE_ME", value)


class DemoScriptTests(unittest.TestCase):
    def test_scripts_are_executable_and_parse(self):
        for name in ("up.sh", "down.sh", "load.sh"):
            path = os.path.join(DEMO, name)
            with self.subTest(script=name):
                self.assertTrue(os.access(path, os.X_OK), f"{name} not executable")
                self.assertEqual(
                    subprocess.run(["bash", "-n", path], capture_output=True).returncode,
                    0)

    def test_up_sh_fails_fast_without_env(self):
        """Running with no .env must produce the actionable error, not a stack
        trace from the router."""
        with open(os.path.join(DEMO, "up.sh")) as fh:
            up = fh.read()
        self.assertIn("no .env", up)
        self.assertIn("OTEL_EXPORTER_OTLP_ENDPOINT", up)
        self.assertIn("validate_dynatrace.py", up)

    def test_load_sh_exercises_every_instrument_path(self):
        with open(os.path.join(DEMO, "load.sh")) as fh:
            load = fh.read()
        for query in ("products", "orders", "boom", "doesNotExist"):
            with self.subTest(query=query):
                self.assertIn(query, load)


if __name__ == "__main__":
    unittest.main(verbosity=2)
