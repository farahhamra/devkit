"""Offline agent-template synchronization with bounded ownership and recoverable writes."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import stat
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

from . import __version__
from .project import DevkitError, detect_root

BEGIN = "<!-- devkit:begin -->"
END = "<!-- devkit:end -->"
INTERNAL = ".agents/.devkit-sync"
STATE = INTERNAL + "/state.json"
IGNORE = "# Local Devkit sync state and backups. Do not commit.\n*\n!.gitignore\n"
TARGETS = {
    ".agents/rules/devkit-rules.md": "devkit-rules.md",
    ".agents/skills/devkit/SKILL.md": "SKILL.md",
    "AGENTS.md": "AGENTS.fragment.md",
}
MAX_BYTES = 2 * 1024 * 1024
LEGACY_TITLE = "**Devkit Source & Schema Discovery Operations**:"
LEGACY_BODY = (
    "   - Read-only inspection commands (`devkit module`, `devkit schema`, `devkit trace`, "
    "`devkit impact`, `devkit search`, `devkit symbols`, `devkit find`, `devkit tree`) are "
    "pre-approved for autonomous execution without prompting the user. `devkit` is globally "
    "installed on system `PATH`—always run it directly as `devkit <subcommand>` "
    "(e.g. `devkit schema inspect <Model>`), never via `node` or script paths."
)


def canonical(text: str) -> str:
    return text.removeprefix("\ufeff").replace("\r\n", "\n")


def digest(text: str) -> str:
    return hashlib.sha256(canonical(text).encode("utf-8")).hexdigest()


def templates() -> dict[str, str]:
    """Wheels carry resources; editable installs use only their own checkout's docs."""
    packaged = files("devkit").joinpath("agent_templates")
    checkout = Path(__file__).resolve().parents[2]
    source = packaged
    if not packaged.is_dir() and (checkout / "pyproject.toml").is_file():
        source = checkout / "docs/agents"
    result = {}
    for target, name in TARGETS.items():
        try:
            text = canonical(source.joinpath(name).read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            raise DevkitError("Bundled agent templates are missing/unreadable; reinstall Devkit.") from exc
        if not text.strip() or "<!-- devkit:" in text:
            raise DevkitError("Invalid bundled agent template; reinstall Devkit.")
        result[target] = text.rstrip() + "\n"
    return result


def fenced(text: str, position: int) -> bool:
    active = None
    offset = 0
    for line in text.splitlines(keepends=True):
        if offset > position:
            break
        match = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line.rstrip("\r\n"))
        if match:
            fence, rest = match.groups()
            if active is None:
                active = fence
            elif fence[0] == active[0] and len(fence) >= len(active) and not rest.strip():
                active = None
        if offset + len(line) > position:
            return active is not None
        offset += len(line)
    return active is not None


def agents_document(text: str, fragment: str) -> tuple[str, str, str, bool]:
    """Return new full text, old/new owned regions and whether a legacy section migrated."""
    newline = "\r\n" if "\r\n" in text else "\n"
    block = BEGIN + newline + fragment.rstrip().replace("\n", newline) + newline + END
    markers = list(re.finditer(r"(?m)^<!-- devkit:(begin|end) -->[ \t]*(?=\r?$)", text))
    if markers or "<!-- devkit:" in text:
        if (
            len(markers) != 2
            or text.count("<!-- devkit:") != 2
            or [m[1] for m in markers] != ["begin", "end"]
            or any(fenced(text, m.start()) for m in markers)
        ):
            raise DevkitError(
                "Ambiguous AGENTS.md markers. Keep exactly one standalone begin/end pair outside code fences; --force cannot repair boundaries."
            )
        start, end = markers[0].start(), markers[1].end()
        return text[:start] + block + text[end:], text[start:end], block, False
    # Only the exact legacy subsection supplied by the user is safe to migrate.
    legacy = list(re.finditer(r"(?m)^\d+\. " + re.escape(LEGACY_TITLE) + r"\r?$", text))
    if len(legacy) == 1 and not fenced(text, legacy[0].start()):
        start = legacy[0].start()
        end = legacy[0].end()
        body = re.match(r"\r?\n" + re.escape(LEGACY_BODY) + r"(?=\r?\n|$)", text[end:])
        if body:
            end += body.end()
            # Extra indented prose belongs to the subsection; never silently discard it.
            remainder = text[end:]
            next_line = next((line for line in remainder.splitlines() if line.strip()), "")
            outside = text[:start] + remainder
            if not next_line.startswith((" ", "\t")) and not re.search(r"\bdevkit\b", outside, re.IGNORECASE):
                return text[:start] + block + remainder, text[start:end], block, True
    if re.search(r"\bdevkit\b", text, re.IGNORECASE):
        raise DevkitError(
            "Unmarked or ambiguous Devkit guidance in AGENTS.md. Wrap only its Devkit section in <!-- devkit:begin --> / <!-- devkit:end -->, preview, then use --force if appropriate. No general content was replaced."
        )
    if fenced(text, len(text)):
        raise DevkitError(
            "AGENTS.md ends inside an unclosed code fence. Close it before appending a Devkit block; --force does not repair general Markdown."
        )
    separator = (
        "" if not text or text.endswith(newline * 2) else newline if text.endswith(newline) else newline * 2
    )
    return text + separator + block + newline, "", block, False


