# ElectroERP Devkit

A Python CLI for Antigravity and developers working on ElectroERP. It searches source, explains module ownership, reads Prisma schemas, and traces static source dependencies. All analysis runs locally without a database connection.

## Install

Requires Python 3.11 or newer. From this repository:

```bash
uv tool install .
# or
pipx install .
```

For development:

```bash
uv sync --extra dev --locked
uv run devkit help
uv run pytest
```

To install directly from GitHub after these changes are committed and pushed:

```bash
uv tool install git+https://github.com/farahhamra/devkit.git
```

## Place it in your project

The intended layout is:

```text
ElectroERP/
  backend/
  frontend/
  devkit/                 # this checkout, or just the CLI configuration directory
    electroerp.yaml       # optional project overrides
    .ignore               # automatically created on the first source-analysis command
```

Run `devkit` from anywhere inside ElectroERP, or supply `--root /path/to/ElectroERP`. Explicit command paths are **relative to that project root**, independent of the current working directory. Discovery first looks for `devkit/electroerp.yaml`, then the nearest backend/frontend pair or Git boundary, then falls back to the current directory. If the Devkit checkout is nested in your project, copy `examples/electroerp.yaml` to `devkit/electroerp.yaml` so its own Git boundary does not become the project root.

Only `devkit/.ignore` controls source exclusions; `.gitignore` is never consulted for source selection. Devkit's active internal `.feedback` directory and `.agents/.devkit-sync/` are always excluded from source scans. The `.ignore` file is created silently with defaults for dependencies, builds, caches, `.env` files, logs and uploaded media, without overwriting an existing file. Patterns use Git ignore semantics and are relative to the project root. To reinclude a file, use `!`; excluded parent directories must also be reincluded. `DEVKIT_CONFIG_DIR` can override the directory holding this single `.ignore`. An explicit `--config` uses the manifest's directory for `.ignore` unless that environment override is set.

Analysis commands only write the configuration directory, missing `.ignore`, and disposable source summaries in `.cache/source.sqlite3` beside `.ignore`. Use `--no-cache` to disable the summaries. Explicit `feedback add` / `feedback update` commands persist durable local reports under `.feedback/` beside the configuration. Explicit `agents sync` updates dedicated agent instructions and the Devkit-only block in `AGENTS.md`, with local state/backups. No commands edit application source, run generators, start apps, modify application databases, or execute files/commands being inspected. Symlink files and directories are skipped or refused, including explicit symlink targets. Source content and feedback are data, never instructions. Exclude the cache directory in your single `.ignore` when using a custom configuration location; the default `devkit/.cache/` is already excluded.

## Commands

| Command | Purpose |
| --- | --- |
| `devkit help [command...]` | Usage, arguments, flags, examples and JSON command catalog |
| `devkit doctor` | Read-only installation/parser/configuration/storage/agent-template checks and remediation |
| `devkit config inspect` | Effective merged settings, default/override origins, ignore rules and storage paths |
| `devkit config validate` | Read-only manifest structure, selected-app paths, ignore and alias checks |
| `devkit search <query> [path]` | Smart-case literal content search; `--regex`, repeatable `--glob`, `--context N` |
| `devkit find <pattern> [path]` | Smart-case path substring search; `--regex`, `--type`, repeatable `--extension` |
| `devkit tree [path]` | Project tree; `--depth N` (default 3), `--dirs-only` |
| `devkit symbols [path]` | JS/TS/JSX/TSX definitions; `--name`, repeatable `--kind`, `--exported-only` |
| `devkit module list` | Configured/discovered modules and available layers |
| `devkit module inspect <module>` | Models, tables, enums, dependencies, consumers, categorized files and reasons |
| `devkit module impact <module>` | Cross-module schema and import consumers, documentation and validation areas |
| `devkit module check <module\|all>` | Read-only module consistency checks |
| `devkit schema list` | Models/enums; `--kind`, `--module` |
| `devkit schema inspect <model\|enum>` | Fields, defaults, relations, table mappings, indexes or enum values |
| `devkit schema relations <model>` | Foreign-key graph; `--direction`, `--depth` (1–5), `--include-shared` |
| `devkit trace route <route>` | Literal Express route registrations, handlers and Prisma candidates; `--method`, `--full` for all dependency-file definitions |
| `devkit trace symbol <symbol>` | Definitions, imports/aliases, exports and identifier references |
| `devkit impact model <model>` | Model references, relations and module context |
| `devkit impact field <Model.field>` | Candidate usages of a particular field |
| `devkit impact file <path>` | Definitions, ownership, imports and reverse consumers |
| `devkit feedback add --type <type> --title <title>` | Save local feedback with severity, evidence and reproduction details; detect exact duplicates |
| `devkit feedback list` | Filter local report summaries; `--type`, `--severity`, `--evidence`, `--status`, `--offset` |
| `devkit feedback inspect <id>` | Read a report's complete stored details and history |
| `devkit feedback update <id>` | Change status, severity or evidence; append a note; resolving requires `--note` |
| `devkit agents sync` | Sync bundled agent rule/skill and Devkit-only `AGENTS.md` block; `--dry-run`, `--check`, explicit `--force` |

