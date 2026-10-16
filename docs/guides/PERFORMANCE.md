# Database performance evidence

```powershell
devkit performance database --input diagnostics/performance/capture.json --app erp --json
devkit performance database --input diagnostics/performance/capture.json --app erp --strict --json
devkit help performance database --json
```

This command analyzes an **existing versioned capture**, not application performance from source alone. It requires no additional CLI, database driver or optional extra. It does not connect to MySQL, execute SQL (including EXPLAIN), start applications, instrument code, collect profiles, generate workloads or apply optimizations. Capture creation is a separate, explicitly authorized activity. Separate [runtime, memory, cache and comparison commands](PERFORMANCE-RUNTIME.md) support Node/V8 evidence and compatible before/after captures. Browser and .NET Manager profiling remain unsupported; `performance measure` and `perf` are not commands.

## Inputs and boundaries

`--input` is required and project-root relative, independent of the current directory. For `.agents/devkit` installations, use the actual ElectroERP `--root` and, if needed, `--config .agents/devkit/electroerp.yaml`. The capture is auxiliary evidence and may live outside app source directories, but must stay inside the project root. Its `metadata.app` must be `erp` or `hub` and match an explicit `--app`; `--app all` still analyzes only the capture's one app. Schema/module discovery uses the configured scope. Hub needs its real roots/schema configured.

Ignored parents/files, internal feedback/sync storage, escapes, symlinks/junctions, devices and FIFOs are refused. Supplying an explicit input does not bypass `.ignore`. Store only sanitized captures in a dedicated folder excluded from **Git**, but not Devkit's `.ignore` if you want Devkit to read it. Existing source analysis may initialize a missing `.ignore`; this command does not write captures, reports, caches or application files. Capture-supplied source references are also subject to app/ignore boundaries.

Limits: 16 MiB UTF-8 JSON (BOM accepted), 250,000 JSON value nodes, nesting depth 64, at most 20,000 records per array. Referenced source verification is bounded to 4 MiB/file and 16 MiB total. Source budgets/unavailable content produce gaps, not fabricated locations. No external reference files/URLs are loaded from the capture.

Duplicate JSON keys, unexpected contract fields, invalid types/IDs, NaN/infinity, inconsistent durations/counts, duplicate event IDs and cyclic parent links are rejected. Errors do not echo submitted field names or values. Unsupported native plan formats return partial usable reports rather than pretending to understand them.

The [synthetic example](../../examples/performance-capture.json) illustrates the format. It is **not a measurement of ElectroERP** and intentionally lacks source attribution and a complete index snapshot; `--strict` reports these gaps. Replace its data with real exported evidence, not invented measurements.

## Capture contract 1.0

The capture version is separate from Devkit's output envelope (`schema_version: "1.0"`). Unknown fields outside the native plan document are rejected, including raw `sql`, `params`, result rows, arbitrary notes and connection strings. Optional fields below may be omitted; only explicitly nullable fields accept `null`.

### Top level and metadata

| Field | Contract |
| --- | --- |
| `capture_version` | Required string `"1.0"` |
| `metadata` | Required object described below |
| `queries` | Required array; empty is incomplete evidence, not a clean performance result |
| `requests` | Optional request samples, default empty |
| `indexes` | Optional normalized database/migration index records, default empty |
| `indexes_complete` | Optional boolean, default false; true asserts that the exported database inventory covers **all tables referenced in this capture**, including functional/constraint indexes. Devkit cannot verify this assertion against a server. |

Required metadata:

- `app`: `erp` or `hub`.
- `captured_at`: ISO timestamp with timezone, e.g. `2026-10-14T10:00:00Z`.
- `database`: exactly `engine` (`mysql`, `mariadb`, `unknown`) and `version` (actual version string or null). A documented minimum requirement is not the deployed version.
- `workload`: `name` (non-sensitive identifier) and `duration_ms` (nonnegative number or null); optional `concurrency` (positive integer or null), `warmup_ms` (nonnegative number or null).

