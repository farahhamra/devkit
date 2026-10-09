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
    .ignore               # automatically created on the first project command
```

Run `devkit` from anywhere inside ElectroERP, or supply `--root /path/to/ElectroERP`. Explicit command paths are **relative to that project root**, independent of the current working directory. Discovery first looks for `devkit/electroerp.yaml`, then the nearest backend/frontend pair or Git boundary, then falls back to the current directory. If the Devkit checkout is nested in your project, copy `examples/electroerp.yaml` to `devkit/electroerp.yaml` so its own Git boundary does not become the project root.

Only `devkit/.ignore` controls exclusions; `.gitignore` is never consulted. The file is created silently with defaults for dependencies, builds, caches, `.env` files, logs and uploaded media, without overwriting an existing file. Patterns use Git ignore semantics and are relative to the project root. To reinclude a file, use `!`; excluded parent directories must also be reincluded. `DEVKIT_CONFIG_DIR` can override the directory holding this single `.ignore`. An explicit `--config` uses the manifest's directory for `.ignore` unless that environment override is set.

The only automatic filesystem write is creation of the configuration directory and missing `.ignore`. Commands do not edit source, run generators, start apps, modify databases, or execute files being inspected. Symlink files and directories are skipped, including explicit symlink targets. File content is treated as data, never instructions.

## Commands

| Command | Purpose |
| --- | --- |
| `devkit help [command...]` | Usage, arguments, flags, examples and JSON command catalog |
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
| `devkit trace route <route>` | Literal Express route registrations and candidate dependency chain; `--method` |
| `devkit trace symbol <symbol>` | Definitions, imports/aliases, exports and identifier references |
| `devkit impact model <model>` | Model references, relations and module context |
| `devkit impact field <Model.field>` | Candidate usages of a particular field |
| `devkit impact file <path>` | Definitions, ownership, imports and reverse consumers |

Every command accepts `--json`, `--root`, `--config`, `--app erp|hub|manager|all`, and `--max-results N`. Flags can appear before or after the command. `help` also works without a project and does not create configuration. `--help` is human-readable; use `devkit help <command> --json` for machine-readable discovery.

```bash
devkit module inspect products --json
devkit schema inspect Product
devkit search 'organizationId' backend/src --glob '*.ts' --context 2
devkit trace route '/api/products/:id' --method PATCH --json
devkit impact field Product.baseSellingPrice --json
devkit module check all
```

Smart case means lowercase queries ignore case; queries containing uppercase letters match case. Content search is line-oriented and produces one result per match. Binary and non-UTF-8 content is skipped. Hidden files are included unless ignored. No shell glob expansion is required; quote patterns and queries.

## Project knowledge

The bundled manifest maps the 65-model ElectroERP schema supplied during design to business modules. It includes products/variants/attributes/categories, orders, finance, organizations, access control, POS, café-net, e-commerce, RMA, WhatsApp, notifications and ERP-side Hub authorization records. It is a starting configuration, not a frozen schema snapshot: all schema and source data is read from the current project on each invocation.

`examples/electroerp.yaml` documents overrides. `apps` and `modules` entries merge by name with bundled defaults; each supplied app/module entry replaces that entry's defaults. Module entries accept `app`, `models`, `aliases`, `paths` (files or directories), and `required` (exact required files). Explicit ownership avoids guessing business boundaries. Additional conventional module folders are discovered automatically. Files may belong to several contexts, and schema files commonly do.

ERP roots default to `backend` and `frontend`. **Hub and Manager roots are intentionally unset** because their source trees were not supplied. Set their roots/schemas and module entries in your manifest. `--app hub` or `--app manager` fails with a configuration message until an available root is supplied. `--app all` searches the project with the same ignore rules, including configured apps.

`module inspect --scope prisma|backend|frontend|ai|docs|tests|all` filters file categories; model metadata remains available. `--related none|direct` controls cross-module relation output. `schema relations` defines outgoing as a foreign key owned by the requested model and incoming as another model's foreign key pointing to it. Shared hubs (`Organization`, `User`, `Currency`) appear as direct neighbors but stop further traversal unless `--include-shared` is set.

## Evidence and limits

Tree-sitter parses definitions, imports, exports and calls; no source is executed. Relative imports, JS-to-TS extension substitution, index files and the ElectroERP `@/` alias are supported. Other path aliases can be configured with `import_aliases` (prefix → project-relative directory). Arbitrary tsconfig inheritance, conditional resolution, dynamic imports, decorators and runtime dispatch are not type-checked.

Route tracing supports literal Express calls on named receivers and imported router mounts. Handler expressions are shown in registration order (including middleware). It lists definitions/Prisma candidates in dependent files; those are not a proven function-level call graph. Computed routes and client URLs can remain unresolved. Identifier references may include shadowed names. Field-impact output labels same-file/model candidates and name-only candidates explicitly; matching a field name does not establish TypeScript model identity.

Module checks validate ownership, declared models, required paths, local imports and named exports, handler bindings, hook-to-API imports, Prisma delegate names, source parse health, conventional backend companions, and matching AI tool implementations/documentation. Conventional optional layers and unresolved handler bindings generate warnings. Missing required paths, known missing exports, and missing models fail. Checks do not prove API payload compatibility, business rules, permissions or runtime behavior. Missing live source/schema is reported rather than filled in from uploaded design documents.

## Agent output contract

JSON analysis responses use `schema_version: "1.0"`, `command`, `root`, `app`, `data`, `warnings`, `truncated`, and `truncation`. Help returns a versioned catalog. Errors return `error.message` and `error.code`. Diagnostics stay out of successful JSON stdout.

Default `--max-results 100` bounds each result collection independently, so a rich module inspection may contain several lists of up to 100 items. A tree has a total node budget. Truncation reports collection paths, shown counts, and lower bounds, never an invented exact total. Search stops scanning after finding one extra match. Narrow the scope or raise the limit to retrieve more. Long source lines themselves are not shortened.

Exit codes: **0** success (including no matches), **1** failed module checks, **2** invalid input/configuration or I/O failure. Warnings are separate from failures; an empty test/check selection does not validate an application. Devkit never claims runtime completeness from static evidence.

See [Antigravity usage rules](docs/ANTIGRAVITY.md) for the agent workflow.
