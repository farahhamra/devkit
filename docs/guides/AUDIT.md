# Security and code-quality audits

Devkit exposes six audit commands. Route and organization-isolation checks use Devkit's own AST/Prisma analysis and an explicit project policy. Other commands adapt optional open-source engines. These commands produce evidence and review candidates; they do not prove runtime security or delete/refactor code. Performance profiling is not included.

| Command | Implementation | What it checks |
| --- | --- | --- |
| `devkit audit routes [path]` | Devkit | Configured Express authentication, roles and permissions, including middleware/mount order |
| `devkit audit isolation [path]` | Devkit | Configured organization constraints in direct Prisma `where`/create-data objects |
| `devkit audit security [path]` | Semgrep Community | Bundled baseline for dynamic evaluation, shell execution, unsafe raw SQL, disabled TLS verification, unverified JWT decoding and hardcoded credential candidates |
| `devkit audit dependencies [path]` | OSV-Scanner | Known advisories for versions in npm, Yarn and pnpm lockfiles |
| `devkit audit unused [path]` | Knip + ESLint with TypeScript parser/plugin | Unused export/file/dependency graph candidates and local binding/import candidates |
| `devkit audit duplicates [path]` | jscpd | Token clone candidates with both source occurrences |

All commands support the normal `--root`, `--config`, `--app`, `--json`, output limits and ignore policy. Paths are project-root relative. Route discovery resolves mounts across the selected app; its positional path filters endpoint findings. Other audits select inputs under the given path. `unused` requires all graph entrypoints to be inside that selection.

```powershell
devkit audit routes --app erp --json
devkit audit isolation backend/src --app erp --json
devkit audit security backend/src --app erp --json
devkit audit dependencies --app erp --json
devkit audit unused --app erp --entry backend/src/app.ts --entry frontend/src/main.tsx --json
devkit audit duplicates frontend/src --app erp --min-lines 8 --min-tokens 60 --json
```

## Engine installation

No optional scanner is installed automatically. The two native checks need only Devkit's standard Python dependencies. Install external engines in a trusted global/tool environment, not as executable configuration supplied by the inspected project. `devkit doctor --json` locates their executables without executing them; each audit validates its engine when invoked.

Tested versions: Semgrep **1.180.0**, OSV-Scanner **2.7.0**, Knip **6.41.0**, ESLint **10.12.0**, `@typescript-eslint/parser` and `@typescript-eslint/eslint-plugin` **8.71.2**, jscpd **5.4.1**, Node **24**. Adapters accept Semgrep 1.x, OSV-Scanner 2.7.x, Knip 6.x, ESLint 10.x, TypeScript ESLint 8.x and jscpd 5.x; report validation can reject incompatible output. Use the tested versions to reproduce the integration tests.

```powershell
# Semgrep is isolated from Devkit's Python dependencies.
uv tool install semgrep==1.180.0
# Or: pipx install semgrep==1.180.0

# Install Node 24, then install these together so ESLint can resolve its parser/plugin.
npm install --global knip@6.41.0 eslint@10.12.0 @typescript-eslint/parser@8.71.2 @typescript-eslint/eslint-plugin@8.71.2 jscpd@5.4.1
```