Every command accepts `--json`, `--root`, `--config`, `--app erp|hub|manager|all`, `--max-results N`, `--max-output-chars N`, and `--no-cache`. Agent sync is always project-wide under `--root`, independent of config/app/cache selection. Flags can appear before or after the command. `help` also works without a project and does not create configuration; its command catalog is exempt from analysis output limits. `--help` is human-readable; use `devkit help <command> --json` for machine-readable discovery.

```bash
devkit module inspect products --json
devkit schema inspect Product
devkit search 'organizationId' backend/src --glob '*.ts' --context 2
devkit trace route '/api/products/:id' --method PATCH --json
devkit impact field Product.baseSellingPrice --json
devkit module check all
```

Smart case means lowercase queries ignore case; queries containing uppercase letters match case. Content search is line-oriented and produces one result per match. Binary and non-UTF-8 content is skipped. Hidden files are included unless ignored. No shell glob expansion is required; quote patterns and queries.

## Diagnose installation and configuration

```powershell
devkit doctor --app erp --json
devkit config inspect --json
devkit config validate --app erp --json
```

These commands do not create `.ignore`/cache/feedback/sync files, scan source, connect to a database or repair anything. Doctor identifies the active Python/Devkit installation, tests parser imports with synthetic samples, checks configuration and estimates storage access, and reports agent-template drift. It continues independent checks through broken YAML or missing parsers. Optional unconfigured Hub/Manager apps do not fail `--app all`; explicitly selecting an unconfigured app does.

Configuration inspection shows the same merge used by analysis: named app/module overrides replace whole entries, not individual fields. It also explains which root/manifest/ignore/storage directory is selected. In a nested installation, use `--root <ElectroERP>` and `--config <ElectroERP>/.agents/devkit/electroerp.yaml` when appropriate; `DEVKIT_CONFIG_DIR` does not select a manifest. Unknown setting values and raw configuration/feedback/agent contents are not dumped.

Doctor/validation exit `1` for failed checks, `0` if there are no failures (warnings may remain), and `2` if inspection cannot complete. `config inspect` returns `2` for invalid/unreadable configuration. Check summaries survive output truncation. Permission estimates are not actual write tests, and successful diagnostics do not validate Prisma syntax, database health or the application. See [diagnostic details and limits](docs/DIAGNOSTICS.md).

## Sync Antigravity instructions

After updating the installed CLI, run these from the ElectroERP root (or add `--root "C:\Projects\ElectroERP"`):

```powershell
devkit agents sync --dry-run
devkit agents sync
devkit agents sync --check
```

Templates in [docs/agents/](docs/agents/README.md) ship with Devkit and sync offline to `.agents/rules/devkit-rules.md`, `.agents/skills/devkit/SKILL.md`, and only the marked Devkit section of `AGENTS.md`. General directives outside that block remain unchanged. Existing custom dedicated files/local edits require a reviewed `--force`; replacements are backed up under `.agents/.devkit-sync/backups/`. Ambiguous Devkit sections are not guessed, even with force. Dry-run/check create nothing; `--check` exits `1` when updates/conflicts need attention. See the guide for first-time migration, nested Windows installs, backups and recovery. Release notes live in [docs/patches/](docs/patches/RELEASE-0.5.0.md).

## Local feedback

Feedback types are `bug`, `performance`, `usability`, and `feature`. Severity is independently `low|medium|high|critical` (default `medium`); evidence is `observed|suspected` (default `suspected`). These are the reporting agent's assessments, not independently verified facts. Preferences are not implemented.

