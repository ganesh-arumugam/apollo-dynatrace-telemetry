# PR notes: Dynatrace overview + dashboard-template pages

## Target paths in apollographql/router

- `docs/source/routing/observability/router-telemetry-otel/apm-guides/dynatrace/index.mdx` ← `index.mdx` here (new)
- `docs/source/routing/observability/router-telemetry-otel/apm-guides/dynatrace/dashboard-template.mdx` ← `dashboard-template.mdx` here (new)
- `docs/source/_sidebar.yaml` ← replace the Dynatrace block per `sidebar-block.yaml`

Path choice: Datadog nests its dashboard page under `datadog/observing-and-monitoring/dashboard-template`. With only one page in that bucket for Dynatrace, a flat `dynatrace/dashboard-template` is simpler. If the docs team prefers strict parity, move the file to `dynatrace/observing-and-monitoring/dashboard-template.mdx` and update the href in the sidebar block and the two links to it (from `index.mdx` and from the cross-links below).

## Cross-links to add to existing pages

Both edits follow the pattern of the Datadog sub-pages, which link back to their overview and forward to next steps.

`dynatrace-metrics.mdx`:
- After the opening paragraph ("This metrics exporter is a configuration of the OTLP exporter..."), add: "For an overview of all connection methods and Dynatrace's ingest requirements, see the [Dynatrace integration overview](/graphos/routing/observability/router-telemetry-otel/apm-guides/dynatrace)."
- At the end of the page (after the final "For more details about Dynatrace configuration..." paragraph), add a "Next steps" line linking to the [dashboard template](/graphos/routing/observability/router-telemetry-otel/apm-guides/dynatrace/dashboard-template).

`dynatrace-traces.mdx`:
- Same two edits, same locations.

Optional cleanup while touching these files: both pages share the identical `title: Dynatrace configuration of OTLP exporter`; consider disambiguating (e.g., "...OTLP metrics exporter" / "...OTLP trace exporter") since the subtitle already does. Also, `dynatrace-traces.mdx` writes "`Api-token`" once (lowercase t); the header value is `Api-Token`.

## Front-matter conventions observed (router repo, main branch)

- Required in practice: `title`, `subtitle`, `description`.
- APM/telemetry pages carry `context:` with the list item `- telemetry` (both existing Dynatrace pages have it; used on both new pages).
- `redirectFrom:` list only when a page moved; the new pages need none.
- Known bug not copied: `datadog/observing-and-monitoring/dashboard-template.mdx` has a stray `  - telemetry` directly under `description:` with no `context:` key — malformed YAML that the new pages fix by using the proper `context:` key.
- Style observed and matched: sentence-case H2/H3 (except product nouns), `<Note>` / `<Caution>` with blank lines inside the tags, ```yaml title="router.yaml"``` code fences, root-relative doc links (`/graphos/routing/...`), numbered lists using `1.` repeated (Datadog index) — plain incrementing numbers also appear; either passes.

## Not verified

- **apm-templates `dynatrace/` directory does not exist yet** (repo listing was unreachable from this environment; as of the repo work preceding this draft, only `datadog/` is published). The dashboard-template page's "Get the template" and import-script references assume the apm-templates PR lands first with: dashboard JSON, `import_dashboard.sh`, the three router YAMLs, and `dql-queries.md` (scope decision 2026-08-17: `tiles.yaml`/`build_dashboard.py` stay canonical in the private source repo; consumers re-target `service.name` via find-and-replace). Gate this docs PR on that one.
- **Screenshot**: the Datadog dashboard page opens with `<img className="screenshot" src=".../images/apm-templates/datadog.jpg" />`. The Dynatrace page omits it; add `docs/source/images/apm-templates/dynatrace.jpg` and the matching `<img>` (six levels up becomes four with the flat path: `../../../../images/apm-templates/dynatrace.jpg`) once a tenant screenshot exists — pending live-tenant validation.
- **Sidebar**: verified against `docs/source/_sidebar.yaml` on `main` (fetched 2026-08-17). If the PR targets `dev`, re-check the surrounding block; the Dynatrace entry currently has only Metrics/Traces children on `main`.
- **Documents API URL**: the import endpoint host (`{env-id}.apps.dynatrace.com/platform/document/v1/documents`) matches the repo's import script but was not re-verified against current Dynatrace docs.

## Content sources

Everything technical is distilled from the repo (`/Users/ganesharumugam/Documents/gitRepos/apollo-dynatrace-telemetry`): README, `docs/datadog-parity.md`, `docs/percentiles-and-buckets.md`, `templates/dynatrace.router.yaml`, `templates/dynatrace-activegate.router.yaml`, `templates/spans.router.yaml`, `templates/instruments.router.yaml`, `dashboards/tiles.yaml`. The minimal `router.yaml` in `index.mdx` is `templates/dynatrace.router.yaml` trimmed to exporter essentials (batch_processor tuning and resource attributes dropped for brevity); the spans snippet in `dashboard-template.mdx` is the supergraph error-marking block from `templates/spans.router.yaml`.
