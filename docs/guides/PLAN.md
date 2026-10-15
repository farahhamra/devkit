# Antigravity plan and task archives

`devkit plan archive <conversation-id>` saves both Antigravity documents locally, verifies their copies, then clears the contents of the original files. Run it after the agent finishes its task and stops writing those documents. Devkit does not inspect task checkboxes or ask Antigravity whether a task is complete.

## Usage

From the ElectroERP project root:

```powershell
devkit plan archive <conversation-id> --dry-run
devkit plan archive <conversation-id>
devkit plan archive <conversation-id> --json
devkit help plan archive
```

With Devkit checked out in `.agents/devkit`, select the application root explicitly if running inside that checkout:

```powershell
devkit plan archive <conversation-id> --root "C:\Backup\Electro ERP\version-2" --dry-run
devkit plan archive <conversation-id> --root "C:\Backup\Electro ERP\version-2" --json
```

Replace `<conversation-id>` with the folder ID. IDs accept 1–128 ASCII letters, digits, hyphens and underscores, starting with a letter or digit; paths and Windows device names are refused. There is no automatic conversation selection.

## Files and numbering

The current user's profile (`Path.home()`, normally `%USERPROFILE%` on Windows) supplies these exact sources:

```text
C:\Users\<you>\.gemini\antigravity\brain\<conversation-id>\implementation_plan.md
C:\Users\<you>\.gemini\antigravity\brain\<conversation-id>\task.md
```

The destination is always inside the selected project root:

```text
.agents/implementation/trash/
  .gitignore
  .devkit-archive/
    055.json                       # Hashes, paths, time, conversation and operation phase
  plans/
    055_implementation_plan.md
  tasks/
    055_task.md
```

The next shared index is one greater than the highest numeric prefix in either archive folder or an existing operation record. All Markdown names beginning with digits followed by `_` participate, including custom names. Start at `001`; `999` advances to `1000`. Gaps are not filled. Retained records preserve the high-water mark even if old copies are manually removed. Indexes are limited to 18 digits.

A new archive requires both source files to exist and contain non-whitespace UTF-8 content, each at most 16 MiB. Byte order marks, line endings and contents are preserved exactly. Clearing truncates each original to zero bytes in place; it does not delete the files. Other artifacts such as `walkthrough.md`, version histories and Antigravity metadata are not changed.

No Prisma configuration, parser, app source scan, database or network connection is required. `--app`, `--config`, `DEVKIT_CONFIG_DIR` and `--no-cache` do not relocate archives or select the Antigravity profile. `--root` selects the project destination. Running under another Windows user selects that user's brain, and running on a cloud/Linux machine does not access your Windows profile.

## Verification and recovery

Both copies must match their SHA-256 hashes before either source is cleared. Devkit checks the source snapshots again before clearing, and checks each file through its open handle immediately before truncating it. Existing archives are never overwritten. Symlinks, junctions, non-regular files and unexpected hard links are refused. Final publication uses temporary same-filesystem hard links, supported by NTFS and standard Linux filesystems; an unsupported filesystem fails without clearing originals. File writes are flushed; directory entries are also flushed where supported by POSIX.

The operation record progresses through `copying`, `clearing` and `complete`. A project archive lock serializes Devkit invocations. Failures return exit `2`, a message, the record path and available per-file verification/source statuses. No document contents appear in output.

- A copy failure preserves both originals. Retrying the same command resumes the recorded index rather than allocating another. A partially published temporary link is recognized and completed.
- A clearing failure leaves both verified copies available. Retrying clears only remaining content that still matches the saved hashes; an already-empty original is recognized.
- Changed sources, a corrupt/missing saved copy or invalid metadata block recovery. New content is never cleared using an old record. Preserve that content and inspect the record and copies before manual recovery; there is no force option.
- A successful repeat with both originals still empty returns `already_archived`, after verifying the saved copies. It creates no duplicate archive.
- A later nonempty plan/task pair in the same conversation receives a new index. One empty file and one new document are not considered a complete new pair. Even a restored pair with identical text is treated as new work once the earlier operation completed.

An interrupted process can leave `.agents/implementation/trash/.devkit-archive/archive.lock`. The lock contains the writer PID. Verify that no archive command is still running before removing this exact lock file and rerunning. Keep operation records and archive copies. Temporary `.archive-*` files left by a hard process crash contain local staging bytes; they are ignored by Git and do not authorize source clearing.

Two originals cannot be cleared as one filesystem transaction. Antigravity does not share Devkit's lock and can write again after checks or after the command finishes. Use the command after work is finished; a successful archive does not reset the IDE's conversation, history or in-memory state. A second project archiving the same conversation is also outside the project lock's scope.

## Local-only storage

Devkit creates or appends these exclusions to `trash/.gitignore`, preserving unrelated existing content:

```gitignore
/plans/
/tasks/
/.devkit-archive/
```

No archives are uploaded or added to Git. Already tracked files remain tracked: `.gitignore` does not remove them from Git history or the index, and Devkit does not change the Git index. The `.gitignore` itself can be tracked to share the exclusions.

Git exclusions do not control Devkit source searches. To exclude archived documents from Devkit discovery too, add `.agents/implementation/trash/` to the project's active Devkit `.ignore`. No source-selection rules are changed automatically. The numeric archive index is a filename sequence and recovery ledger, not a content-search database.

## Output and exit codes

Readable text and the existing JSON `schema_version: "1.0"` envelope contain the same archive result. `data` includes `conversation_id`, `mode`, `status`, `index`, `phase`, `record` and `files.plan` / `files.task`. Each file reports `source`, `archive`, `verified` and `source_status` (`pending`, `empty`, `changed`, `missing` or `unreadable`). Validation failures before an index is selected may omit archive details. Failures inside the operation add `data.error`; argument/root errors use the normal top-level `error` envelope.

`--dry-run` validates readable inputs/state and shows `would_archive`, `would_resume` or `already_archived`. It creates no directories, ignore files, locks, records or copies, and clears nothing. Its index is provisional: another invocation may archive first. It does not test write permissions, disk space, hard-link support or whether Antigravity holds a Windows sharing lock.

- `0`: archived, resumed, already archived, or a valid preview.
- `2`: invalid/unsafe inputs, storage/verification failure, or incomplete clearing. Inspect per-file statuses and the error; do not infer that nothing was written from this code alone.

The archive operation does not produce analysis coverage. Existing output limits still apply; inspect `truncated` for unusually long paths or messages.
