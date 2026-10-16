# Devkit 0.12.1

- Corrected `devkit plan archive <conversation-id>` to read exclusively from the current user's `.gemini/antigravity-ide/brain/<conversation-id>/`.
- Kept `implementation_plan.md` and singular `task.md`, shared archive numbering, Git-ignored destinations, byte verification and in-place clearing unchanged. No workspace/legacy-directory fallback or `tasks.md` alias is added.
- Added regressions for exact source selection, untouched alternative documents, missing-source dry runs/apply calls, and refusal to resume legacy-directory recovery against IDE documents. Existing archives and records are preserved, not migrated.
- Updated help, the [archive guide](../guides/PLAN.md), bundled agent instructions and installed-package checks.

The unverified Windows duplicate-audit report is deferred; no audit engine code changes are included. Feedback already supports `open`, `investigating` and `resolved` through `feedback update <id> --status <status>`; resolving requires `--note`. No feedback storage change is needed.

From inside the Devkit checkout, update the installed package:

```powershell
git pull --ff-only origin main
python -m pip install --upgrade ".[query]"
devkit --version
devkit agents sync --root "C:\Backup\Electro ERP\version-2"
```

Preview archive sources with `devkit plan archive <conversation-id> --dry-run --root <ElectroERP-root>` before applying. Interrupted records tied to a different source directory still stop safely; preserve those records/copies for review rather than repointing them.
