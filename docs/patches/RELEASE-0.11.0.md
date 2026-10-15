# v0.11.0 — local Antigravity plan archives

- Added `devkit plan archive <conversation-id>` with `--dry-run`, explicit `--root`, readable output and `--json`.
- Saves exact copies of `implementation_plan.md` and `task.md` under `.agents/implementation/trash/plans` and `tasks`, using a shared next numeric index. Both copies are verified before clearing the original files in place.
- Added local Git exclusions, operation records, exclusive archive publication, source-change checks and interrupted-operation recovery. Conversation reuse creates a new archive pair; completed empty-source retries do not create duplicates.
- Added failure/recovery, numbering, path, Windows junction and installed-package checks. Updated the [archive guide](../guides/PLAN.md), help and bundled agent capability descriptions.

Update the installed CLI, then run `devkit agents sync --root <ElectroERP-root>` to synchronize its bundled instructions. Run archiving after the agent finishes writing the two documents; it does not reset Antigravity's conversation state.