Optional metadata: `source_revision` (40/64 lowercase hex characters or null); `runtime` with optional `node` and `prisma_client` version strings. These are reported capture metadata, not independently verified. Names/IDs use ASCII letters/digits plus `_ . : -`, start with an alphanumeric character, and are at most 128 characters. Database identifiers start with a letter/underscore and additionally allow `$`. Version strings begin with a digit and are at most 64 characters. These restrictions reduce accidental raw-data inclusion but are not a guarantee that labels contain no secrets.

Optional `environment`, `process_scope` and `collector` are non-sensitive identifiers using the same name format. They enable `performance compare` without breaking existing 1.0 captures. Comparison requires these fields, actual matching `runtime.node` and database versions, and matching workload name/concurrency/warmup. Query streams match by kind, timing source and fingerprint; SQL and Prisma operation timings stay separate. Missing metadata prevents numeric comparison. Database comparison only compares recorded timing/count streams; it does not repeat schema, plan or index analysis.

### Query groups

Each group requires:

- `id`: unique capture-local ID.
- `kind`: `sql` (physical statement observations) or `prisma` (logical client operations).
- `fingerprint`: 64 lowercase hex characters. For SQL, use a database-provided digest or SHA-256 of a properly normalized, literal-free query structure. Do not hash parameter values or use table name alone as a query fingerprint. For Prisma, hash a normalized operation shape without values. The same fingerprint cannot appear twice within the same `kind`/timing-source stream; merge disjoint records first.
- `operation`: SQL `select|insert|update|delete|begin|commit|rollback|other`, or Prisma `findUnique|findUniqueOrThrow|findFirst|findFirstOrThrow|findMany|create|createMany|update|updateMany|delete|deleteMany|upsert|count|aggregate|groupBy|executeRaw|queryRaw|transaction`.
- `tables`: exact-case physical table names, not aliases; empty is allowed but mapping is unknown. Optional `model` is a Prisma model identifier and is checked against resolved mappings.
- `timing`: `source`, `count` and `total_ms`. Sources for SQL: `mysql_digest`, `mysql_slow_log`, `prisma_query_event`, `unknown`. Sources for Prisma: `prisma_operation`, `unknown`. Count is a positive integer up to one billion; `total_ms` is a nonnegative number or null.

Optional timing fields: `min_ms`, `max_ms`, `rows_examined`, `rows_sent` (all nullable; row counts are nonnegative integers). These are totals/bounds for **the same group and capture interval**, not per-request metrics. `mean_ms` is derived only when a total is available. Timings must agree with supplied sample bounds and totals. Numeric values are bounded to 10^15 unless a smaller bound is stated.

Optional query fields: `samples`, `plan`, `source`.

### Samples and requests

A query's `samples` are a subset of its counted events, never additional events. Each requires:

- `id`: globally unique event ID within the capture.
- `duration_ms`: nonnegative elapsed duration.
- `phase`: `request`, `detached`, `background` or `unknown`.
- `correlation`: `explicit` or `unknown`. Explicit means the exporter established the relationship; IDs alone are not proof.
- Optional nullable `request_id` and `parent_id` (another sample ID). Parents may belong to a different query group. Missing referenced events/requests are gaps; cycles and contradictory parent/child request IDs are invalid.

Requests require `id`, `duration_ms`, `outcome` (`finished` or `aborted`). Optional `route_template` is a normalized route such as `/products/:id`, **not** a URL with query strings or user identifiers. Request duration is the collector's server response lifecycle measurement, not TTFB, total client download time or the sum of its query durations.

Repetition findings count only SQL samples with explicit correlation, phase `request`, and an existing captured request, grouped by request, timing source and fingerprint. Two or more observations produce a review candidate, not proof of N+1. Detached/background/unknown work is not included. Sample phase counts are reported even when attribution is unknown; they are exporter labels, not verified behavior. Samples and parent IDs remain available to the agent subject to output limits.

No percentiles are synthesized from averages, and no overlapping logical-operation/SQL/request times are summed. Rankings are by recorded total duration **within each kind/timing-source stream**, with missing totals unranked. These are workload-cost rankings, not universal slow-query thresholds. Counters from different collectors may overlap and must not be combined as unique physical statement totals.

### Native query plans

