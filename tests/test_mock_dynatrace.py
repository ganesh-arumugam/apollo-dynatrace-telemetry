#!/usr/bin/env python3
"""Tests for the mock Dynatrace OTLP endpoint.

The mock is the thing that decides whether a live router run passes, so it needs
its own tests: a mock that accepts everything would make layer 3 of the harness
meaningless.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "harness"))

import mock_dynatrace  # noqa: E402

VALID_AUTH = "Api-Token dt0c01.unittest"
PROTOBUF = "application/x-protobuf"


def post(url, body=b"payload", headers=None):
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def get(url):
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.status, resp.read()


class MockServerTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.record = os.path.join("/tmp", "dynatrace-mock-unittest.jsonl")
        if os.path.exists(cls.record):
            os.remove(cls.record)
        cls.httpd = mock_dynatrace.serve(0, record=cls.record)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        post(f"{self.base}/_harness/reset", b"")

    # -- accept path ------------------------------------------------------
    def test_accepts_each_signal(self):
        for signal, path in (("metrics", "/api/v2/otlp/v1/metrics"),
                             ("traces", "/api/v2/otlp/v1/traces"),
                             ("logs", "/api/v2/otlp/v1/logs")):
            with self.subTest(signal=signal):
                status, _ = post(self.base + path, headers={
                    "Authorization": VALID_AUTH, "Content-Type": PROTOBUF})
                self.assertEqual(status, 200)
        stats = json.loads(get(f"{self.base}/_harness/stats")[1])
        self.assertEqual(stats["counts"], {"metrics": 1, "traces": 1, "logs": 1})
        self.assertEqual(stats["total_accepted"], 3)

    def test_accepts_json_protocol(self):
        status, _ = post(self.base + "/api/v2/otlp/v1/metrics", headers={
            "Authorization": VALID_AUTH, "Content-Type": "application/json"})
        self.assertEqual(status, 200)

    def test_content_type_with_charset_suffix_is_accepted(self):
        status, _ = post(self.base + "/api/v2/otlp/v1/metrics", headers={
            "Authorization": VALID_AUTH,
            "Content-Type": "application/x-protobuf; charset=utf-8"})
        self.assertEqual(status, 200)

    def test_records_payload_size(self):
        post(self.base + "/api/v2/otlp/v1/metrics", b"0123456789", headers={
            "Authorization": VALID_AUTH, "Content-Type": PROTOBUF})
        stats = json.loads(get(f"{self.base}/_harness/stats")[1])
        self.assertEqual(stats["bytes"]["metrics"], 10)

    def test_appends_to_record_file(self):
        post(self.base + "/api/v2/otlp/v1/traces", headers={
            "Authorization": VALID_AUTH, "Content-Type": PROTOBUF})
        with open(self.record) as fh:
            lines = [json.loads(line) for line in fh if line.strip()]
        self.assertTrue(any(entry["signal"] == "traces" for entry in lines))

    # -- reject path ------------------------------------------------------
    def test_rejects_bearer_token(self):
        status, body = post(self.base + "/api/v2/otlp/v1/metrics", headers={
            "Authorization": "Bearer dt0c01.unittest", "Content-Type": PROTOBUF})
        self.assertEqual(status, 401)
        self.assertIn("Api-Token", json.loads(body)["error"])

    def test_rejects_empty_token(self):
        status, _ = post(self.base + "/api/v2/otlp/v1/metrics", headers={
            "Authorization": "Api-Token   ", "Content-Type": PROTOBUF})
        self.assertEqual(status, 401)

    def test_rejects_missing_authorization(self):
        status, _ = post(self.base + "/api/v2/otlp/v1/metrics",
                         headers={"Content-Type": PROTOBUF})
        self.assertEqual(status, 401)

    def test_rejects_collector_style_path(self):
        """A collector path (/v1/metrics) is the most common Dynatrace mistake."""
        status, body = post(self.base + "/v1/metrics", headers={
            "Authorization": VALID_AUTH, "Content-Type": PROTOBUF})
        self.assertEqual(status, 404)
        self.assertIn("/api/v2/otlp/v1/metrics", json.loads(body)["error"])

    def test_rejects_wrong_content_type(self):
        status, _ = post(self.base + "/api/v2/otlp/v1/logs", headers={
            "Authorization": VALID_AUTH, "Content-Type": "text/plain"})
        self.assertEqual(status, 415)

    def test_rejections_are_recorded_and_not_counted_as_accepted(self):
        post(self.base + "/v1/metrics", headers={
            "Authorization": VALID_AUTH, "Content-Type": PROTOBUF})
        stats = json.loads(get(f"{self.base}/_harness/stats")[1])
        self.assertEqual(stats["total_accepted"], 0)
        self.assertEqual(len(stats["rejections"]), 1)
        self.assertEqual(stats["rejections"][0]["status"], 404)

    # -- control plane ----------------------------------------------------
    def test_reset_clears_counters(self):
        post(self.base + "/api/v2/otlp/v1/metrics", headers={
            "Authorization": VALID_AUTH, "Content-Type": PROTOBUF})
        post(f"{self.base}/_harness/reset", b"")
        stats = json.loads(get(f"{self.base}/_harness/stats")[1])
        self.assertEqual(stats["total_accepted"], 0)

    def test_health_endpoint(self):
        status, body = get(f"{self.base}/_harness/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "ok")


if __name__ == "__main__":
    unittest.main(verbosity=2)
