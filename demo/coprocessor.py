#!/usr/bin/env python3
"""
Minimal external coprocessor, stdlib-only, for the `RouterRequest` stage.

Exists to demonstrate the coprocessor path for setting a router context key,
side by side with demo/rhai/main.rhai (which does the same thing for
`tenant_id` via Rhai instead). Router config: demo/router.direct.yaml
`coprocessor:` block.

What it does: every `RouterRequest` coprocessor call gets a JSON body with
`context.entries` (see docs/dynatrace-credentials.md's sibling doc, the
Apollo coprocessor reference, for the full payload shape). This handler adds
one key, `coprocessor_tag`, and returns the *whole* entries map back —
returning only the new key would replace, not merge, the router's context
and silently drop whatever was already there (`accepts-json` etc.).

Live-verified rule this exists to demonstrate (see templates/spans.router.yaml
for the full write-up): a value set during a stage's own coprocessor/Rhai call
is invisible to `request_context` at that *same* scope — the request-time
context snapshot for `router` span attributes is taken before this hook runs.
Read it with `response_context` at `router` scope instead, or with
`request_context` from `supergraph` scope onward (router_service, which owns
this coprocessor call, has already run by then).

Usage:
    python3 demo/coprocessor.py                 # listens on :8082
    COPROCESSOR_PORT=9000 python3 demo/coprocessor.py
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("COPROCESSOR_PORT", "8082"))
TAG_VALUE = os.environ.get("COPROCESSOR_TAG_VALUE", "seen-by-copro")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quieter than the default stderr spam
        print(f"[coprocessor] {self.address_string()} {fmt % args}")

    def do_GET(self):  # readiness check for demo/up.sh
        self._send_json(200, {"status": "ok"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")

        entries = ((payload.get("context") or {}).get("entries")) or {}
        if payload.get("stage") == "RouterRequest":
            entries["coprocessor_tag"] = TAG_VALUE
            print(f"[coprocessor] RouterRequest {payload.get('id')}: "
                  f"set coprocessor_tag={TAG_VALUE!r}")

        self._send_json(200, {
            "version": payload.get("version", 1),
            "stage": payload.get("stage"),
            "control": "continue",
            "id": payload.get("id"),
            "context": {"entries": entries},
        })

    def _send_json(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[coprocessor] listening on http://127.0.0.1:{PORT}")
    server.serve_forever()