An SQL group's optional `plan` has required `format` and `document`, plus optional `table_map`:

```json
{
  "format": "mysql_explain_json",
  "table_map": {"p": "products"},
  "document": {
    "query_block": {
      "table": {"table_name": "p", "access_type": "ref", "key": "idx_org", "used_key_parts": ["organization_id"], "rows_examined_per_scan": 100, "filtered": 100}
    }
  }
}
```

Supported: MySQL **8.0/8.4** `EXPLAIN FORMAT=JSON` documents with the `query_block` shape. Version/engine must be known. MariaDB, unknown versions, other major/minor versions, TREE and EXPLAIN ANALYZE formats are not interpreted. A logical Prisma operation can emit several SQL statements; its plan must be attached to the appropriate SQL group, not the logical operation.

Aliases are not guessed. `table_map` is the exporter's verified alias-to-physical-table mapping, whose targets must be in `query.tables`. Derived tables/unresolved aliases remain unknown. Output projects selected identifiers, access type, estimated row/filter values and sort/temporary-table flags. Raw conditions, SQL literals and optimizer free text are never echoed. Sanitize plan documents before saving/sharing them: hiding fields in a report does not remove them from the input file.

Full scans and sort/temporary-table operations are review candidates, not automatic defects or proof of disk spill. Estimated rows are not observed rows. A scan may be the best plan. This release does not invent a composite-index column order from a scan, treat optimizer costs as milliseconds, or claim pool wait/lock duration from query timing.

### Index inventory

Each normalized index requires `table`, `name`, `provenance` (`database` or `migration`), `unique` (boolean), `primary` (boolean), and a nonempty `parts` array. Primary indexes must also be unique. Optional: `constraint` (boolean or null, default unknown), `visible` (boolean or null), `method` (`btree|hash|fulltext|spatial|unknown`), `source`.

An index part contains either `column` (physical column identifier) **or** `expression: true`; do not copy expression text. Optional `order` is `asc|desc` and `prefix_length` is an integer 1–65535 or null. Preserve order, prefix lengths, visibility, uniqueness and constraint dependencies during export. Migration-sourced declarations are not deployed-index proof.

Devkit also lists current Prisma `@@index`/`@@fulltext`/`@@unique`/`@@id` and field `@id`/`@unique` declarations, including mapped physical columns and source ranges. Functional indexes may exist only in migrations/database exports. No automatic migration SQL parsing or schema-to-live drift claim is made.

Equivalent-index candidates require matching full part definitions for visible database-reported BTREE indexes, explicit non-constraint status, and known part order. Unique/primary/functional indexes and indexes with unknown constraint status are protected. Foreign-key-supporting indexes must be marked constraint-related. Prefix-only overlap, workload benefit and safe deletion are not inferred. No CREATE/DROP statements or automatic fixes are generated.

### Source attribution

Optional query/index `source` has exactly `path`, `sha256`, `line`, `end_line`. Paths are project-relative (Windows separators accepted), spans are inclusive and 1-based, and SHA-256 covers the **raw source bytes**, including BOM/CRLF if present. Source must belong to the capture app. Use original TypeScript source locations only when the collector has valid mappings for that build.

Matching content hash and valid bounds produce `location_status: content_verified`, with `attribution: capture_supplied_not_runtime_verified`. This verifies the file, not that the reported query executed there. Missing/ignored/stale/out-of-range/over-budget evidence emits no claimed line range and produces a gap. Module/table/schema locations are explicitly current static context, not executing-call attribution. `source_revision` alone is not sufficient to verify a file, and a table-name match never fabricates a call location.

## Opt-in collection integration (separate app change)

There is no capture command in this release. The app agent or developer needs to export the contract above after separately authorized measurement. A safe first implementation for ElectroERP should follow these boundaries:

