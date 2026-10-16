# Consistent analysis coverage (v0.6.0)

Coverage is automatic, not a new command. Every invocation of `search`, `find`, `tree`, `symbols`, `analyze`, `query`, `module`, `schema`, `trace` or `impact` includes a top-level `coverage` object in JSON. Human-readable analysis prints a compact summary to stderr. Existing `data` shapes and envelope `schema_version: "1.0"` remain unchanged. Help, diagnostics, feedback and agent synchronization do not claim source-analysis coverage. Structural queries additionally report `stages.query`, `counts.query_selected` and `counts.query_analyzed`, with ast-grep version/grammar details. These fields are query-specific; parser coverage and query processing are measured separately.

## Interpret the three independent signals

`performance database` also returns coverage, with command-specific `stages.performance`, `performance_selected` and `performance_analyzed` query-group counts. Missing timing/correlation/plans/index inventories, unsupported plans and stale source ranges remain gaps. Its `--strict` gates incomplete evidence, not measured slowness. Source hash verification is not AST analysis, and offline capture processing is not runtime verification. See the [performance capture guide](PERFORMANCE.md).

`performance runtime`, `memory`, `cache` and `compare` use the same performance stage/count fields for processed capture units (CPU/heap nodes, process samples, cache entries, or comparison metrics). They do not run or claim schema/source/dependency scans. Missing companion measurements/metadata, partial windows, counter resets, unavailable dominators and incompatible/unmatched comparison metrics remain explicit gaps. Native V8 inputs without metadata are partial. `--strict` gates incompleteness; comparison's separate `--fail-on-regression` gates measured candidates, not significance. See the [extended performance guide](PERFORMANCE-RUNTIME.md).

1. **Exit code / findings:** did the operation succeed, find failing module checks, or fail to execute?
2. **Coverage:** what inputs and stages were actually processed, and which known gaps remain?
3. **Output truncation:** how much of the computed result and metadata is visible?

Exit `0` is not proof of complete coverage. Existing discovery commands retain their check/error exit semantics. Audit commands additionally offer `--strict`, which returns `1` for incomplete coverage; see the [audit guide](AUDIT.md). Exit `2` keeps the usual error object and adds measured coverage for recognized analysis invocations, including setup failures. Argument-parser errors before a command can be selected have no coverage. No automatic repair is performed.

| Coverage status | Meaning |
| --- | --- |
| `complete` | Processing completed for selected eligible inputs under this command's supported capabilities, with no recorded processing gaps. Findings may still fail checks. |
| `partial` | Processing started, but a known gap or aborted stage prevents claiming full processing of that scope. Usable results can still be returned. |
| `not_analyzed` | No processing started, or a command-required stage had no analyzable inputs. An unsupported single-file symbol query is an example. |

`complete` never means a full runtime call graph, type safety, valid Prisma semantics, correct business rules, or knowledge of excluded files/unconfigured apps. A supported-language parser can process every selected file while still being unable to infer dynamic dispatch, type identity, custom framework wrappers or unknown aliases. Those capability limits remain in force.

## Stable fields

`coverage.version` is `"1.0"`. These summary fields survive both result-count limits and the overall output budget:

