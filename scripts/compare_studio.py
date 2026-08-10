#!/usr/bin/env python3
"""Compare GraphOS Studio and Dynatrace for the same traffic, side by side.

Both describe the same router. This pulls each side for one window and prints
them together, so a disagreement is visible rather than argued about.

Latency is read from **spans**, not from the duration histogram: `percentile()`
on a metric needs a `rollup:`, and rollup collapses each slot to one value before
the percentile is taken, which returns the average. See
docs/studio-vs-dynatrace-latency.md.

Requires the router to report to both at once:
    APOLLO_KEY + APOLLO_GRAPH_REF      -> GraphOS usage reporting
    telemetry.exporters.*              -> Dynatrace OTLP

Env (read from .env when not exported):
    APOLLO_KEY           graph API key (service:...)
    APOLLO_GRAPH_REF     e.g. my-graph@current   (or APOLLO_GRAPH_ID)
    DT_ENVIRONMENT_ID    e.g. abc12345
    DT_BEARER_TOKEN      platform token (dt0s16...) with storage read

Usage:
    python3 scripts/compare_studio.py                 # last complete hour
    python3 scripts/compare_studio.py --hours-ago 3
    python3 scripts/compare_studio.py --service-name my-supergraph
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STUDIO_API = "https://api.apollographql.com/api/graphql"

STUDIO_QUERY = """query Compare($id: ID!, $from: Timestamp!, $to: Timestamp!) {
  graph(id: $id) {
    operationInsightsTimeseriesReport(
      dimensions: [OPERATION_NAME],
      metrics: [REQUEST_COUNT, REQUEST_LATENCY_P50_MS, REQUEST_LATENCY_P90_MS,
                REQUEST_LATENCY_P99_MS, REQUEST_WITH_ERROR_COUNT],
      resolution: HOUR, from: $from, to: $to, limit: 200) { csv }
  }
}"""


def load_env() -> None:
    path = os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        return
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def need(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        sys.exit(f"ERROR: {name} is not set (see the header of this script)")
    return value


def post_json(url: str, payload: dict, headers: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST")
    for key, value in headers.items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:400]
        sys.exit(f"ERROR: {url} returned HTTP {exc.code}\n  {body}")


# ── Studio ───────────────────────────────────────────────────────────────────
def studio_rows(graph_id: str, start: dt.datetime, end: dt.datetime) -> dict:
    body = post_json(
        STUDIO_API,
        {"query": STUDIO_QUERY,
         # Timestamp accepts ISO 8601 (or a negative relative-seconds string).
         # Epoch seconds are not interpreted as absolute.
         "variables": {"id": graph_id,
                       "from": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "to": end.strftime("%Y-%m-%dT%H:%M:%SZ")}},
        {"Content-Type": "application/json",
         "X-API-KEY": need("APOLLO_KEY"),
         # Both of these are mandatory; the API rejects the request without them.
         "apollographql-client-name": "dynatrace-comparison",
         "apollographql-client-version": "1.0"})
    if "errors" in body:
        message = body["errors"][0].get("message", "")
        if "Rate limit" in message:
            sys.exit("ERROR from Studio: rate limited (the insights API allows only a "
                     "few requests per minute). Wait a moment and re-run.")
        if "must not exceed 7 days" in message:
            sys.exit("ERROR from Studio: --hours-ago must be under 168 (HOUR "
                     "resolution is capped at 7 days).")
        sys.exit("ERROR from Studio: " + message[:300])
    report = (body["data"]["graph"] or {}).get("operationInsightsTimeseriesReport")
    if not report:
        sys.exit("ERROR: no insights report returned — check APOLLO_GRAPH_REF")

    out: dict[str, dict] = {}
    for row in csv.DictReader(io.StringIO(report["csv"])):
        name = row["operation name"]
        acc = out.setdefault(name, {"n": 0.0, "err": 0.0,
                                    "p50": 0.0, "p90": 0.0, "p99": 0.0})
        n = float(row["request count"])
        acc["n"] += n
        acc["err"] += float(row["request with error count"])
        # Weight percentiles by request count when a window spans several buckets.
        for key, column in (("p50", "request latency p50 ms"),
                            ("p90", "request latency p90 ms"),
                            ("p99", "request latency p99 ms")):
            acc[key] += float(row[column]) * n
    for acc in out.values():
        if acc["n"]:
            for key in ("p50", "p90", "p99"):
                acc[key] /= acc["n"]
    return out


# ── Dynatrace ────────────────────────────────────────────────────────────────
def dql(env_id: str, token: str, query: str) -> list[dict]:
    base = f"https://{env_id}.apps.dynatrace.com/platform/storage/query/v1"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    started = post_json(f"{base}/query:execute", {"query": query}, headers)
    request_token = started.get("requestToken")
    if not request_token:
        sys.exit(f"ERROR: Dynatrace did not accept the query: {started}")
    poll = f"{base}/query:poll?" + urllib.parse.urlencode(
        {"request-token": request_token})
    for _ in range(30):
        req = urllib.request.Request(poll)
        req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=90) as resp:
            out = json.loads(resp.read())
        if out.get("state") == "SUCCEEDED":
            return (out.get("result") or {}).get("records") or []
        time.sleep(3)
    sys.exit("ERROR: Dynatrace query timed out")


def strip_operation_type(span_name: str) -> str:
    for prefix in ("query ", "mutation ", "subscription "):
        if span_name.startswith(prefix):
            return span_name[len(prefix):]
    return span_name


def dynatrace_rows(env_id: str, token: str, service: str,
                   start: dt.datetime, end: dt.datetime) -> tuple[dict, dict]:
    window = (f'from: "{start.strftime("%Y-%m-%dT%H:%M:%SZ")}", '
              f'to: "{end.strftime("%Y-%m-%dT%H:%M:%SZ")}"')
    per_op = dql(env_id, token, f"""
        fetch spans, {window}
        | filter {service} and request.is_root_span == true
        | summarize n = count(),
                    p50 = percentile(duration, 50) / 1000000.0,
                    p90 = percentile(duration, 90) / 1000000.0,
                    p99 = percentile(duration, 99) / 1000000.0,
              by: {{span.name}}
    """)
    ops = {strip_operation_type(r["span.name"]): {
               "n": float(r["n"]), "p50": float(r["p50"]),
               "p90": float(r["p90"]), "p99": float(r["p99"])}
           for r in per_op}

    totals: dict[str, float | None] = {}
    for label, metric in (("requests", "dynatrace.router.requests"),
                          ("graphql_errors", "apollo.router.graphql_error"),
                          ("server_5xx", "dynatrace.router.server.errors")):
        recs = dql(env_id, token,
                   f"timeseries n = sum({metric}, scalar: true), "
                   f"filter: {{{service}}}, {window}")
        totals[label] = float(recs[0]["n"]) if recs and recs[0].get("n") is not None else None
    return ops, totals


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours-ago", type=int, default=1,
                    help="how many complete hours back to compare (default 1)")
    ap.add_argument("--service-name", default="apollo-router",
                    help="service.name your router reports (default: apollo-router)")
    args = ap.parse_args(argv)

    load_env()
    graph_id = os.environ.get("APOLLO_GRAPH_ID") or need("APOLLO_GRAPH_REF").split("@")[0]
    env_id, token = need("DT_ENVIRONMENT_ID"), need("DT_BEARER_TOKEN")
    service = f'service.name == "{args.service_name}"'

    end = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - dt.timedelta(hours=args.hours_ago)

    print(f"window   {start:%Y-%m-%dT%H:%M}Z .. {end:%Y-%m-%dT%H:%M}Z")
    print(f"graph    {graph_id}")
    print(f"service  {args.service_name}\n")

    studio = studio_rows(graph_id, start, end)
    ops, totals = dynatrace_rows(env_id, token, service, start, end)

    print("Per operation — count, then latency in ms (S = Studio, D = Dynatrace spans)")
    print(f"  {'operation':<26} {'n S/D':>11}  {'p50 S/D':>15}  "
          f"{'p90 S/D':>15}  {'p99 S/D':>15}")
    for name in sorted(set(studio) | set(ops)):
        s, d = studio.get(name), ops.get(name)
        def pair(key, fmt="{:.0f}"):
            left = fmt.format(s[key]) if s else "-"
            right = fmt.format(d[key]) if d else "-"
            return f"{left}/{right}"
        print(f"  {name:<26} {pair('n'):>11}  {pair('p50'):>15}  "
              f"{pair('p90'):>15}  {pair('p99'):>15}")

    studio_requests = sum(v["n"] for v in studio.values())
    studio_errors = sum(v["err"] for v in studio.values())
    print("\nTotals")
    print(f"  requests           Studio {studio_requests:>8.0f}   "
          f"Dynatrace {fmt_or_dash(totals['requests'])}")
    print(f"  errors             Studio {studio_errors:>8.0f}   "
          f"apollo.router.graphql_error {fmt_or_dash(totals['graphql_errors'])}")
    print(f"  5xx                          {'':>8}   "
          f"dynatrace.router.server.errors {fmt_or_dash(totals['server_5xx'])}")

    print("\nReading this:")
    print("  - per-operation counts should match exactly")
    print("  - Studio-only rows mean another router reports to this graph variant:")
    print("    Studio aggregates every one of them, this Dynatrace side is filtered")
    print("    to a single service.name, so the TOTALS only match when one router")
    print("    feeds the graph")
    print("  - Studio's '# GraphQLValidationFailure' is the same traffic Dynatrace")
    print("    records as 'GraphQL Operation' spans — a rejected operation has no")
    print("    name to report, and each side labels it differently")
    print("  - Studio errors match apollo.router.graphql_error, NOT the 5xx counter:")
    print("    GraphQL errors return HTTP 200 and validation failures 400")
    print("  - Studio exposes p50/p90/p99 only; there is no p95 to compare against")
    print("  - p50 should agree within ~1%; tails diverge more at low sample counts")
    return 0


def fmt_or_dash(value) -> str:
    return f"{value:>8.0f}" if value is not None else f"{'no series':>8}"


if __name__ == "__main__":
    sys.exit(main())