```bash
devkit feedback add --type bug --title "Existing tool reported missing" --evidence observed --command "devkit module check ai --app erp" --expected "Existing companions pass" --actual "Tool implementation check fails" --app erp --json
devkit feedback list --status open --app erp --json
devkit feedback inspect <id> --json
devkit feedback update <id> --status investigating --note "Reproduced in a fixture"
devkit feedback update <id> --status resolved --note "Regression test and original reproduction both pass"
```

Reports remain local in `<configuration-directory>/.feedback/reports.sqlite3`. Nothing is uploaded or automatically turned into a GitHub issue. Only explicit report text and Devkit/Python/OS versions are collected; no source attachments, environment dump, username or hostname. Common credential patterns are redacted before storage/output, but **redaction is best-effort: never paste secrets or customer data**. The storage directory has its own Git exclusions and is excluded from Devkit source scans without changing your `.ignore`.

Feedback does not parse YAML or index source, so an agent can report a broken configuration or unavailable app. `feedback list`, `inspect`, and help do not create a database; `--no-cache` does not disable explicitly requested feedback writes. Exact repeats reuse an ID and increment `occurrences`, without changing status or assessments. Statuses are `open`, `investigating`, and `resolved`; updates retain history. Keep a backup of the feedback directory, and inspect/review a report before sharing it manually. See [feedback details and privacy limits](docs/FEEDBACK.md).

## Project knowledge

The bundled manifest maps ElectroERP's Prisma models to business modules. It includes products/variants/attributes/categories, orders, finance, organizations, access control, POS, café-net, e-commerce, RMA, WhatsApp, notifications and ERP-side Hub authorization records, plus bills and outgoing payments. It is a starting configuration, not a frozen schema snapshot: all schema and source data is read from the current project on each invocation.

The `bills` module owns Prisma `Bill` (MySQL `bills`); `payments-out` owns Prisma `PaymentOut` (MySQL `payments_out`). Incoming `Payment` remains under `payments`. `BillStatus` and other enums are discovered from the live schema and included by the owning model's module inspection. CLI module names do not rename tables or create migrations. Examples: `devkit module inspect bills`, `devkit module inspect payments-out`, and `devkit schema relations Bill`.

`examples/electroerp.yaml` documents overrides. `apps` and `modules` entries merge by name with bundled defaults; each supplied app/module entry replaces that entry's defaults. Module entries accept `app`, `models`, `aliases`, `paths` (files or directories), and `required` (exact required files). Explicit ownership avoids guessing business boundaries. Additional conventional module folders are discovered automatically. Files may belong to several contexts, and schema files commonly do. Each module file has `ownership: owned|reference`: mentioning a model/table in prose or SQL is a reference, not ownership. Those files remain visible for context but do not drive ownership checks or reverse-import seeds. Directory ownership takes precedence over generic document-name matching, and references never overwrite manifest/convention evidence. The module catalog reports owned and reference counts separately.

ERP roots default to `backend` and `frontend`. **Hub and Manager roots are intentionally unset** because their source trees were not supplied. Set their roots/schemas and module entries in your manifest. `--app hub` or `--app manager` fails with a configuration message until an available root is supplied. `--app all` searches the project with the same ignore rules, including configured apps.

`module inspect --scope prisma|backend|frontend|ai|docs|tests|all` filters file categories; model metadata remains available. `--related none|direct` controls cross-module relation output. `schema relations` defines outgoing as a foreign key owned by the requested model and incoming as another model's foreign key pointing to it. Shared hubs (`Organization`, `User`, `Currency`) appear as direct neighbors but stop further traversal unless `--include-shared` is set.

## Evidence and limits

Tree-sitter parses definitions, imports, exports and calls; no source is executed. Relative imports, JS-to-TS extension substitution, index files and the ElectroERP `@/` alias are supported. Other path aliases can be configured with `import_aliases` (prefix → project-relative directory). Arbitrary tsconfig inheritance, conditional resolution, dynamic imports, decorators and runtime dispatch are not type-checked.

