# Agent instruction synchronization

`devkit agents sync` installs/updates the agent guidance bundled with your installed Devkit version. It works offline, does not upgrade the CLI and never fetches templates from GitHub. Update Devkit first, then synchronize instructions. Templates are maintained here in `docs/agents/` and included in wheels/source distributions; editable installs read this directory in their own Devkit checkout, never a template directory supplied by the target project.

| Bundled source | Destination, relative to the ElectroERP root |
| --- | --- |
| [devkit-rules.md](devkit-rules.md) | `.agents/rules/devkit-rules.md` (dedicated file) |
| [SKILL.md](SKILL.md) | `.agents/skills/devkit/SKILL.md` (dedicated file) |
| [AGENTS.fragment.md](AGENTS.fragment.md) | Only the marked Devkit block in `AGENTS.md` |

No complete general `AGENTS.md` is stored as a template. Project-specific rule/skill additions belong in separate files, and general agent directives belong outside the Devkit block.

## Usage

From the ElectroERP project root (not a nested Devkit repository):

```powershell
devkit agents sync --dry-run
devkit agents sync
devkit agents sync --check
```

Or specify the root explicitly from anywhere, including when Devkit lives at `ElectroERP/.agents/devkit`:

```powershell
devkit agents sync --root "C:\Projects\ElectroERP" --dry-run --json
devkit agents sync --root "C:\Projects\ElectroERP"
```

Targets are always the three project-wide paths above. `--config`, `DEVKIT_CONFIG_DIR`, `--app` and `--no-cache` do not relocate them. App/config flags produce a scope reminder. Sync does not parse YAML, initialize `.ignore`/feedback/cache, or index source. Existing root-discovery rules apply, so use `--root` if nested Git boundaries make discovery uncertain. Help, `--dry-run` and `--check` create no files/directories.

`--dry-run` shows actions and a unified diff for the owned content only. General `AGENTS.md` content is not included in the preview. `--json` uses the normal schema-version `1.0` envelope. Respect `truncated`: large previews may need larger `--max-output-chars` or `--max-results`. The output includes `template_version`, `mode`, `up_to_date`, `applied`, `conflicts`, `state_update`, `files` and `backup`. File actions are `create`, `update`, `migrate`, `unchanged` or `conflict`; state-only registration/version updates may be necessary even when text already matches.

Exit codes:

- `0`: successful apply/preview, no-op, or current `--check`.
- `1`: `--check` found drift, missing registration, or content conflicts. This mode does not repair anything.
- `2`: conflicting apply/preview, unsafe paths, invalid arguments/state/encoding, or I/O failure.

`--dry-run` and `--check` are mutually exclusive. Neither writes even with `--force`.

## First migration and local edits

Existing dedicated rule/skill files that differ from the templates are not overwritten by default. Preview the intended replacement and explicitly authorize it:

```powershell
devkit agents sync --dry-run --force
devkit agents sync --force
```

Both commands must use the same intended project root. `--force` replaces unmanaged/locally edited dedicated files or the content of an existing marked block, always retaining backups. It does not override unsafe paths, corrupt state, unknown/ambiguous Devkit sections, or malformed markers. One conflict blocks the whole apply; other targets are not partially updated.

The exact legacy numbered subsection headed `**Devkit Source & Schema Discovery Operations**:` supplied during design can be migrated automatically, provided its body is unchanged and there is no other unmarked Devkit guidance. Custom additions, alternate wording or multiple sections are ambiguous. In those cases manually wrap only the existing Devkit-specific section using standalone markers outside code fences:

```markdown
<!-- devkit:begin -->
Your existing Devkit-only guidance
<!-- devkit:end -->
```

Then preview and apply with `--force`. Do not wrap the entire general file. Missing, duplicated, reversed or fenced markers are never guessed or repaired automatically. A file without Devkit guidance receives a new block; a missing `AGENTS.md` is created with just that block.

Subsequent syncs track hashes of dedicated-file content and **only** the managed `AGENTS.md` region. General-file edits outside the block are preserved byte-for-byte and do not block upgrades. Local managed-content changes require `--force`; already-current files are not rewritten. UTF-8 BOMs and the existing LF/CRLF style are preserved. Pure LF/CRLF/BOM conversion does not count as a local content change. Unsupported encodings, non-regular/hardlinked files, symlinks/junctions below the selected root, and files over 2 MiB are refused.

## State, backups and recovery

Local state is stored in `.agents/.devkit-sync/state.json`. Original replaced files and previous state are backed up under `.agents/.devkit-sync/backups/<UTC-timestamp>-<unique-id>/`, preserving their relative paths. A local backup of `AGENTS.md` contains its original full bytes for recovery; only the Devkit fragment is bundled/versioned as a template. The CLI never uploads backups or prunes them automatically. Keep them private: they may include your local instructions. They are not disposable source cache.

The internal directory is always excluded from Devkit source scans, including explicit targets. It has its own `.gitignore`, without rewriting existing project ignore files:

```gitignore
# Local Devkit sync state and backups. Do not commit.
*
!.gitignore
```

Writes stop if that storage-local ignore file is customized; restore the exact exclusions above. Git ignores do not untrack existing commits or prevent deliberate force-adds. New internal files use restrictive POSIX modes where supported; Windows access follows directory ACLs. Backups are not encrypted.

Sync takes an exclusive `.agents/.devkit-sync/sync.lock`, preflights all targets, saves backups before replacement, and uses atomic per-file replacement. It rechecks snapshots before writing and attempts to restore changed targets on a caught write failure. Concurrent edits detected during recovery are preserved and reported for manual recovery. Multi-file replacement is **not** a filesystem transaction: power loss or a killed process can leave a partial update. Preserve backups/state, inspect the three targets, and recover deliberately. After a crash, remove a stale lock only after confirming no sync is running; locks are never stolen automatically. The command coordinates other Devkit syncs, not arbitrary external editors.

To restore a backup, stop syncs, review the saved files, and copy back only the intended versions. If `AGENTS.md` has newer unrelated edits, restore only the managed region instead of overwriting those edits with a full-file backup. Keep corresponding state consistent or explicitly approve a later replacement with `--force`. Corrupt/unsupported state is never silently reset; preserve it and restore a known-good state/backup.

## Update example for your nested Windows checkout

If installed using `python -m pip install .` from the nested repository, run from the ElectroERP root:

```powershell
git -C .agents/devkit pull --ff-only
python -m pip install --upgrade ./.agents/devkit
devkit --version
devkit agents sync --dry-run
devkit agents sync
```

Use the interpreter/environment that owns the `devkit` command; pipx/uv-tool installations must be updated with their corresponding installer. `git pull` alone does not update a normal non-editable Python installation. On the first adoption of your existing customized files, use the reviewed `--force` workflow above. Future unmodified managed files update without it. Antigravity may require reloading its project/rules after files change.
