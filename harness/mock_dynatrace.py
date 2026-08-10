#!/usr/bin/env python3
"""
Mock Dynatrace OTLP ingest endpoint.

Stands in for https://<env>.live.dynatrace.com/api/v2/otlp/* so the router's
Dynatrace configuration can be exercised end to end without a tenant, a token,
or egress. It is deliberately strict: it enforces the same things the real
endpoint enforces, so a config that passes here is a config that works there.

Enforced on every request:
  * path must be /api/v2/otlp/v1/{metrics,traces,logs}   -> else 404
  * Authorization must be `Api-Token <non-empty>`        -> else 401
  * Content-Type must be application/x-protobuf or /json -> else 415
  * method must be POST                                  -> else 405

Control plane (not part of the OTLP surface):
  GET  /_harness/stats   -> JSON counts + last error per signal
  POST /_harness/reset   -> clear counters

Usage:
  python3 mock_dynatrace.py --port 4318 --record /tmp/otlp.jsonl
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SIGNAL_BY_PATH = {
    "/api/v2/otlp/v1/metrics": "metrics",
    "/api/v2/otlp/v1/traces": "traces",
    "/api/v2/otlp/v1/logs": "logs",
}
VALID_CONTENT_TYPES = ("application/x-protobuf", "application/json")


class Recorder:
    """Thread-safe tally of what the router actually sent."""

    def __init__(self, record_path: str | None = None):
        self._lock = threading.Lock()
        self.record_path = record_path
        self.reset()

    def reset(self):
        with self._lock:
            self.counts = {"metrics": 0, "traces": 0, "logs": 0}
            self.bytes = {"metrics": 0, "traces": 0, "logs": 0}
            self.rejections: list[dict] = []
            self.first_seen: dict[str, float] = {}

    def accept(self, signal: str, size: int, headers: dict):
        with self._lock:
            self.counts[signal] += 1
            self.bytes[signal] += size
            self.first_seen.setdefault(signal, time.time())
            if self.record_path:
                with open(self.record_path, "a") as fh:
                    fh.write(json.dumps({
                        "ts": time.time(), "signal": signal, "bytes": size,
                        "content_type": headers.get("Content-Type"),
                    }) + "\n")

    def reject(self, path: str, status: int, reason: str):
        with self._lock:
            self.rejections.append({"path": path, "status": status,
                                    "reason": reason, "ts": time.time()})

    def stats(self) -> dict:
        with self._lock:
            return {
                "counts": dict(self.counts),
                "bytes": dict(self.bytes),
                "rejections": list(self.rejections),
                "total_accepted": sum(self.counts.values()),
            }


class Handler(BaseHTTPRequestHandler):
    recorder: Recorder = Recorder()
    verbose: bool = False
    protocol_version = "HTTP/1.1"

    # -- plumbing ---------------------------------------------------------
    def log_message(self, fmt, *args):  # silence default stderr spam
        if Handler.verbose:
            super().log_message(fmt, *args)

    def _respond(self, status: int, body: bytes = b"", ctype="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _fail(self, status: int, reason: str):
        Handler.recorder.reject(self.path, status, reason)
        self._respond(status, json.dumps({"error": reason}).encode())

    # -- routes -----------------------------------------------------------
    def do_GET(self):
        if self.path == "/_harness/stats":
            self._respond(200, json.dumps(Handler.recorder.stats()).encode())
        elif self.path == "/_harness/health":
            self._respond(200, b'{"status":"ok"}')
        else:
            self._fail(405, "only POST is accepted on OTLP paths")

    def do_POST(self):
        if self.path == "/_harness/reset":
            Handler.recorder.reset()
            self._respond(200, b'{"status":"reset"}')
            return

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""

        signal = SIGNAL_BY_PATH.get(self.path.split("?")[0])
        if signal is None:
            self._fail(404, f"unknown OTLP path {self.path!r}; expected one of "
                            f"{sorted(SIGNAL_BY_PATH)}")
            return

        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Api-Token ") or not auth[len("Api-Token "):].strip():
            self._fail(401, "Authorization must be 'Api-Token <token>'")
            return

        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if ctype not in VALID_CONTENT_TYPES:
            self._fail(415, f"unsupported Content-Type {ctype!r}")
            return

        Handler.recorder.accept(signal, len(body), dict(self.headers))
        self._respond(200, b"{}", ctype="application/x-protobuf")


def serve(port: int, record: str | None = None, verbose: bool = False):
    Handler.recorder = Recorder(record)
    Handler.verbose = verbose
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    return httpd


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=4318)
    ap.add_argument("--record", help="append accepted exports to this JSONL file")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    httpd = serve(args.port, args.record, args.verbose)
    print(f"mock dynatrace listening on http://127.0.0.1:{args.port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