class AgentsContext:
    def __init__(self, root=None, config=None, app="all") -> None:
        self.root = Path(root).expanduser().resolve() if root else detect_root(Path.cwd().resolve())
        if not self.root.is_dir():
            raise DevkitError(f"Project root is not a directory: {self.root}")
        self.app = "all"
        self.warnings = []
        if config or app != "all":
            self.warnings.append(
                "Agent sync is project-wide: --config and --app do not relocate/select its targets. Use --root for the ElectroERP project."
            )


@dataclass
class Change:
    path: str
    before: bytes | None
    after: bytes
    owned_before: str
    owned_after: str
    action: str
    reason: str = ""

    def summary(self, preview=False):
        row = {"path": self.path, "action": self.action}
        if self.reason:
            row["reason"] = self.reason
        if preview and self.before != self.after:
            row["diff"] = (
                "\n".join(
                    difflib.unified_diff(
                        self.owned_before.splitlines(),
                        self.owned_after.splitlines(),
                        fromfile=self.path + " (current managed content)",
                        tofile=self.path + " (bundled template)",
                        lineterm="",
                    )
                )
                + "\n"
            )
        return row


class AgentSync:
    def __init__(self, context: AgentsContext) -> None:
        self.context = context
        self.root = context.root

    def path(self, relative: str) -> Path:
        path = self.root / relative
        if not path.is_relative_to(self.root) or ".." in Path(relative).parts:
            raise DevkitError("Agent sync path escapes the project root.")
        for item in (path, *path.parents):
            if item == self.root:
                break
            try:
                info = item.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(
                stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
            ):
                raise DevkitError(f"Agent sync refuses symlink/junction paths: {relative}")
            if item != path and not stat.S_ISDIR(info.st_mode):
                raise DevkitError(f"Agent sync parent is not a directory: {relative}")
        return path

    def read(self, relative: str) -> bytes | None:
        path = self.path(relative)
        if not path.exists():
            return None
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
            raise DevkitError(f"Agent sync requires a regular, non-hardlinked file: {relative}")
        if info.st_size > MAX_BYTES:
            raise DevkitError(f"Agent sync file exceeds the 2 MiB safety limit: {relative}")
        with path.open("rb") as handle:
            data = handle.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise DevkitError(f"Agent sync file exceeds the 2 MiB safety limit: {relative}")
        return data

    def state(self):
        raw = self.read(STATE)
        if raw is None:
            return {}, raw
        try:
            data = json.loads(raw)
            hashes = data["files"]
            if (
                data["version"] != 1
                or not isinstance(data["devkit_version"], str)
                or not isinstance(hashes, dict)
                or set(hashes) != set(TARGETS)
                or not all(isinstance(v, str) and re.fullmatch(r"[a-f0-9]{64}", v) for v in hashes.values())
            ):
                raise ValueError()
        except (ValueError, TypeError, KeyError) as exc:
            raise DevkitError(
                "Invalid/unsupported agent sync state. Preserve state/backups and restore valid state; --force does not reset it."
            ) from exc
        return hashes, raw

    def plan(self, force=False):
        hashes, state_before = self.state()
        sources = templates()
        changes = []
        for relative, source in sources.items():
            before = self.read(relative)
            try:
                text = (before or b"").decode("utf-8-sig")
            except UnicodeError as exc:
                raise DevkitError(f"Agent sync requires UTF-8 (optional BOM): {relative}") from exc
            bom = b"\xef\xbb\xbf" if before and before.startswith(b"\xef\xbb\xbf") else b""
            migrated = False
            if relative == "AGENTS.md":
                try:
                    after, old, new, migrated = agents_document(text, source)
                except DevkitError as exc:
                    changes.append(Change(relative, before, before or b"", "", "", "conflict", str(exc)))
                    continue
            else:
                old = text
                new = source.replace("\n", "\r\n") if "\r\n" in text else source
                after = new
            encoded = bom + after.encode("utf-8")
            action = (
                "unchanged"
                if encoded == before
                else "create"
                if before is None
                else "migrate"
                if migrated
                else "update"
            )
            reason = ""
            protected = bool(old) or (before is not None and relative in hashes)
            if action != "unchanged" and protected and not migrated and digest(old) != hashes.get(relative):
                if not force:
                    action = "conflict"
                    reason = "Unmanaged or locally edited content; preview with --dry-run --force and explicitly use --force to replace it with a backup."
                else:
                    reason = (
                        "Explicit --force replaces unmanaged/local edits; original file will be backed up."
                    )
            changes.append(Change(relative, before, encoded, old, new, action, reason))
        state_after = (
            json.dumps(
                {
                    "version": 1,
                    "devkit_version": __version__,
                    "files": {c.path: digest(c.owned_after) for c in changes},
                },
                indent=2,
            )
            + "\n"
        ).encode()
        return changes, state_before, state_after

    @contextmanager
    def locked(self):
        directory = self.path(INTERNAL)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = self.path(INTERNAL + "/sync.lock")
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise DevkitError(
                "Agent sync lock exists. Wait for the other sync; after a crash, verify no sync is running before removing .agents/.devkit-sync/sync.lock."
            ) from exc
        try:
            os.close(descriptor)
            ignore = self.read(INTERNAL + "/.gitignore")
            if ignore is not None and ignore.decode("utf-8") != IGNORE:
                raise DevkitError(
                    "Agent sync storage .gitignore was customized; restore its documented exclusions before syncing."
                )
            if ignore is None:
                self.write_new(INTERNAL + "/.gitignore", IGNORE.encode())
            yield
        finally:
            lock.unlink()

    def write_new(self, relative: str, data: bytes):
        path = self.path(relative)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

    def replace(self, relative: str, data: bytes, expected: bytes | None):
        path = self.path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.read(relative) != expected:
            raise DevkitError(f"File changed during agent sync; no overwrite attempted: {relative}")
        descriptor, staging = tempfile.mkstemp(prefix=".devkit-sync-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if expected is not None:
                os.chmod(staging, stat.S_IMODE(path.stat().st_mode))
            if self.read(relative) != expected:
                raise DevkitError(f"File changed during agent sync; no overwrite attempted: {relative}")
            os.replace(staging, path)
        finally:
            if os.path.exists(staging):
                os.unlink(staging)

    def apply(self, changes, state_before, state_after):
        changed = [c for c in changes if c.before != c.after]
        backup = None
        if any(c.before is not None for c in changed) or state_before is not None:
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex
            backup = INTERNAL + "/backups/" + stamp
            for change in changed:
                if change.before is not None:
                    self.write_new(backup + "/" + change.path, change.before)
            if state_before is not None:
                self.write_new(backup + "/previous-state.json", state_before)
        applied = []
        try:
            # Recheck all snapshots before the first target write.
            for change in changes:
                if self.read(change.path) != change.before:
                    raise DevkitError(f"File changed during agent sync: {change.path}")
            if self.read(STATE) != state_before:
                raise DevkitError("Agent sync state changed during synchronization.")
            for change in changed:
                self.replace(change.path, change.after, change.before)
                applied.append(change)
            self.replace(STATE, state_after, state_before)
        except (OSError, DevkitError) as exc:
            unrestored = []
            for change in reversed(applied):
                try:
                    if self.read(change.path) != change.after:
                        raise DevkitError("Changed concurrently")
                    if change.before is None:
                        self.path(change.path).unlink()
                    else:
                        self.replace(change.path, change.before, change.after)
                except (OSError, DevkitError):
                    unrestored.append(change.path)
            detail = (
                "Original targets restored."
                if not unrestored
                else "Manual recovery required for: " + ", ".join(unrestored)
            )
            raise DevkitError(
                f"Agent sync failed: {exc}. {detail} Backup: {backup or 'none (new files only)'}. No backups were deleted."
            ) from exc
        return backup

    def run(self, args):
        changes, before, after = self.plan(args.force)

        def result(applied=False, backup=None):
            conflict = any(c.action == "conflict" for c in changes)
            needed = before != after or any(c.before != c.after or c.action == "conflict" for c in changes)
            return (
                {
                    "template_version": __version__,
                    "mode": "check" if args.check else "dry-run" if args.dry_run else "apply",
                    "up_to_date": applied or not needed,
                    "applied": applied,
                    "conflicts": conflict,
                    "state_update": before != after,
                    "files": [c.summary(args.dry_run) for c in changes],
                    "backup": backup,
                },
                needed,
                conflict,
            )

        data, needed, conflict = result()
        if args.check:
            return data, 1 if needed else 0
        if args.dry_run or conflict or not needed:
            return data, 2 if conflict else 0
        with self.locked():
            # A preceding writer may have completed since the preview; replan under the lock.
            changes, before, after = self.plan(args.force)
            data, needed, conflict = result()
            if conflict or not needed:
                return data, 2 if conflict else 0
            backup = self.apply(changes, before, after)
        return result(applied=True, backup=backup)[0], 0
