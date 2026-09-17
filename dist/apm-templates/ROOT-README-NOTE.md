# Note: root README change for the apm-templates PR

The root `README.md` of `apollographql/apm-templates` currently has no explicit provider list —
it says each template lives in a subdirectory named for its APM provider. Two options:

**Option A (minimal, matches current structure):** no root README change needed at all; the
`dynatrace/` directory is self-describing, like `datadog/`, `grafana/`, and `newrelic/`.

**Option B (add a provider list):** if a list is wanted, add this after the "Each template is
provided within a subdirectory..." paragraph:

```markdown
Available templates:

- [Datadog](./datadog/README.md)
- [Dynatrace](./dynatrace/README.md)
- [Grafana](./grafana/README.md)
- [New Relic](./newrelic/README.md)
```

If only a single line is being added to an existing list, use:

```markdown
- [Dynatrace](./dynatrace/README.md)
```

## CI reminders

- The repo uses [`mise`](https://mise.jdx.dev/) for tooling: run `mise trust`, `mise install`,
  then `mise pr-all` (markdown lint + spell check) before pushing. `mise fix-spelling` and
  `mise format-markdown` fix most failures.
- `dynatrace/dql-queries.md` and `dynatrace/README.md` contain DQL keywords and metric names that
  the spell checker will likely flag and that need exceptions, e.g.: `timeseries`,
  `makeTimeseries`, `fieldsAdd`, `countIf`, `arrayAvg`, `arraySum`, `isNotNull`, `loglevel`,
  `otlphttp`, `cumulativetodelta`, `filelog`, `dt0c01`, `dt0s16`, `dt0s02`, `subgraph`,
  `subgraphs`, `supergraph`, `coprocessor`, `Grail`, `DQL`, `OTLP`, `UpDownCounter`.
- The markdown linter typically requires blank lines before lists and tables, and language tags on
  fenced code blocks — the files in `dynatrace/` follow that, but re-check after any edits.
