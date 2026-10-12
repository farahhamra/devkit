# Devkit 0.6.0

- Added consistent measured `coverage` to all source-analysis commands: status, per-stage progress, unique input counts, typed gaps/exclusions, selected scope, app availability and module-check selection. Help/doctor/config/feedback/agent sync do not claim source coverage.
- Distinguished `complete`, `partial` and `not_analyzed` from exit codes, check findings and output truncation. Text output includes a coverage summary; JSON adds metadata without changing existing data shapes or envelope version.
- Counted unreadable/binary/non-UTF-8 inputs, traversal errors, missing configured roots/schemas, parser errors, unresolved local/configured-alias imports, observed dynamic imports/routes and search early termination. Unreadable schemas no longer become silently empty schemas.
- Preserved coverage summaries under output budgets and reported omitted details explicitly. Excluded directories are never entered to calculate totals or percentages. Targeted symbols remain targeted.
- Made parser/dynamic-import coverage consistent on cold, warm and uncached runs. Incremented disposable source-cache format to `2`; old summaries rebuild automatically, without resetting feedback or agent-sync backups.
- Updated bundled agent guidance, usage documentation, regression tests and installed-package smoke checks.

See [coverage semantics](../guides/COVERAGE.md). `complete` means processing selected eligible inputs with supported static capabilities, not complete project understanding, compiler validation or runtime/business correctness. Existing exit-code meanings remain unchanged; no strict mode, repair, preference synchronization or new analysis command is added.
