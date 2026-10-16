"""Local Antigravity plan/task archives with verified copies and resumable clearing."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .project import DevkitError, detect_root

ARCHIVE = ".agents/implementation/trash"
INTERNAL = ARCHIVE + "/.devkit-archive"
NAMES = {"plan": "implementation_plan.md", "task": "task.md"}
FOLDERS = {"plan": "plans", "task": "tasks"}
IGNORE = b"# Local Devkit plan archives and recovery records.\n/plans/\n/tasks/\n/.devkit-archive/\n"
MAX_BYTES = 16 * 1024 * 1024
INDEX = re.compile(r"[0-9]{3,}")
PREFIX = re.compile(r"([0-9]+)_.*\.md", re.IGNORECASE)


def conversation_id(value: str) -> str:
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"{p}{i}" for p in ("COM", "LPT") for i in range(1, 10))}
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value) or value.upper() in reserved:
        raise DevkitError(
            "Conversation ID must be 1–128 ASCII letters, digits, hyphens or underscores; not a path or Windows device name."
        )
    return value


def safe_path(path: Path) -> Path:
    """Check lexical parents before opening; do not resolve away links/junctions."""
    for item in reversed((path, *path.parents)):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise DevkitError(f"Plan archive refuses symlink/junction paths: {item}")
        if item != path and not stat.S_ISDIR(info.st_mode):
            raise DevkitError(f"Plan archive parent is not a directory: {item}")
    return path


@contextmanager
def opened(path: Path, *, writable=False, links=1):
    safe_path(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != links:
        raise DevkitError(f"Plan archive requires a regular, non-hardlinked file: {path}")
    descriptor = os.open(
        path,
        (os.O_RDWR if writable else os.O_RDONLY) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0),
    )
    with os.fdopen(descriptor, "r+b" if writable else "rb") as handle:
        actual = os.fstat(handle.fileno())
        if (actual.st_dev, actual.st_ino, actual.st_nlink) != (before.st_dev, before.st_ino, links):
            raise DevkitError(f"File changed while opening: {path}")
        yield handle


def read(path: Path, *, links=1, maximum=MAX_BYTES) -> bytes | None:
    try:
        with opened(path, links=links) as handle:
            before = os.fstat(handle.fileno())
            data = handle.read(maximum + 1)
            after = os.fstat(handle.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise DevkitError(f"File changed while reading: {path}")
    except FileNotFoundError:
        return None
    if len(data) > maximum:
        raise DevkitError(f"File exceeds the {maximum}-byte archive limit: {path}")
    return data


def fingerprint(data: bytes) -> dict:
    return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def nonempty(data: bytes | None) -> bool:
    if data is None:
        return False
    try:
        return bool(data.decode("utf-8-sig").strip())
    except UnicodeError as exc:
        raise DevkitError("Antigravity documents must be UTF-8 Markdown.") from exc


def sync_directory(path: Path):
    # POSIX needs durable directory entries as well as fsynced file contents.
    # Windows has no portable directory fsync; file handles are still flushed.
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def make_directory(path: Path):
    safe_path(path)
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    for created in reversed(missing):
        sync_directory(created.parent)


def write_new(path: Path, data: bytes):
    safe_path(path)
    make_directory(path.parent)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def replace(path: Path, data: bytes, expected: bytes | None):
    """Atomic updates only to owned metadata/staging, never source or final archives."""
    if read(path) != expected:
        raise DevkitError(f"Archive storage changed concurrently: {path}")
    make_directory(safe_path(path).parent)
    descriptor, temporary = tempfile.mkstemp(prefix=".archive-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if read(path) != expected:
            raise DevkitError(f"Archive storage changed concurrently: {path}")
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class PlanContext:
    def __init__(self, root=None, config=None, app="all"):
        self.root = Path(root).expanduser().absolute() if root else detect_root(Path.cwd())
        if not safe_path(self.root).is_dir():
            raise DevkitError(f"Project root is not a directory: {self.root}")
        self.root = self.root.resolve()
        self.app = "all"
        self.warnings = []
        if config or app != "all":
            self.warnings.append(
                "Plan archiving is project-wide; --app and --config do not select its paths. Use --root for the ElectroERP project."
            )


class PlanArchive:
    def __init__(self, context: PlanContext, identifier: str):
        self.context = context
        self.root = context.root
        self.identifier = conversation_id(identifier)
        # Active documents belong only to the IDE brain, never workspace archives
        # or the legacy directory: successful archiving clears these exact files.
        self.source = Path.home().absolute() / ".gemini/antigravity-ide/brain" / self.identifier
        self.record = None
        self.record_before = None

    def path(self, relative: str) -> Path:
        return safe_path(self.root / relative)

    def destination(self, kind: str) -> Path:
        return self.path(f"{ARCHIVE}/{FOLDERS[kind]}/{self.record['index']}_{NAMES[kind]}")

    def stage(self, kind: str) -> Path:
        return self.path(f"{INTERNAL}/{self.record['index']}_{NAMES[kind]}.stage")

    def record_path(self) -> Path:
        return self.path(f"{INTERNAL}/{self.record['index']}.json")

    def sources(self) -> dict:
        data = {kind: read(self.source / name) for kind, name in NAMES.items()}
        if any(value is None for value in data.values()):
            missing = ", ".join(NAMES[k] for k, value in data.items() if value is None)
            raise DevkitError(
                f"Missing Antigravity document(s) in {self.source}: {missing}. Both files are required."
            )
        return data

    def records(self) -> list:
        directory = self.path(INTERNAL)
        if not directory.exists():
            return []
        result = []
        seen = set()
        for path in sorted(directory.glob("*.json")):
            raw = read(path, maximum=1024 * 1024)
            try:
                row = json.loads(raw)
                index = row["index"]
                if (
                    row["version"] != 1
                    or not isinstance(index, str)
                    or not INDEX.fullmatch(index)
                    or not 0 < int(index) < 10**18
                    or index != f"{int(index):03d}"
                    or path.name != index + ".json"
                    or int(index) in seen
                    or row["phase"] not in {"copying", "clearing", "complete"}
                    or set(row["files"]) != set(NAMES)
                    or not isinstance(row["source_directory"], str)
                    or not isinstance(row["created_at"], str)
                ):
                    raise ValueError
                conversation_id(row["conversation_id"])
                for value in row["files"].values():
                    if (
                        type(value["bytes"]) is not int
                        or not 0 < value["bytes"] <= MAX_BYTES
                        or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"])
                    ):
                        raise ValueError
            except (ValueError, TypeError, KeyError, DevkitError) as exc:
                raise DevkitError(
                    f"Invalid archive record: {path}. Preserve archives and repair the record before retrying."
                ) from exc
            seen.add(int(index))
            result.append((row, raw))
        return result

    def prepare(self):
        sources = self.sources()
        records = self.records()
        matching = [
            (row, raw)
            for row, raw in records
            if row["conversation_id"].casefold() == self.identifier.casefold()
        ]
        pending = [(row, raw) for row, raw in matching if row["phase"] != "complete"]
        if len(pending) > 1:
            raise DevkitError(
                "Multiple unfinished archives exist for this conversation; inspect recovery records before retrying."
            )
        if pending or (matching and all(value == b"" for value in sources.values())):
            self.record, self.record_before = max(pending or matching, key=lambda pair: int(pair[0]["index"]))
            if os.path.normcase(self.record["source_directory"]) != os.path.normcase(str(self.source)):
                raise DevkitError(
                    "Archive record belongs to a different Antigravity profile; no source contents were cleared."
                )
            self.validate_sources(sources)
            return "resume" if pending else "already_archived", sources
        if not all(nonempty(value) for value in sources.values()):
            raise DevkitError(
                "A new archive requires two nonempty UTF-8 documents: implementation_plan.md and task.md."
            )
        maximum = max((int(row["index"]) for row, _ in records), default=0)
        for folder in FOLDERS.values():
            directory = self.path(f"{ARCHIVE}/{folder}")
            if directory.exists():
                for path in directory.iterdir():
                    match = PREFIX.fullmatch(path.name)
                    if match:
                        if len(match[1]) > 18:
                            raise DevkitError(f"Archive index is too large: {path.name}")
                        maximum = max(maximum, int(match[1]))
        if maximum >= 10**18 - 1:
            raise DevkitError("Archive index exceeds the supported 18-digit limit.")
        self.record = {
            "version": 1,
            "index": f"{maximum + 1:03d}",
            "conversation_id": self.identifier,
            "source_directory": str(self.source),
            "created_at": datetime.now(UTC).isoformat(),
            "phase": "copying",
            "files": {k: fingerprint(value) for k, value in sources.items()},
        }
        self.record_before = None
        return "archive", sources

    def validate_sources(self, sources: dict):
        for kind, value in sources.items():
            if value == b"" and self.record["phase"] in {"clearing", "complete"}:
                continue
            if value is None or fingerprint(value) != self.record["files"][kind]:
                raise DevkitError(
                    f"Source changed since archive {self.record['index']}: {NAMES[kind]}. New content will not be cleared; inspect the saved copies and recovery record."
                )

    def save_record(self):
        raw = (json.dumps(self.record, indent=2) + "\n").encode()
        replace(self.record_path(), raw, self.record_before)
        self.record_before = raw

    def ignore(self, *, apply: bool):
        path = self.path(ARCHIVE + "/.gitignore")
        before = read(path, maximum=1024 * 1024)
        patterns = [b"/plans/", b"/tasks/", b"/.devkit-archive/"]
        # Append exclusions after existing rules, including any earlier negations.
        if before is not None and before.rstrip().endswith(b"\n".join(patterns)):
            return
        if apply:
            after = (
                IGNORE
                if before is None
                else before + (b"\n" if not before.endswith(b"\n") else b"") + b"\n" + IGNORE
            )
            replace(path, after, before)

    @contextmanager
    def locked(self):
        path = self.path(INTERNAL + "/archive.lock")
        try:
            write_new(path, (str(os.getpid()) + "\n").encode())
        except FileExistsError as exc:
            raise DevkitError(
                f"Archive lock exists: {path}. Wait for the other command. After a crash, verify no archive command is running before removing this lock and retrying."
            ) from exc
        try:
            yield
        finally:
            path.unlink()

    def archive_bytes(self, kind: str, *, cleanup=False):
        """Recognize the one intentional temporary hard link used for publication."""
        destination, stage = self.destination(kind), self.stage(kind)
        linked = destination.exists() and stage.exists() and destination.samefile(stage)
        data = read(destination, links=2 if linked else 1)
        if linked:
            staged = read(stage, links=2)
            if staged is None or fingerprint(staged) != self.record["files"][kind]:
                raise DevkitError(f"Archive staging checksum mismatch: {stage}")
            if cleanup:
                stage.unlink()
                sync_directory(stage.parent)
        return data

    def verify(self, *, cleanup=False):
        for kind in NAMES:
            data = self.archive_bytes(kind, cleanup=cleanup)
            if data is None or fingerprint(data) != self.record["files"][kind]:
                raise DevkitError(
                    f"Archive copy missing or checksum mismatch: {self.destination(kind)}. Originals will not be cleared."
                )

    def copy(self, kind: str, source: bytes):
        destination = self.destination(kind)
        existing = self.archive_bytes(kind, cleanup=True)
        if existing is not None:
            if fingerprint(existing) != self.record["files"][kind]:
                raise DevkitError(
                    f"Archive destination already exists with different content: {destination}. It will not be overwritten."
                )
            return
        stage = self.stage(kind)
        staged = read(stage)
        if staged is not None and staged != source and not source.startswith(staged):
            raise DevkitError(f"Unexpected archive staging content: {stage}")
        if staged != source:
            replace(stage, source, staged)
        make_directory(destination.parent)
        # Same-filesystem link is exclusive on POSIX and Windows; never replace a destination.
        os.link(stage, destination)
        sync_directory(destination.parent)
        stage.unlink()
        sync_directory(stage.parent)

    def clear(self, kind: str):
        path = self.source / NAMES[kind]
        with opened(path, writable=True) as handle:
            before = os.fstat(handle.fileno())
            data = handle.read(MAX_BYTES + 1)
            if data == b"":
                return
            if fingerprint(data) != self.record["files"][kind]:
                raise DevkitError(f"Source changed before clearing: {path}; new content was preserved.")
            info = safe_path(path).stat()
            actual = os.fstat(handle.fileno())
            if (info.st_dev, info.st_ino, info.st_nlink) != (actual.st_dev, actual.st_ino, 1):
                raise DevkitError(f"Source replaced before clearing: {path}")
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                actual.st_size,
                actual.st_mtime_ns,
                actual.st_ctime_ns,
            ):
                raise DevkitError(f"Source changed while reading before clearing: {path}")
            handle.seek(0)
            handle.truncate(0)
            handle.flush()
            os.fsync(handle.fileno())

    def result(self, status: str, dry_run: bool, error=None):
        files = {}
        for kind, name in NAMES.items():
            row = {"source": str(self.source / name)}
            try:
                data = read(self.source / name)
                row["source_status"] = "missing" if data is None else "empty" if data == b"" else "pending"
                if (
                    self.record
                    and data not in (None, b"")
                    and fingerprint(data) != self.record["files"][kind]
                ):
                    row["source_status"] = "changed"
            except (OSError, DevkitError):
                row["source_status"] = "unreadable"
            if self.record:
                row["archive"] = f"{ARCHIVE}/{FOLDERS[kind]}/{self.record['index']}_{name}"
                try:
                    saved = self.archive_bytes(kind)
                    row["verified"] = saved is not None and fingerprint(saved) == self.record["files"][kind]
                except (OSError, DevkitError):
                    row["verified"] = False
            files[kind] = row
        result = {
            "conversation_id": self.identifier,
            "mode": "dry-run" if dry_run else "apply",
            "status": status,
            "index": self.record["index"] if self.record else None,
            "phase": self.record["phase"] if self.record else None,
            "record": f"{INTERNAL}/{self.record['index']}.json" if self.record else None,
            "files": files,
        }
        if error:
            result["error"] = str(error)
        return result

    def run(self, *, dry_run=False):
        try:
            action, sources = self.prepare()
            self.ignore(apply=False)
            if dry_run:
                if action != "archive":
                    if self.record["phase"] != "copying":
                        self.verify()
                    else:
                        for kind in NAMES:
                            saved = self.archive_bytes(kind)
                            if saved is not None and fingerprint(saved) != self.record["files"][kind]:
                                raise DevkitError(f"Archive destination conflict: {self.destination(kind)}")
                            if saved is None:
                                staged = read(self.stage(kind))
                                if staged is not None and not sources[kind].startswith(staged):
                                    raise DevkitError(
                                        f"Unexpected archive staging content: {self.stage(kind)}"
                                    )
                return self.result(
                    "already_archived" if action == "already_archived" else "would_" + action, True
                ), 0
            self.ignore(apply=True)
            with self.locked():
                # Re-read state and sources under the lock before allocating an index.
                action, sources = self.prepare()
                if action == "already_archived":
                    self.verify(cleanup=True)
                    return self.result(action, False), 0
                if action == "archive":
                    self.save_record()
                if self.record["phase"] == "copying":
                    for kind, source in sources.items():
                        self.copy(kind, source)
                    self.verify(cleanup=True)
                    self.validate_sources(self.sources())
                    self.record["phase"] = "clearing"
                    self.save_record()
                self.verify(cleanup=True)
                self.validate_sources(self.sources())
                for kind in NAMES:
                    self.clear(kind)
                if any(value != b"" for value in self.sources().values()):
                    raise DevkitError(
                        "Antigravity wrote content during clearing; saved copies remain available. Inspect the sources before retrying."
                    )
                self.record["phase"] = "complete"
                self.save_record()
                return self.result("archived" if action == "archive" else "resumed", False), 0
        except (OSError, DevkitError) as exc:
            if self.record_before:
                self.record = json.loads(self.record_before)
            return self.result("failed", dry_run, exc), 2
