#!/usr/bin/env python3
"""
Two minimal federated subgraphs — `products` and `orders` — with zero dependencies.

They exist for one reason: to make the router emit *real* Dynatrace telemetry.
An entity join across the two produces a supergraph span, two subgraph fetches,
and an `_entities` fetch, which is what populates the subgraph instruments and the
trace waterfall. Nothing here is a product demo; if you want a real storefront,
point the templates at your own graph.

Deliberately stdlib-only (no npm, no Docker) so `demo/up.sh` needs nothing but
python3 and a router binary.

Shape (a typical products/orders split):

    products :4011   Product @key(id) { id title price }   Query.products, Query.product
    orders   :4012   Order { id total items: [Product] }   Query.orders

Fault injection, so the error instruments have something to count:

    Query.boom                  -> GraphQL error from the products subgraph
    ?fail=1 on the subgraph URL -> HTTP 500 for the next request
    SLOW_MS=250                 -> add latency to every response

Usage:
    python3 demo/subgraphs.py                 # both subgraphs
    python3 demo/subgraphs.py --only products
"""
from __future__ import annotations

import argparse
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PRODUCTS = {
    "product:1": {"id": "product:1", "title": "Trail Runner Pro", "price": 129.0},
    "product:2": {"id": "product:2", "title": "Summit Fleece Jacket", "price": 89.5},
    "product:3": {"id": "product:3", "title": "Alpine Shell", "price": 219.0},
}

ORDERS = [
    {"id": "order:1", "total": 218.5, "items": ["product:1", "product:2"]},
    {"id": "order:2", "total": 219.0, "items": ["product:3"]},
]

SLOW_MS = int(os.environ.get("SLOW_MS", "0"))


def product_stub(product_id: str) -> dict:
    """What `orders` knows about a Product: the key only. The router fetches the
    rest from `products` — that entity fetch is the interesting span."""
    return {"__typename": "Product", "id": product_id}


class SubgraphHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    name = "products"

    def log_message(self, *args):
        pass

    # -- helpers ----------------------------------------------------------
    def _send(self, payload: dict, status: int = 200):
        if SLOW_MS:
            time.sleep(SLOW_MS / 1000)
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # Liveness probe for demo/up.sh
        self._send({"data": {"__typename": "Query"}})

    # -- resolution -------------------------------------------------------
    def do_POST(self):
        if "fail=1" in (self.path or ""):
            self._send({"errors": [{"message": f"{self.name} subgraph unavailable"}]},
                       status=500)
            return

        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send({"errors": [{"message": "invalid JSON"}]}, status=400)
            return

        query = payload.get("query") or ""
        variables = payload.get("variables") or {}
        self._send(self.resolve(query, variables))

    def resolve(self, query: str, variables: dict) -> dict:
        """Keyword-matched resolution.

        This is a stub, not a GraphQL engine: it looks for the field names the
        router could have asked for and always returns every field of the type.
        Extra fields are ignored by the router; missing ones would break it.
        """
        # Entity fetch: {"query": "...{_entities(representations:$representations){...}}"}
        if "_entities" in query:
            entities = []
            for ref in variables.get("representations", []):
                if ref.get("__typename") == "Product":
                    entities.append(PRODUCTS.get(ref.get("id"))
                                    or {"id": ref.get("id"), "title": "unknown",
                                        "price": 0.0})
                else:
                    entities.append(None)
            return {"data": {"_entities": entities}}

        if "boom" in query:
            return {"data": {"boom": None},
                    "errors": [{"message": "the products subgraph exploded",
                                "extensions": {"code": "BOOM"},
                                "path": ["boom"]}]}

        if "orders" in query:
            return {"data": {"orders": [
                {"id": o["id"], "total": o["total"],
                 "items": [product_stub(pid) for pid in o["items"]]}
                for o in ORDERS
            ]}}

        if "products" in query:
            return {"data": {"products": list(PRODUCTS.values())}}

        if "product" in query:
            product_id = variables.get("id") or "product:1"
            return {"data": {"product": PRODUCTS.get(product_id)}}

        return {"data": {"__typename": "Query"}}


def make_handler(subgraph_name: str):
    return type(f"{subgraph_name.title()}Handler", (SubgraphHandler,),
                {"name": subgraph_name})


def serve(subgraph_name: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), make_handler(subgraph_name))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--products-port", type=int,
                    default=int(os.environ.get("PRODUCTS_PORT", "4011")))
    ap.add_argument("--orders-port", type=int,
                    default=int(os.environ.get("ORDERS_PORT", "4012")))
    ap.add_argument("--only", choices=("products", "orders"),
                    help="run just one subgraph")
    args = ap.parse_args()

    wanted = [("products", args.products_port), ("orders", args.orders_port)]
    if args.only:
        wanted = [(n, p) for n, p in wanted if n == args.only]

    servers = []
    for name, port in wanted:
        httpd = serve(name, port)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        servers.append(httpd)
        print(f"{name} subgraph on http://127.0.0.1:{port}/", flush=True)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for httpd in servers:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    main()
