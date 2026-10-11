# Devkit 0.4.0

## Bundled agent configuration sync

- Added `devkit agents sync`, `--dry-run`, `--check` and explicit `--force` handling.
- Canonical templates live in `docs/agents/` and ship in installed packages. Sync is offline and version-aligned; it does not independently fetch GitHub files or update Devkit itself.
- Installs the dedicated `.agents/rules/devkit-rules.md` and `.agents/skills/devkit/SKILL.md`, and updates only a marked Devkit block in the project's general `AGENTS.md`.
- Exact legacy Devkit subsection migration, local-edit protection, state hashes and original-file backups. Unrelated `AGENTS.md` bytes, UTF-8 BOM and LF/CRLF conventions are preserved.
- Read-only previews/checks, exclusive writer lock, safe-path checks, preflight, atomic per-file replacement and best-effort rollback on write failure. Ambiguous general-file boundaries are never force-overwritten.
- Templates now describe feedback/sync writes accurately, avoid assumed Hub/Manager roots, distinguish static candidates and parser warnings from verified results, and retain real application tests alongside Devkit checks.
- Internal sync state/backups are excluded from source analysis and locally Git-ignored. Existing project ignore files are not modified.
- Release notes moved to `docs/patches/`; the old combined Antigravity guidance is replaced by dedicated agent templates and the [sync guide](../agents/README.md).
- Added lifecycle, legacy migration, edit protection, BOM/CRLF, unsafe-path and write-recovery tests, plus a non-editable package smoke test in the Linux/Windows CI matrix.

The JSON envelope remains `1.0`; the command catalog adds `agents` and `agents sync`. `agents sync --check` uses exit `1` for drift/conflicts; existing analysis/feedback exit behavior is unchanged. Preferences remain deferred.

See the [first-use migration, update and recovery instructions](../agents/README.md). Local backups contain original data and should not be published. Crash recovery is not an automatic multi-file transaction.
