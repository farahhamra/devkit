# Devkit 0.2.0

## Fixes

- Source-derived documentation references no longer become module ownership or trigger unrelated AI-tool companion failures. Existing owned evidence is preserved.
- Module file attribution builds one shared index; document identifiers are scanned once. App root/path ownership and export/import adjacency lookups are cached in memory.
- `symbols <path>` parses only the selected files. Persistent SQLite/JSON summaries avoid reparsing unchanged source for broader queries. `--no-cache` bypasses them.
- Cache invalidation covers content hashes (including preserved-mtime edits), file metadata, configuration, ignore rules, parser/format versions and app scope. Import resolution always uses live file membership. Corrupt/locked/unavailable caches fall back to live analysis with warnings.
- Parser errors include location spans/snippets and mean incomplete analysis, not a TypeScript compiler verdict. TSX ampersand grammar failures receive a compatibility hint. Actual malformed source remains visible as a warning; run the app's compiler/build to validate it.
- Route traces show handler-name definition candidates by default. `--full` includes all dependency definitions, while call-expression previews stay bounded.
- `--max-output-chars` defaults to 32,000 characters across each analysis response. JSON remains valid, truncation is explicit, and check summary counts/exit status survive truncation. Help catalogs are exempt.
- Windows symlink tests skip only when creation fails with privilege error 1314. Root-escape checks run independently on every platform.

The JSON envelope remains version 1.0. File `ownership`, parser `diagnostics`, route `detail`, definition counts and call-preview truncation flags are additive fields. Parser-related check failures intentionally become warnings. Existing consumers must already honor the `truncated` flag.

## Local benchmark

Synthetic fixture: 1,047 TypeScript files, 10,470 symbols, 1,857 Markdown documents, 32 modules and 2,907 selected files including schema/config/ignore. Python 3.12.14 on this Linux cloud instance. The baseline is commit `c2a16b5`, imported from a temporary archive without altering the checkout. Each run uses the same fixture generator, with a fresh project for each revision.

| Operation | Baseline | 0.2.0 |
| --- | ---: | ---: |
| First source index | 7.166 s | 1.408 s |
| Repeated source index | 7.512 s | 0.831 s |
| Module catalog | 12.999 s | 0.580 s |

The repeated 0.2.0 index reused all 1,047 summaries and parsed zero files. Timings include project discovery for each measurement, but not interpreter startup. These are single-run illustrative measurements, not performance guarantees or measurements of the private ElectroERP source tree. Repeat with `python benchmarks/benchmark.py` using the installed development interpreter. `--root <fixture>` allows reusing a synthetic fixture; use a dedicated temporary directory, not an application directory.

Warm queries still read/hash live source, decode summaries and rebuild dependency resolution. Trace, impact and module checks still inspect all selected source to retain reverse consumers and router mounts. Purely outward traversal would miss those relationships. No sub-200 ms latency guarantee or Windows benchmark is claimed.

## Upgrade on Windows

From the Devkit checkout (including an `.agents/devkit` checkout):

```powershell
git pull --ff-only origin main
# Use the installer matching your existing installation:
python -m pip install --upgrade .
# OR: pipx reinstall electroerp-devkit
# OR: uv tool install --force .
devkit --version
```

Keep using your existing `--root` / `--config` settings for `.agents/devkit`; this release does not change project-root autodetection. Add `.agents/devkit/.cache/` to the project's single Devkit `.ignore` if that is your cache location. Do not replace existing ignore rules.

Retest on the actual Windows project:

```powershell
devkit module check products --app erp --json
devkit symbols backend/src/core/features/products --app erp --json
devkit trace route "/api/v2/core/products" --app erp --json
# Compare a cache-bypassed run when diagnosing differences:
devkit module check products --app erp --no-cache --json
```

The repository's CI matrix covers Windows/Linux with Python 3.11 and 3.13. Local verification here is Linux Python 3.12; a pushed workflow is not itself proof of a passing Windows run.
