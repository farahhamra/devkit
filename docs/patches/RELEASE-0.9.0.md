# Devkit 0.9.0 — security and code-quality audits

Adds `devkit audit routes|isolation|security|dependencies|unused|duplicates [path]` with the existing text/JSON output, app/ignore scope, exact available source ranges and measured coverage. Full finding/check counts survive output truncation.

- Devkit route checks resolve configured guard identities and middleware/mount order. Prisma isolation checks validate explicit scope contracts and mandatory filter branches. Unknown policy/dynamic cases remain visible.
- Optional Semgrep Community, OSV-Scanner, Knip + TypeScript-aware ESLint, and jscpd adapters run against temporary selected inputs with controlled settings. No application code/config plugins, fixes or migrations are executed.
- `--fail-on` selects a finding severity gate; `--strict` also fails incomplete audits. Missing engines, malformed reports, timeouts and failed online advisory lookups exit `2`.
- Doctor reports optional engine availability without executing it. Bundled agent templates describe all commands without prescribing command preference.

See the [audit guide](../guides/AUDIT.md) for installation, policy setup, supported constructs, offline advisory databases and limitations. External scanners are separately installed. Project-specific route/isolation contracts must be configured; the bundled example is not a verified ElectroERP policy. Source-backed findings are candidates, not runtime security proof. Performance analysis remains outside this release.

After updating the installed CLI, synchronize the bundled agent guidance with `devkit agents sync`. Documentation remains under `docs/guides/`, `docs/patches/` and `docs/agents/`.