- `status` and `stages`: discovery, text reading, source parsing, schema parsing and dependency resolution. Each stage is `not_run`, `not_analyzed`, `partial` or `complete`. `find` and `tree` only enumerate paths; schema and source stages are not silently called complete. Targeted symbols do not resolve the repository's dependency graph.
- `counts`: unique eligible `files` and encountered `directories`; successfully decoded `text_read` and attempted-but-skipped `text_skipped`; `source_selected`, `source_analyzed`, `source_partial`; `schema_selected`, `schema_analyzed`; and `modules_checked`, `modules_unavailable` for module checks. Categories overlap and must not be summed as disjoint files. `source_analyzed` includes partially parsed files; `source_partial` identifies that subset. Text reads are not proof that a search evaluated every line: an early stop is separately a gap.
- `gaps`: typed, deduplicated reason totals. Examples include `read_error`, `binary_content`, `non_utf8`, `traversal_error`, `configured_schema_unavailable`, `schema_unreadable`, `schema_parse_error`, `source_parse_error`, `unresolved_local_import`, `dynamic_import`, `nonliteral_route_or_mount`, `source_changed_during_read`, and `search_result_limit`. Counts within a reason refer to unique path/line locations, not every missing import on the same line. Reasons overlap; one unreadable source can affect both text and source stages.
- `exclusions`: observed policy/filter exclusions, such as `ignore_rule`, `internal_storage`, `app_scope`, `glob_filter`, `symlink_or_junction`, `non_regular` and `not_source_language`. They are not read failures. `not_source_language` includes non-code assets/documents as well as unsupported languages; it does not imply every excluded file should have an AST.
- `output_truncated`: agrees with the envelope's `truncated` flag. `details_truncated` specifically indicates omitted/shortened coverage details.

When available, `details` includes requested scope/filters, effective manifest/ignore paths, configured app-root availability, supported source extensions, issue locations, actual parser/cache counts, parsed schema-declaration count and module-check selection. An app root's `available` state is a path observation, not a claim that all its files were analyzed. `module check all` lists unavailable modules it did not check; their count remains visible even if details are omitted. Source operations for module/trace/impact can index the entire selected app scope; their result target is not a promise of module-only parsing.

## Selection and gap rules

- Only the existing ignore/app selection policy is used. Ignored directories are pruned; Devkit does not enter them to count hidden descendants. Exclusion counts therefore describe encountered entries, not all excluded files. No whole-project percentage is fabricated. Repeated walks/read requests count each path once per category.
- Missing or excluded configured roots intersecting the requested path/app scope are coverage gaps. Unselected apps and optional unconfigured apps remain identified in details but do not degrade another app's coverage. A direct query of an excluded path is a gap, not a verified empty result.
- Whenever schema analysis runs, configured schema paths missing from the eligible catalog are gaps, even if another schema was successfully loaded. Unreadable/binary schemas are not parsed as empty strings. If no readable schema remains, schema commands exit `2`; syntax errors detected by the lightweight parser retain the existing exit `2`. This is not the Prisma compiler.
- Source indexing measures only supported JS/TS-family extensions. Unsupported single-file symbol requests have no analyzable source; unsupported `impact file` targets are explicitly partial rather than implying a complete reverse-consumer analysis.
- Unresolved relative imports, `@/` imports and explicitly configured aliases are local dependency gaps. Arbitrary package names/unconfigured aliases are not assumed to be broken local imports. Dependencies in unselected apps can remain unresolved; widening scope may help.
- Observed nonliteral imports and recognizable nonliteral Express route/mount arguments are reported as static gaps. This detection is conservative, not an inventory of every unsupported dynamic construct. Dependency queries record the depth limits actually used; relation direction/depth/shared-node stops remain intentional query boundaries.
- Cold parsing and validated cache hits record the same parser errors and observed dynamic import limitations. Cache failures that successfully fall back to live analysis remain warnings, not missing coverage. The summary-cache format was incremented; older summaries are re-parsed without touching durable feedback or agent-sync state.

## Examples

```powershell
devkit search "not-present" backend/src --json
devkit symbols backend/src/core/features/products/product.service.ts --json
devkit module check products --app erp --json
devkit trace route "/api/v2/core/products" --app erp --json
```

A no-match search with successfully processed eligible text can have `status: complete`. Searching an excluded `.env` path returns no content with a `requested_path_excluded` gap. Asking for symbols in a C# file returns `not_analyzed`, not a claim that it has no definitions. `find` may have complete path coverage with `source: not_run`.

A `find` or symbol listing limited only when rendering can have `status: complete` and `output_truncated: true`. A search that stops after finding one extra match has `status: partial`, a `search_result_limit` gap and truncated output. Review relevant gaps, narrow the query or increase limits before concluding no references or impact. Generic capabilities and runtime correctness still require source review and the application's real tests/type checks.
