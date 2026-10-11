# Read-only diagnostics (v0.5.0)

```powershell
devkit doctor [--app erp|hub|manager|all] [--json]
devkit config inspect [--json]
devkit config validate [--app erp|hub|manager|all] [--json]
```

All accept the existing `--root`, `--config`, `--max-results`, `--max-output-chars` and `--no-cache` flags. They do not create `.ignore`, cache, feedback or sync state; write test files; connect to databases; scan source; access the network; run external executables; or repair configuration. Normal filesystem reads may update access times. Running the CLI still requires a supported Python installation and its own readable package files.

## Doctor

`devkit doctor` reports structured `checks` with `id`, `status` (`pass`, `warning`, `fail`), a message, relevant details and remediation for problems. `summary` counts all checks before display truncation. `runtime` describes the running Devkit/code version, installed distribution metadata, Python executable/version, entrypoint and PATH's `devkit` candidate. It does not execute that candidate or claim that different paths conclusively prove different versions.

Checks cover:

- Imports of PyYAML, pathspec, Tree-sitter and JS/TS grammar packages, plus synthetic JavaScript/TypeScript/TSX parser smoke samples. No project source is parsed. The command can run with these dependencies missing/broken and report independent failures instead of crashing before argument parsing.
- The same configuration/path/ignore checks as `config validate`. A broken manifest produces a failed check; runtime, storage and agent checks still run without pretending the bad manifest loaded successfully.
- Read/write/creation **permission estimates** for the configured storage directory, disposable cache, durable feedback and agent-sync state; safe regular-file/sidecar paths; storage-local Git exclusions; and existing sync locks. Missing directories are normal if their nearest existing parent appears writable. Cache access problems are warnings because analysis can fall back to live parsing; durable/configuration storage problems fail. `--no-cache` skips cache-access checks, not feedback/sync checks.
- Bundled agent templates versus the three target files and saved sync hashes, without displaying their contents or applying changes. Missing, outdated, legacy or edited/unmanaged guidance produces warnings; inaccessible/invalid sync state is a failure. Missing/outdated registration is reported separately. Use `agents sync --dry-run` to review drift, and only synchronize when authorized.

Permission estimates are based on filesystem metadata and `os.access`; they do **not** establish actual Windows ACL/write access, lock availability, free disk space or SQLite integrity. Databases and backups are not read. Doctor never resets a cache, steals a sync lock, publishes feedback, installs dependencies or updates agent files. Its successful smoke samples do not prove complete parsing of your repository or application correctness.

## Effective configuration inspection

`devkit config inspect` returns:

- `selection`: absolute project root and its discovery reason (`explicit`, `manifest`, `source_pair`, `git_boundary`, `cwd_fallback`), selected manifest, configuration directory and its origin, ignore path and cache/feedback/sync storage paths.
- `effective`: recognized settings after the **same merge and structural validation used by source-analysis commands**. Apps/modules are listed with default-versus-manifest origin; omitted per-module list fields and app selection are explicit. It lists configured modules only, not convention-discovered modules or inferred ownership. Use `module inspect` for actual ownership evidence.
- `top_level_sources`: provenance of version, shared-model and import-alias settings.
- `ignore`: existing patterns or the bundled defaults that would be created on an analysis invocation, plus mandatory internal exclusions. No ignore file is created by inspection.
- `unrecognized_fields`: unsupported setting names, with their values omitted. No raw YAML, credentials from unknown fields, complete environment dump, source, feedback text or general agent instructions are emitted. Common sensitive patterns in displayed strings are sanitized best-effort; still review diagnostics before sharing them.

Apps/modules merge by name, but **each overridden entry replaces the whole default entry**. Other supplied top-level settings replace their defaults. For example, overriding `modules.products` with only `models` does not preserve its bundled aliases. Unknown fields generate warnings; fix misspellings instead of relying on them. A malformed manifest is never represented as successfully loaded defaults. Duplicate explicit YAML mapping keys are rejected with a line/column diagnostic; standard YAML merge anchors with explicit overrides remain supported. Manifests must be UTF-8 (optional BOM) regular files within the 2 MiB safety limit.

Inspection shows the whole merged configuration even when `--app` is specified; each app's `selected` field shows the requested scope. It does not establish path availability. A syntactically/structurally valid config can be inspected even when its configured app directories do not exist; run validation/doctor to check those paths.

## Configuration validation

`devkit config validate` checks global manifest types, schema version and configured path safety, then checks **selected apps'** declared roots/schemas for presence, type, permission estimates and effective ignore exclusions. It also checks required module files, explicit custom ownership paths, import-alias targets and ambiguous normalized module aliases. Required files and selected configured root/schema failures are errors; missing optional module layers/alias targets and ambiguous aliases are warnings. It does not verify that configured Prisma model names exist or validate Prisma syntax; those are schema/module operations, not configuration loading.

- Under `--app all`, apps with empty `roots` and `schemas` are unconfigured, not broken. Explicitly selecting such an app fails with instructions to configure it.
- Nonempty roots that are missing are failures for selected apps, not silently treated as optional. `--app erp` skips Hub/Manager path availability, but global invalid manifest structure or escaping/symlinked paths still fails; source analysis uses the same global validation.
- Apps without configured Prisma schemas are allowed (for example Manager). Dynamically discovered schemas are outside this read-only check. A configured schema outside its app roots is warned about because app-scoped discovery will omit it.
- Analysis ignores are honored, including exclusions on ancestors; configured but ignored roots/schemas cannot be claimed as available for analysis. An absent `.ignore` uses bundled defaults in memory. Invalid/unreadable ignore input is reported, not rewritten.
- A nearby nested/root-level manifest that was not selected produces a hint, not an automatic change to root discovery. Existing discovery semantics remain unchanged.

## Nested Windows installation example

Run from the ElectroERP root, not the Devkit Git checkout:

```powershell
devkit doctor --root . --config .agents/devkit/electroerp.yaml --app erp --json
devkit config inspect --root . --config .agents/devkit/electroerp.yaml --json
devkit config validate --root . --config .agents/devkit/electroerp.yaml --app erp
```

`--config` is resolved from the current working directory, while source paths in the manifest are relative to the selected project root. `DEVKIT_CONFIG_DIR` overrides only the directory holding `.ignore`/cache/feedback; it **does not select a different manifest**. Agent-sync state always remains under `<project-root>/.agents/.devkit-sync`. These locations are displayed so a wrong installation/root/configuration can be diagnosed without guessing.

## Exit codes and output limits

| Command | 0 | 1 | 2 |
| --- | --- | --- | --- |
| `doctor` | No failed checks; warnings may remain | One or more failed checks, including broken config/dependencies | Invalid arguments/root or inspection could not complete |
| `config validate` | No failed checks; warnings may remain | Invalid manifest/ignore or failed configuration/path checks | Invalid arguments/root or inspection could not complete |
| `config inspect` | Effective recognized configuration/ignore loaded and displayed | Not used | Invalid arguments/root or manifest/ignore could not be loaded/validated |

The usual JSON envelope remains schema version `1.0`; warnings and failed checks are structured data on stdout. Read check statuses and `summary`, not just the top-level warnings array. Truncation preserves diagnostic summary counts and exit status; narrow app scope or raise output limits to see omitted details. Passing diagnostics establishes only the checks described here, not runtime, database, application-test or complete repository coverage.
