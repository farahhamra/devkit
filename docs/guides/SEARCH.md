# Search diagnostics and path matching

```powershell
devkit search "world-countries" --app erp --timings --json
devkit search "world-countries" frontend/src --timings --json
devkit find customers --type dir --app erp --json
devkit find "product.*\.service\.ts$" --regex --app erp --json
```

`search` matches file contents; `find` matches **relative path substrings**, including directories. Both use smart case: a query with uppercase characters is case-sensitive, otherwise matching uses Python Unicode case-insensitive semantics. `find "*customers*"` searches for literal asterisks, not a glob. A no-match literal pattern containing `*` or `?` now gets a hint in `warnings` (stderr for human output). Exit status remains `0`. Regex queries and successful literal matches get no such hint; bracketed paths such as `[id]` remain ordinary literal substrings. The command never rewrites a query or scans again for a suggestion. Quote patterns to prevent shell expansion before Devkit receives them.

`search --glob '*.ts'` is an existing **file selection** filter, separate from the query and from `find` matching. `find --regex` explicitly selects regular expressions, not glob syntax.

## Timings

The new opt-in `search --timings` adds `coverage.details.search_timings`:

- `setup_ms`: argument parsing, project configuration and coverage setup from entry into `main`.
- `discovery_ms`: eligible path inventory and glob filtering.
- `read_ms`: file safety rechecks, bytes read, decoding and text coverage bookkeeping.
- `match_ms`: searching, line splitting, context and match construction.
- `through_search_ms`: elapsed time from `main` entry through the search, including bookkeeping.
- `bytes_read`: actual file bytes read by this invocation; binary sniff reads count, unread tails do not.

Timings exclude interpreter/import startup before `main`, output bounding/serialization, terminal rendering and IDE task-runner overhead. They are diagnostic observations, not additive exact subdivisions of end-to-end time. Human-mode timing summaries go to stderr; JSON stdout stays one envelope. Normal invocations add no timing fields. Existing output budgets can omit details, including timings; inspect truncation or narrow the search. Measure the whole process externally when comparing user-visible latency.

An IDE returning a background-task identifier is not a failed search. The task still needs to finish for a valid elapsed-time measurement. Changing the runner's synchronous wait threshold is an IDE setting, not a Devkit flag.

## Implementation and compatibility

Search keeps the complete selected inventory and the existing ignore/app/safety checks. Relative-path formatting is cached only for the invocation; filesystem eligibility is not cached. Search releases file contents after use instead of retaining a repository-wide text cache. Literal queries reject nonmatching whole files before splitting into lines. An ASCII-only fast path is used only when **both** query and content are ASCII; Unicode queries/content use Python regex semantics. Regex queries retain per-line matching, anchors, lookarounds and zero-width matches.

Full-file UTF-8/BOM and binary checks happen before reporting matches. Line/column/context ordering, ignore negations, hidden-file behavior, result limits and coverage semantics remain unchanged. No dependency, text index, large-file exclusion, or ignore-rule change is introduced. `--no-cache` still concerns source summaries, not a persistent search index.

## Reproducible benchmark

```powershell
python tests/benchmark_search.py --files 1200 --lines 1000 --runs 5 --profile
```

The script creates a temporary synthetic fixture and launches a fresh CLI process for every run. It checks file counts and no-match results and reports first-run/repeated-run timings. It does not flush OS caches or reproduce ElectroERP's disk/content distribution.

The supplied ElectroERP agent report (Windows 11, Python 3.12.10, Devkit 0.12.1, local NTFS) identifies the original command as `devkit search world-countries --app erp --json`. Its four repeated-run median was 2.059 s (1,291 discovered/1,287 text-read files); whole-project was 4.851 s, `frontend` 1.416 s, and `frontend/src` 1.111 s. Separately reported 44.16/58.93-second initial passes did not control OS cache, antivirus, startup or other competing costs, so disk-cache causality is unproven. The whole-project result was output-budget truncated. These are externally reported **before** measurements, not an after-update validation. Repeat the exact commands with `--timings` on that Windows workspace after upgrading; preserve counts, filters, ignore contents and output limits when comparing.

On the development Linux host, the same 1,200-file, 62.4 MB literal no-match fixture changed from **0.878 s to 0.243 s** repeated median (four repetitions after the first invocation), with identical counts. This is approximately 3.6x on that fixture, **not** verification of the reported 8–12-second Windows searches. Real Windows timings, exact query/root, selected bytes/files and storage characteristics remain required to establish that bottleneck and its improvement. Do not use these wall-clock values as flaky CI assertions.
