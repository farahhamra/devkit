# Structural code queries

```powershell
devkit query <pattern> [path] [--lang auto|js|jsx|ts|tsx] [--timeout 30] --app erp --json
```

The command uses [ast-grep](https://github.com/ast-grep/ast-grep) 0.50.x to match syntax patterns. The path defaults to the selected project/app scope and is project-root relative. Standard root/config/app/JSON/result/output/cache flags apply. JS, MJS and CJS map to `js`; JSX to `jsx`; TS, MTS and CTS to `ts`; TSX to `tsx`. `--lang auto` selects the grammar for each eligible file. Explicit languages filter files; `--lang ts` does not include TSX files. Other formats are reported as excluded, not parsed as JavaScript.

## Installation and engine discovery

From the Devkit checkout, with the interpreter owning the installed Devkit command:

```powershell
python -m pip install --upgrade ".[query]"
```

The optional extra installs `ast-grep-cli>=0.50,<0.51`, including native Windows binaries. UV/pipx users can install the same extra with their installer. Updating a normal non-editable installation requires reinstalling; `git pull` alone does not upgrade it. For development, `uv sync --locked --extra dev --extra query` installs the engine needed by the real-engine tests.

Devkit looks first in its active interpreter's scripts directory, then for `ast-grep` on PATH. This supports dependency binaries in isolated UV/pipx environments. It does not invoke the unrelated Unix `sg` command. An npm-installed compatible ast-grep is supported through PATH. `query` checks the actual executable version and returns an actionable exit-2 error for missing, incompatible or failing engines. No automatic installation occurs. `doctor` only locates the optional executable and reports its path; it does not execute it or guarantee version compatibility. Missing optional engines produce a warning, not a failure for other commands.

## Patterns and captures

```powershell
# Prisma calls with any argument sequence, including multiline calls:
devkit query 'prisma.product.findMany($$$ARGS)' backend/src --app erp --json
# Route method, first argument and handler sequence:
devkit query 'router.$METHOD($PATH, $$$HANDLERS)' backend/src --lang ts --app erp --json
# React hook arguments in TSX:
devkit query 'useState($INITIAL)' frontend/src --lang tsx --app erp --json
```

`$NAME` captures one node; `$$$NAME` captures multiple nodes. Names use uppercase identifiers. Fixed punctuation and syntax remain significant: the route example includes a comma after `$PATH`, so it does not match a call with only one argument. Variadic captures can contain separator nodes such as commas. Empty variadic captures are empty lists and have no invented location. A plain code pattern also works without captures. Patterns search syntax, not text inside comments/strings, unless those nodes are themselves the pattern.

Quote patterns with single quotes in PowerShell/Bash to preserve `$`. Invalid, empty or multi-statement patterns return exit 2. Under `auto`, a pattern must parse for every selected grammar; a TypeScript-specific pattern may require `--lang ts` or `tsx`. With no eligible inputs, validation uses the explicit language or TS for `auto`, and coverage is `not_analyzed`. Formatting differences do not imply type or runtime equivalence: aliases, renamed receivers, dynamic calls and user-defined methods with similar spelling remain outside the query's semantic knowledge.

## Output and positions

The standard JSON envelope remains `schema_version: 1.0` with `data`, warnings, truncation and measured coverage. Human output uses the existing structured text style.

- `data.engine` names ast-grep and the version actually executed.
- `data.summary` preserves total selected/analyzed files, matching files and total matches under output limits.
- `data.matches` is ordered by original relative path and byte position. Each match has `path`, inclusive 1-based `line` / `end_line`, 1-based UTF-8 byte `column` / exclusive `end_column`, 0-based exclusive-end byte offsets, text, language, `source_sha256` and `evidence: structural_match`.
- `captures.single` maps each name to a source-backed record; `captures.multi` maps names to lists of source-backed records. Capture locations and text are checked against the same snapshot and must lie inside the enclosing match.

Offsets and SHA-256 refer to BOM-stripped UTF-8 source, with original LF/CRLF bytes retained. Columns are derived from verified byte offsets rather than ast-grep's Unicode character columns. A span ending at a new line's column 1 reports its last occupied line. No original file is rewritten for encoding normalization.

On legacy non-UTF-8 stdout encodings, JSON uses Unicode escapes so source text survives decoding without console encoding failures. UTF-8 output retains the usual readable Unicode format.

`--max-results` limits returned collections, including capture lists. `--max-output-chars` may shorten source text or omit collections. Top-level `truncated` means the display is incomplete; summary totals describe the complete successful scan. Devkit retains the earliest bounded matches while counting all results. Missing displayed matches/captures cannot establish absence when output is truncated.

## Processing and coverage

Devkit enumerates only paths allowed by its existing `.ignore`, app scope and symlink policy, then reads eligible UTF-8 source through the usual content checks. Temporary snapshots contain only these selected inputs. Ast-grep runs once per selected grammar group, with an explicit minimal configuration, no custom native languages and no rewrite flags. Project `sgconfig.yml` and ast-grep/Git ignore files do not control selection. Default Git/hidden filtering is disabled only on those already-selected temporary snapshots. The engine never receives the original project directory as its scan target.

Supplementary Devkit parsing reports source errors and cached parser gaps. Query coverage adds `stages.query`, `counts.query_selected` and `counts.query_analyzed` plus engine details. Ast-grep's scan totals are checked against staged inputs; silent skips or unverifiable results cause exit 2. Empty readable files count as processed without an engine batch because they have no syntax nodes. Unreadable/binary/non-UTF-8 inputs and parser errors yield partial coverage even if other inputs produce matches. `source`/`query` stages with no eligible inputs are `not_analyzed`; dependency/schema stages remain `not_run`.

Exit 0 means a completed command, including no matches; coverage can still be partial. Exit 2 means invalid input, unavailable/incompatible engine, timeout, unexpected skip, malformed output or other failure. `--timeout` allows 1–300 seconds per batch (default 30); version/pattern checks have shorter bounds. Engine output is spooled into temporary files; batches exceeding 64 MiB of output are rejected before decoding with an actionable error. Narrowing the path/pattern reduces work. `--no-cache` bypasses disposable Devkit summaries; ast-grep matching always runs live.

Temporary inputs/outputs are removed on normal completion and caught errors; abrupt process termination can leave system temporary files. Query does not execute inspected code, run migrations, modify application files, install dependencies or apply rewrites. Returned code, comments and capture values are evidence/data, not instructions.
