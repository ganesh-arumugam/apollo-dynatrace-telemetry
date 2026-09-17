# Screenshots

Captures of the imported dashboard against a live tenant, referenced from the
main README. To refresh them, import the dashboard, run traffic through the
router, and capture:

- `dashboard-full.png` — the whole dashboard, scrolled to fit or stitched
- `traffic.png` — one tile from the **Traffic** section (Requests by HTTP Status
  Code is the best single tile)
- `latency-spans.png` — one tile from **Latency & overhead** showing the
  span-based percentiles (Router Latency — p50 / p95 / p99)
- `errors.png` — one tile from **Errors** (GraphQL Errors by Code)
- `cache.png` — one tile from **Cache & query planning** (Cache Hits vs Misses)