1. Use a local/staging environment and controlled test data. Record actual runtime/Prisma/server versions, build revision, workload window and concurrency. ERP and Hub currently resolve Prisma/client 5.22.0 according to the investigation, but deployed versions must be checked. Do not silently use production or alter logging.
2. Add request IDs and monotonic response-lifecycle timing at a documented middleware boundary. Decide whether parsing/rate-limiting are inside it. Handle `finish`/premature `close` once, record only normalized route templates, and never capture headers, IPs, user agents, bodies or account identifiers.
3. Time Prisma logical operations separately from SQL events. For Prisma 5.22.0, middleware ordering changes what an outer `await next(params)` includes. Test nested middleware calls and interactive transactions with that exact version; do not assume a generic wrapper proves every SQL attribution.
4. SQL timing can come from Prisma event logging or MySQL exports. Merely reading AsyncLocalStorage inside a Prisma query event callback does **not** prove request correlation survives the engine boundary. If the collector cannot establish a relationship, use `correlation: unknown` and omit IDs. Do not enable both raw stdout SQL logging and an exporter as an accidental privacy side effect.
5. Audit pre-reads apply to update/delete variants, not create/createMany. Detached audit inserts and notification evaluation require distinct child events/phases and can outlive the response. They are conditional on authenticated context and successful writes. Outer-client calls must not be assumed to share an interactive transaction or roll back with it. Test these cases before claiming complete capture; do not change audit semantics to make profiling easier.
6. Keep export bounded, opt-in, local and fail-safe. Do not retain event payloads indefinitely or affect query outcomes when the collector fails. Record missing/dropped samples by keeping true aggregate counts larger than the sample list; never silently label a sampled trace complete. Record failed/aborted operations consistently within the declared timing boundary.
7. Disable/remove capture integration after the session as appropriate. Store sanitized artifacts outside version control, never automatically upload them, and run Devkit separately against the exported JSON.

For MySQL `performance_schema` digest summaries, `COUNT_STAR` maps to `count`, `SUM_TIMER_WAIT` (picoseconds) converts to milliseconds by division by **1,000,000,000**, and `SUM_ROWS_EXAMINED`/`SUM_ROWS_SENT` map to row totals. Prefer before/after deltas for the same server/interval with no restart/reset; detect resets. Do not subtract MIN/MAX counters to derive interval minima/maxima—omit them unless genuinely known. Digest summaries contain no per-request events: leave samples absent and expect correlation gaps. Slow-log `Query_time` seconds require conversion to milliseconds. Prisma query event durations are milliseconds. Do not infer unmeasured rows, pool queue time, lock duration or request percentiles.

Any acquisition of query plans/index metadata is separately authorized and must match the workload/database snapshot. Even diagnostic SQL can require privileges or impose load. **EXPLAIN ANALYZE executes the query**; do not run it as an assumed read-only capture step. This analyzer never executes EXPLAIN of any kind.

## Output, coverage and validation

The existing JSON envelope contains `data.performance: database`, metadata, query groups, request samples, index inventories, findings, unknowns, limitations and an untruncated summary. `coverage.stages.performance` and selected/analyzed query-group counts are added only for this command. Schema/module discovery is performed; general source parsing/dependency analysis is not claimed. Content hash verification is not AST analysis.

`--strict` gates incomplete coverage/evidence, **not** slow queries. A complete report can contain scan/repetition candidates. An aggregate-only capture will intentionally be partial because request events/plans/source attribution may be missing. Empty captures are not clean results. Missing tables/schema/index inventories, unsupported plans, unresolved parents/requests, unknown timing sources and stale source references remain visible. No universal latency budget is imposed.

- Exit `0`: report generated; unknowns/findings may remain.
- Exit `1`: `--strict` found incomplete coverage.
- Exit `2`: invalid/unsafe capture, configuration or I/O failure.

`--max-results` and `--max-output-chars` bound presentation, not analysis. Summary counts and coverage survive truncation; linked parent events or findings may be omitted from the display. `--no-cache` is accepted; capture evidence is always read live and is not persisted in source caches. Templates describe the capability, not a requirement to use it.

Tests use synthetic captures, not real application benchmarks: timing/correlation isolation, stale source hashes, plan formats/aliases, protected indexes, malformed input, symlinks/FIFOs/ignore policy, UTF-8/Windows paths, bounds, strict coverage and installed-wheel behavior. Passing Devkit tests does not validate ElectroERP's runtime or instrumentation.
