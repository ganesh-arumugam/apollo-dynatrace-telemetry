#!/usr/bin/env python3
"""Tests for the generated Dynatrace dashboard document.

The dashboard is the artifact a customer actually looks at, and Dynatrace accepts
a lot of nonsense silently: overlapping tiles render on top of each other, an
unknown visualization renders empty, a `singleValue` tile with the wrong
recordField shows a blank number. All of that is checked here rather than in the
tenant.
"""
from __future__ import annotations

import json
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import yaml  # noqa: E402
import build_dashboard  # noqa: E402

GRID_WIDTH = build_dashboard.GRID_WIDTH


def load_dashboard() -> dict:
    with open(build_dashboard.TARGET) as fh:
        return json.load(fh)


class TestGeneratedDashboardIsCurrent(unittest.TestCase):
    def test_committed_json_matches_tiles_yaml(self):
        self.assertEqual(build_dashboard.main(["--check"]), 0,
                         "run python3 scripts/build_dashboard.py")


class TestDocumentShape(unittest.TestCase):
    def setUp(self):
        self.dash = load_dashboard()

    def test_top_level_shape(self):
        self.assertEqual(self.dash["version"], 17)
        self.assertIn("tiles", self.dash)
        self.assertIn("layouts", self.dash)
        self.assertIsInstance(self.dash["variables"], list)

    def test_every_tile_has_a_layout_and_vice_versa(self):
        self.assertEqual(set(self.dash["tiles"]), set(self.dash["layouts"]))

    def test_tile_keys_are_contiguous_stringified_indices(self):
        keys = sorted(int(k) for k in self.dash["tiles"])
        self.assertEqual(keys, list(range(len(keys))))

    def test_no_tile_exceeds_the_grid(self):
        for key, box in self.dash["layouts"].items():
            with self.subTest(tile=key):
                self.assertGreaterEqual(box["x"], 0)
                self.assertLessEqual(box["x"] + box["w"], GRID_WIDTH)
                self.assertGreater(box["h"], 0)

    def test_no_overlapping_tiles(self):
        build_dashboard.assert_no_overlap(self.dash)  # raises on overlap

    def test_data_tiles_are_well_formed(self):
        # Metrics-based tiles use `timeseries`; the span/log tiles added for
        # Datadog-parity (top operations, dependencies, exemplars, error logs)
        # use `fetch spans` / `fetch logs` instead — both are valid DQL commands.
        valid_prefixes = ("timeseries", "fetch spans", "fetch logs")
        data_tiles = [t for t in self.dash["tiles"].values() if t["type"] == "data"]
        self.assertGreater(len(data_tiles), 10)
        for tile in data_tiles:
            with self.subTest(tile=tile["title"]):
                self.assertTrue(tile["title"])
                self.assertIn(tile["visualization"], build_dashboard.VALID_VIZ)
                self.assertTrue(tile["query"].startswith(valid_prefixes),
                                f"query must start with one of {valid_prefixes}")

    def test_single_value_tiles_declare_the_field_they_display(self):
        """A singleValue tile whose recordField doesn't match the DQL variable
        renders blank in Dynatrace."""
        for tile in self.dash["tiles"].values():
            if tile.get("visualization") != "singleValue":
                continue
            with self.subTest(tile=tile["title"]):
                field = tile["visualizationSettings"]["singleValue"]["recordField"]
                self.assertTrue(field, "recordField missing")
                assigned = re.findall(r"(\w+)\s*=", tile["query"])
                self.assertIn(field, assigned,
                              f"recordField {field!r} is not assigned in the query")

    def test_every_query_is_scoped_to_the_service(self):
        for tile in self.dash["tiles"].values():
            if tile["type"] != "data":
                continue
            with self.subTest(tile=tile["title"]):
                self.assertIn("service.name", tile["query"],
                              "unscoped query will pull in every service in the tenant")


class TestDashboardCoversTheInstruments(unittest.TestCase):
    """The config, the dashboard, and the DQL pack must describe the same metrics.
    Any one of them drifting produces empty tiles or unmonitored instruments."""

    def setUp(self):
        with open(os.path.join(ROOT, "templates", "instruments.router.yaml")) as fh:
            cfg = yaml.safe_load(fh)
        instruments = cfg["telemetry"]["instrumentation"]["instruments"]
        self.custom = []
        for service, body in instruments.items():
            if not isinstance(body, dict):
                continue
            for name, spec in body.items():
                if isinstance(spec, dict) and "value" in spec:
                    self.custom.append(name)
        self.dash_json = json.dumps(load_dashboard())

    def test_custom_instruments_have_tiles(self):
        self.assertTrue(self.custom)
        for name in self.custom:
            with self.subTest(instrument=name):
                self.assertIn(name, self.dash_json,
                              f"{name} is collected but never charted")

    def test_tile_metrics_are_plausible_metric_keys(self):
        """Grail uses the plain dotted key; the ext: prefix belongs to the classic
        Metrics API only. An ext: key in a DQL tile silently returns nothing."""
        dash = load_dashboard()
        for tile in dash["tiles"].values():
            if tile["type"] != "data":
                continue
            with self.subTest(tile=tile["title"]):
                self.assertNotIn("ext:", tile["query"])


class TestDqlPackStaysInSync(unittest.TestCase):
    def test_every_dashboard_metric_appears_in_the_dql_pack(self):
        with open(os.path.join(ROOT, "dashboards", "dql-queries.md")) as fh:
            pack = fh.read()
        dash = load_dashboard()
        metrics = set()
        for tile in dash["tiles"].values():
            if tile["type"] != "data":
                continue
            # metric keys are the arguments to the aggregation functions
            metrics |= {
                name for name in re.findall(
                    r"(?:sum|count|avg|percentile|min|max)\(\s*([a-z][\w.]*)",
                    tile["query"])
                # skip DQL helpers wrapped by an aggregation in summarize clauses
                # (sum(arraySum(cnt)) etc.) - those aren't metric keys
                if "." in name
            }
        self.assertTrue(metrics)
        missing = sorted(m for m in metrics if m not in pack)
        self.assertEqual(missing, [],
                         "metrics charted but not documented in dql-queries.md")


if __name__ == "__main__":
    unittest.main(verbosity=2)