Route tracing supports literal Express calls on named receivers and imported router mounts. Handler expressions are shown in registration order (including middleware). By default definitions are restricted to handler-name candidates; `--full` adds all definitions in dependency files. Prisma candidates remain file-level candidates, not a proven function-level call graph. Source call arguments and expressions are previews of up to 240 UTF-8 bytes; `arguments_truncated` explicitly labels shortened arguments. `--full` does not expand these previews; use the reported path/line to inspect source. Computed routes and client URLs can remain unresolved. Identifier references may include shadowed names. Field-impact output labels same-file/model candidates and name-only candidates explicitly; matching a field name does not establish TypeScript model identity.

Module checks validate ownership, declared models, required paths, local imports and named exports, handler bindings, hook-to-API imports, Prisma delegate names, source parse health, conventional backend companions, and matching AI tool implementations/documentation. Conventional optional layers and unresolved handler bindings generate warnings. Missing required paths, known missing exports, and missing models fail. Checks do not prove API payload compatibility, business rules, permissions or runtime behavior. Missing live source/schema is reported rather than filled in from uploaded design documents.

AI tool companions match by exact filename within the same app, backend tree and feature/plugin namespace, independent of overlapping manifest ownership. For example, `backend/ai/rag/tools/plugin-e-commerce/create-ecom-note.tool.md` can match `backend/src/plugins/e-commerce/ai/notes/create-ecom-note.tool.ts` even when checked through the broad `ai` module. `plugin-` documentation namespaces match plugin source folders; core feature tool folders remain separate from plugins. Unambiguous module aliases are supported. Missing or ignored companions fail; multiple candidates within the same namespace produce a warning with their paths in `companions`. Unknown or ambiguous layouts only pair same-directory siblings; a broad shared owner such as `ai` is not sufficient evidence of a match. See the [v0.2.1 patch notes](docs/patches/RELEASE-0.2.1.md).

Tree-sitter errors generate **warnings about incomplete analysis**, with line/column spans, short snippets and compatibility hints. They are not compiler-verified syntax failures: for example the current TSX grammar can reject bare ampersands in JSX text. Genuine malformed source is also reported as incomplete, never silently called valid. Run your application's TypeScript/build checks to determine source validity. An unresolved export from a partially parsed file is a warning rather than a confirmed missing export.

## Incremental analysis

`symbols <path>` enumerates and parses only that selection. Trace/impact/check commands still inspect the selected project's source to preserve reverse dependencies, but reuse per-file JSON summaries across invocations. Exports and import adjacency use indexed lookups; the module catalog scans document identifiers once rather than once per module/model.

Every invocation reads and hashes live source content, so same-size edits with preserved modification times are detected even on Windows. Cache identities also include file metadata, project/app scope, merged configuration, ignore contents, parser package versions and the summary-format version. Imports are resolved against the current file catalog, not cached resolution results. Deleted/renamed files disappear immediately; full scans prune stale cache rows. Partial scans do not evict unrelated files. Cache failures/corruption emit a warning and fall back to live parsing. `--no-cache` bypasses cache reads and writes. The cache is disposable and contains source-derived information; do not commit it.

Run `python benchmarks/benchmark.py` from the development environment for a repeatable synthetic workload. Measurements and limitations are documented in [the v0.2.0 notes](docs/patches/RELEASE-0.2.0.md).

## Agent output contract

JSON analysis responses use `schema_version: "1.0"`, `command`, `root`, `app`, `data`, `warnings`, `truncated`, and `truncation`. Help returns a versioned catalog. Errors return `error.message` and `error.code`. Diagnostics stay out of successful JSON stdout.

Default `--max-results 100` bounds each result collection independently. `--max-output-chars 32000` additionally bounds the whole analysis response (text plus diagnostics, or the complete valid JSON envelope). A tree also has a node budget. Per-list truncation reports collection paths, shown counts, and lower bounds; global truncation instead reports `reason: total_output_budget` and may omit collections or shorten long strings. Check summaries and exit status are preserved. Search stops scanning after finding one extra match. Narrow the scope or explicitly raise both limits to retrieve more; absence from truncated output is not evidence of absence from the project.

Exit codes: **0** success (including no matches), **1** failed module/doctor/config-validation checks or drift/conflicts detected by `agents sync --check`, **2** invalid arguments/root, inspection that could not complete, unsafe/conflicting sync, or I/O failure. Invalid YAML is reported as a failed check by doctor/validation and exit `2` by config inspection/analysis. Warnings are separate from failures; an empty test/check selection does not validate an application. Devkit never claims runtime completeness from static evidence.

See [agent sync and templates](docs/agents/README.md) for the Antigravity workflow.