Install the OSV-Scanner 2.7.0 binary for your operating system from its [official release](https://github.com/google/osv-scanner/releases/tag/v2.7.0), verify it against the release `SHA256SUMS`, and put `osv-scanner`/`osv-scanner.exe` on PATH. Windows package managers may provide a different version; check `osv-scanner --version` before relying on it. Semgrep availability depends on the platform/Python version supported by its distribution; a missing or incompatible engine produces exit `2` rather than a successful empty scan.

For a dedicated npm prefix instead of a global install, add its `node_modules/.bin` to PATH or `DEVKIT_AUDIT_BIN_DIRS`. That variable accepts additional executable directories separated by `;` on Windows or `:` on Linux/macOS. Node must also be on PATH. Devkit invokes Node with the installed JS entry files, avoiding shell expansion and Windows `.cmd` execution.

## Project policy

The default policy is `audit.yaml` beside the active Devkit `.ignore`: commonly `devkit/audit.yaml`, or `.agents/devkit/audit.yaml` for a nested installation. `--policy <project-relative-file>` overrides it for `routes`, `isolation` and `unused`. It is independent of `electroerp.yaml`. Missing default policy is reported as unknown where required; an explicitly requested missing/invalid policy is an error. Policies reject unknown keys, duplicate YAML keys and root escapes/symlinks.

Start from [examples/audit.yaml](../../examples/audit.yaml), replacing every example guard/entrypoint/route with the actual project definition. This is a sample, not a claim about ElectroERP's current middleware. Devkit does not invent policy from names such as `auth`, `admin` or `organizationId`.

Route policy fields:

- `entrypoints`: Express app/router identities, each with a source `path` and local `router` variable. Unreachable route registrations remain unknown.
- `guards.authentication`: exported middleware identities `{path, symbol}`. Direct identifiers are supported; authentication factories/wrappers remain unknown.
- `guards.permissions` / `guards.roles`: exported factories, `argument` index (default `0`) and `mode: all|any` (default `all`). Only literal string/array values are interpreted. Guard semantics are trusted policy contracts, not verified implementation behavior.
- `routes`: exact `app`, uppercase `method`, full mounted `route`, `authentication: required|public`, and optional `permissions` / `roles`. `permission_mode` defaults to `all`; `role_mode` to `any`. `ALL` is a fallback method contract; overlapping exact/ALL policies produce unknown rather than choosing silently. Path parameters use the exact registration spelling. Wildcard policy paths are not supported.

A guard imported from a different file does not match merely because it has the same name. Local import aliases and barrel exports are resolved. Multiple routers in one file have separate identities. Middleware registered after an endpoint or after a router mount does not retroactively protect it. Unsupported dynamic paths, conditional registration, chained `.route()` calls, cycles and ambiguous guards are unknown. Handler-internal authentication and runtime `next()` behavior require separate review/API tests. A passing result means the configured static middleware contract was observed.

Isolation model policy requires `app`, Prisma `model`, scalar scope `field`, accepted receiver expressions in `receivers`, and `trusted_values` expressions. A model can instead be explicitly marked `global: true`. Live schema metadata verifies model/field names; physical table names are not Prisma delegate names.

For reads, updates and deletes, the checker looks for a mandatory scope constraint in `where`. An `AND` can contain a mandatory scope conjunct; every `OR` branch must enforce scope. A scope field under `select`, an unrelated string, or a negated condition does not satisfy the contract. Creates check every literal data row. Dynamic objects, spreads, untrusted values, unsupported operations and missing contracts remain unknown. Configured receiver/trusted-value expressions are assumptions: this is not data-flow or type analysis. Nested relation operations, ownership changes in update data, compound unique selectors, raw SQL, extensions and runtime aliasing need separate checks. Cross-organization API tests remain necessary.

Unused-code policy uses `unused.entry`, a list of exact project-relative source files, or repeat `--entry` to override it. Entries are required for each selected package whose graph should be analyzed. Packages without entries get unknown coverage; ESLint local checks still run. With no entries at all, only ESLint runs and graph coverage remains partial. Select the package manifests too when checking unused dependencies. Knip plugins, custom TS path aliases and project scripts/configs are intentionally not loaded; build/test dependencies and runtime/external consumers can make candidates appear unused.

## Output and gates

The existing schema-version `1.0` JSON envelope contains `data.audit`, `engines`, `checks`, `findings`, `limitations`, policy metadata where applicable, and a full `summary`. Checks have `pass|fail|unknown|not_applicable`; findings have severity and candidate confidence. Source evidence includes original relative paths and inclusive 1-based `line` / `end_line`, plus exact byte spans when available. jscpd spans cover the reported clone lines. Missing precise locations are explicitly marked `location_status: unavailable`; aggregate/policy records do not get invented lines. Exact dependency locations currently support npm lockfiles; Yarn/pnpm findings retain the lockfile path and a coverage gap for the missing precise span.

Coverage has an `audit` stage and `audit_selected` / `audit_analyzed` counts. Full summary counts survive `--max-results` and `--max-output-chars` truncation. `summary.complete` means eligible inputs were processed under the stated static/engine capabilities, not absence of vulnerabilities. Unknown checks, engine skips, unresolved graph imports and unavailable advisory severity produce partial coverage. Empty selections are not a clean audit.

- Exit `0`: no configured gate triggered. Unknown/partial results can still be present.
- Exit `1`: a native policy check failed, a finding meets `--fail-on`, or `--strict` found incomplete coverage.
- Exit `2`: invalid input/policy, missing/incompatible engine, timeout, malformed report, unsafe result path/range, or scanner/network failure.

`--fail-on low|medium|high|critical|none` defaults to `high` for security audits and `none` for unused/duplicate candidates. Native policy violations always fail independently of that threshold. `--strict` additionally gates unknown, partial or not-analyzed results. All external commands support `--timeout` in seconds, default `120` per engine invocation. Engine output larger than 64 MiB is rejected; subprocess output is spooled to temporary files before validation. No automatic fixes, dependency updates or deletions are performed.

## Network, snapshots and privacy

Devkit stages only eligible source/lockfile inputs into a temporary directory, maintaining relative paths. Original project `.gitignore`, Semgrep configs, Knip/ESLint plugins and jscpd configs do not control the scan. The adapters use bundled/explicit settings and clear executable startup hooks such as `NODE_OPTIONS` and scanner tokens from child environments. Installed scanner code is trusted software; inspected application code is not executed. The ESLint bridge disables inline configuration, and Semgrep's `nosemgrep` suppressions are disabled. Temporary snapshots/reports are cleaned up on normal completion and handled errors; forcibly killing the entire process can leave OS temporary files.

Security/quality engines use local rules; Semgrep telemetry/version checks and remote rule fetching are disabled. Secret candidate values, raw source excerpts and scanner stderr are not copied to audit output. Paths, symbol names and dependency identities are still project metadata. Raw engine error text is withheld because it may contain source or credentials; the response reports failure/exit status.

`dependencies` is the exception to offline analysis: its default mode queries advisory services with package names/versions. It does not upload application source. Network/authentication failures return `2`, never a clean empty result. For offline use, populate OSV's local database separately, then run:

```powershell
devkit audit dependencies --app erp --offline --offline-db "C:\Tools\osv-cache" --json
```

The directory is OSV-Scanner's cache base (with its `osv-scalibr/<ecosystem>/all.zip` layout), not a lockfile. `--offline-db` requires `--offline`; without it OSV uses its standard local cache. Devkit never silently downloads missing offline databases. Local/unknown-origin dependencies, packages without advisory severity and unreported lockfiles are explicit gaps. Offline database freshness remains the user's responsibility.

## Development verification

Normal tests include native contracts, malformed output, timeouts, input boundaries, policy validation and JSON/coverage behavior without requiring optional engines. After installing all tested engines, run:

```powershell
$env:DEVKIT_TEST_AUDIT_ENGINES = '1'
uv run --no-sync pytest tests/test_audit.py -q
```

Real-engine tests cover security candidate/redaction, unused bindings/exports/files/dependencies, separate backend/frontend packages, project-config non-execution, clone locations and an OSV offline database with a deliberately synthetic advisory. They do not claim that the actual ElectroERP application has passed a security audit. Online advisory availability is a separate environment check.
