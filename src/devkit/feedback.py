"""Explicit, local-only feedback. Reports are durable data, never executable instructions."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .project import DevkitError, detect_root

TYPES = ("bug", "performance", "usability", "feature")
SEVERITIES = ("low", "medium", "high", "critical")
EVIDENCE = ("observed", "suspected")
STATUSES = ("open", "investigating", "resolved")
STORAGE_VERSION = 1
GITIGNORE = "# Local Devkit feedback; not application source.\n*\n!.gitignore\n"
TEXT_LIMITS = {
    "title": 200,
    "command": 2000,
    "description": 4000,
    "expected": 4000,
    "actual": 4000,
    "diagnostics": 12000,
    "note": 4000,
    "step": 2000,
}
SECRET_NAME = r"[\w.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|cookie|database[_-]?url|connection[_-]?string|private[_-]?key)[\w.-]*"


def sanitize(value: str) -> tuple[str, bool]:
    """Best-effort redaction, not a guarantee that arbitrary pasted data is safe."""
    original = value
    value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)
    value = re.sub(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)",
        "[REDACTED PRIVATE KEY]",
        value,
    )
    value = re.sub(
        r"""(?im)(\b(?:(?:proxy-)?authorization|(?:set-)?cookie)["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\r\n]+)""",
        r"\1[REDACTED]",
        value,
    )
    value = re.sub(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9+/_.=-]+", r"\1 [REDACTED]", value)
    value = re.sub(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@]+@", r"\1[REDACTED]@", value)
    value = re.sub(
        r"""(?ix)(?<![\w.-])((?:["']?"""
        + SECRET_NAME
        + r"""["']?\s*[:=]\s*|--"""
        + SECRET_NAME
        + r"""\s+))(?:"[^"]*"|'[^']*'|[^\s,;&}\]]+)""",
        r"\1[REDACTED]",
        value,
    )
    value = re.sub(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\b",
        "[REDACTED]",
        value,
    )
    return value, value != original


def clean(name: str, value: str | None) -> tuple[str, bool]:
    value = (value or "").strip()
    if len(value) > TEXT_LIMITS[name]:
        raise DevkitError(
            f"Feedback {name} exceeds {TEXT_LIMITS[name]} characters; provide a smaller sanitized excerpt."
        )
    result, changed = sanitize(value)
    if name == "title" and not result.strip():
        raise DevkitError("Feedback title cannot be empty.")
    return result, changed


class FeedbackContext:
    """Locate storage without parsing possibly broken YAML or requiring an available app."""

    def __init__(self, root=None, config=None, app="all") -> None:
        self.root = Path(root).expanduser().resolve() if root else detect_root(Path.cwd().resolve())
        if not self.root.is_dir():
            raise DevkitError(f"Project root is not a directory: {self.root}")
        config_path = Path(config).expanduser().absolute() if config else self.root / "devkit/electroerp.yaml"
        config_dir = (
            Path(os.environ.get("DEVKIT_CONFIG_DIR", str(config_path.parent))).expanduser().absolute()
        )
        self.directory = config_dir / ".feedback"
        self.app = app
        self.warnings: list[str] = []


class FeedbackStore:
    def __init__(self, context: FeedbackContext) -> None:
        self.context = context
        self.directory = context.directory
        self.path = self.directory / "reports.sqlite3"
        # A shared DEVKIT_CONFIG_DIR must not mix feedback from different projects.
        self.project = hashlib.sha256(os.path.normcase(str(context.root)).encode()).hexdigest()

    @contextmanager
    def connection(self, *, write=False):
        connection = None
        try:
            for candidate in (
                self.path,
                *self.path.parents,
                self.directory / ".gitignore",
                self.directory / "reports.sqlite3-journal",
                self.directory / "reports.sqlite3-wal",
                self.directory / "reports.sqlite3-shm",
            ):
                if candidate.is_symlink() or (getattr(candidate, "is_junction", lambda: False)()):
                    raise DevkitError("Feedback storage symlink/junction paths are not followed.")
            if not write and not self.path.exists():
                yield None
                return
            if self.path.exists() and not self.path.is_file():
                raise DevkitError("Feedback database must be a regular file.")
            if write:
                self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                try:
                    self.path.touch(mode=0o600, exist_ok=False)
                except FileExistsError:
                    pass
                connection = sqlite3.connect(self.path, timeout=2)
            else:
                connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=2)
            with connection:
                if write:
                    connection.execute("BEGIN IMMEDIATE")
                    # Serialize first-use initialization with report writes, so
                    # another agent cannot observe a partially written ignore file.
                    ignore = self.directory / ".gitignore"
                    if ignore.exists() and not ignore.is_file():
                        raise DevkitError("Feedback storage .gitignore must be a regular file.")
                    try:
                        with ignore.open("x", encoding="utf-8") as handle:
                            handle.write(GITIGNORE)
                    except FileExistsError:
                        if ignore.read_text(encoding="utf-8") != GITIGNORE:
                            raise DevkitError(
                                "Feedback storage .gitignore was customized; restore its documented exclusions before saving reports."
                            ) from None
                else:
                    connection.execute("BEGIN")
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if (
                    write
                    and version == 0
                    and not connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone()
                ):
                    connection.execute(
                        "CREATE TABLE reports (id TEXT PRIMARY KEY, project TEXT NOT NULL, fingerprint TEXT NOT NULL, app TEXT NOT NULL, type TEXT NOT NULL, severity TEXT NOT NULL, evidence TEXT NOT NULL, status TEXT NOT NULL, updated_at TEXT NOT NULL, payload TEXT NOT NULL, UNIQUE(project, fingerprint))"
                    )
                    connection.execute("CREATE INDEX report_listing ON reports(project, updated_at, id)")
                    connection.execute(f"PRAGMA user_version={STORAGE_VERSION}")
                elif version != STORAGE_VERSION:
                    raise DevkitError(
                        "Unsupported feedback storage version. Keep the database and use a compatible Devkit version."
                    )
                yield connection
        except sqlite3.Error as exc:
            raise DevkitError(
                f"Cannot access local feedback storage: {exc}. Check permissions/locks or restore a backup; reports are not disposable cache."
            ) from exc
        finally:
            if connection:
                connection.close()

    @staticmethod
    def decode(payload, report_id=None):
        try:
            record = json.loads(payload)
            if not isinstance(record, dict) or not all(
                key in record
                for key in (
                    "id",
                    "type",
                    "severity",
                    "evidence",
                    "status",
                    "title",
                    "app",
                    "occurrences",
                    "created_at",
                    "updated_at",
                    "history",
                    "environment",
                )
            ):
                raise ValueError("invalid report shape")
            if not isinstance(record["history"], list) or not isinstance(record["occurrences"], int):
                raise TypeError("invalid report history/count")
            strings = (
                "id",
                "type",
                "severity",
                "evidence",
                "status",
                "title",
                "app",
                "command",
                "description",
                "expected",
                "actual",
                "diagnostics",
                "created_at",
                "updated_at",
                "last_seen_at",
            )
            if not all(isinstance(record.get(key), str) for key in strings):
                raise TypeError("invalid report text")
            if not re.fullmatch(r"fb_[0-9a-f]{32}", record["id"]) or (
                report_id and record["id"] != report_id
            ):
                raise ValueError("invalid report identity")
            for key, choices in (
                ("type", TYPES),
                ("severity", SEVERITIES),
                ("evidence", EVIDENCE),
                ("status", STATUSES),
            ):
                if record[key] not in choices:
                    raise ValueError("invalid report classification")
            if not all(
                isinstance(record.get(key), list) and all(isinstance(v, str) for v in record[key])
                for key in ("steps", "redacted_fields")
            ):
                raise TypeError("invalid report text list")
            if (
                not isinstance(record["environment"], dict)
                or not all(isinstance(row, dict) for row in record["history"])
                or record["occurrences"] < 1
            ):
                raise ValueError("invalid report metadata")
            return record
        except (ValueError, TypeError) as exc:
            raise DevkitError(
                "Feedback report data is corrupt; keep the database and restore a backup. No reports were discarded."
            ) from exc

    def get(self, connection, report_id):
        if not re.fullmatch(r"fb_[0-9a-f]{32}", report_id):
            raise DevkitError("Invalid feedback ID; use devkit feedback list.")
        row = (
            connection.execute(
                "SELECT payload FROM reports WHERE project=? AND id=?", (self.project, report_id)
            ).fetchone()
            if connection
            else None
        )
        if not row:
            raise DevkitError("Feedback report not found in this project.")
        record = self.decode(row[0], report_id)
        if self.context.app != "all" and record["app"] != self.context.app:
            raise DevkitError("Feedback report is outside the selected app scope.")
        return record

    @staticmethod
    def now():
        return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")

    def save(self, connection, record):
        cursor = connection.execute(
            "UPDATE reports SET severity=?, evidence=?, status=?, updated_at=?, payload=? WHERE project=? AND id=?",
            (
                record["severity"],
                record["evidence"],
                record["status"],
                record["updated_at"],
                json.dumps(record, ensure_ascii=False),
                self.project,
                record["id"],
            ),
        )

        if cursor.rowcount != 1:
            raise DevkitError("Feedback report identity is inconsistent; no update was saved.")

    def add(self, args):
        fields = {}
        redacted = []
        for name in ("title", "command", "description", "expected", "actual", "diagnostics"):
            value = getattr(args, "reported_command" if name == "command" else name)
            fields[name], changed = clean(name, value)
            if changed:
                redacted.append(name)
        if len(args.step) > 20:
            raise DevkitError("At most 20 reproduction steps are allowed.")
        fields["steps"] = []
        for i, value in enumerate(args.step):
            step, changed = clean("step", value)
            fields["steps"].append(step)
            if changed:
                redacted.append(f"steps[{i}]")
        if (
            sum(len(v) for v in fields.values() if isinstance(v, str)) + sum(map(len, fields["steps"]))
            > 32000
        ):
            raise DevkitError("Feedback text exceeds 32000 characters in total; provide a smaller report.")
        if redacted:
            self.context.warnings.append(
                "Feedback text was sanitized/redacted before storage. Review the report; automatic redaction is best-effort."
            )
        fingerprint = hashlib.sha256(
            json.dumps(
                {"type": args.type, "app": self.context.app, "version": __version__, **fields}, sort_keys=True
            ).encode()
        ).hexdigest()
        with self.connection(write=True) as connection:
            existing = connection.execute(
                "SELECT id, payload FROM reports WHERE project=? AND fingerprint=?",
                (self.project, fingerprint),
            ).fetchone()
            now = self.now()
            if existing:
                record = self.decode(existing[1], existing[0])
                record["occurrences"] += 1
                record["updated_at"] = record["last_seen_at"] = now
                self.save(connection, record)
                self.context.warnings.append(
                    "Duplicate report reused; severity, evidence and status were not changed. Use feedback update to revise them."
                )
                return {"duplicate": True, "report": record}
            record = {
                "id": "fb_" + uuid.uuid4().hex,
                "type": args.type,
                "severity": args.severity,
                "evidence": args.evidence,
                "status": "open",
                "app": self.context.app,
                **fields,
                "environment": {
                    "devkit": __version__,
                    "python": platform.python_version(),
                    "os": platform.system(),
                },
                "created_at": now,
                "updated_at": now,
                "last_seen_at": now,
                "occurrences": 1,
                "redacted_fields": redacted,
                "history": [],
            }
            connection.execute(
                "INSERT INTO reports VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record["id"],
                    self.project,
                    fingerprint,
                    record["app"],
                    record["type"],
                    record["severity"],
                    record["evidence"],
                    record["status"],
                    now,
                    json.dumps(record, ensure_ascii=False),
                ),
            )
            return {"duplicate": False, "report": record}

    def list(self, args):
        with self.connection() as connection:
            if connection is None:
                return {"reports": [], "total": 0, "offset": args.offset, "next_offset": None}
            conditions = ["project=?"]
            values = [self.project]
            for name in ("type", "severity", "evidence", "status"):
                value = getattr(args, name)
                if value:
                    conditions.append(name + "=?")
                    values.append(value)
            if self.context.app != "all":
                conditions.append("app=?")
                values.append(self.context.app)
            where = " AND ".join(conditions)
            total = connection.execute("SELECT COUNT(*) FROM reports WHERE " + where, values).fetchone()[0]
            rows = connection.execute(
                "SELECT id, payload FROM reports WHERE "
                + where
                + " ORDER BY updated_at DESC, id LIMIT ? OFFSET ?",
                [*values, args.max_results + 1, args.offset],
            ).fetchall()
            keys = (
                "id",
                "type",
                "severity",
                "evidence",
                "status",
                "title",
                "app",
                "occurrences",
                "updated_at",
            )
            reports = [
                {k: record[k] for k in keys}
                for report_id, payload in rows
                if (record := self.decode(payload, report_id))
            ]
            return {
                "reports": reports,
                "total": total,
                "offset": args.offset,
                "next_offset": args.offset + args.max_results if len(reports) > args.max_results else None,
            }

    def inspect(self, report_id):
        with self.connection() as connection:
            return self.get(connection, report_id)

    def update(self, args):
        changes = {
            key: getattr(args, key)
            for key in ("status", "severity", "evidence")
            if getattr(args, key) is not None
        }
        note, redacted = clean("note", args.note)
        if not changes and not note:
            raise DevkitError("Provide --status, --severity, --evidence or --note to update feedback.")
        if args.status == "resolved" and not note:
            raise DevkitError("Resolving feedback requires --note describing the fix or verification.")
        if redacted:
            self.context.warnings.append("Feedback note was sanitized/redacted before storage.")
        # Do not create an empty database for an invalid update.
        self.inspect(args.report_id)
        with self.connection(write=True) as connection:
            record = self.get(connection, args.report_id)
            changes = {
                key: {"from": record[key], "to": value}
                for key, value in changes.items()
                if record[key] != value
            }
            if not changes and not note:
                return {"updated": False, "report": record}
            now = self.now()
            record["history"].append({"at": now, "changes": changes, "note": note, "redacted": redacted})
            record.update({key: value["to"] for key, value in changes.items()})
            record["updated_at"] = now
            self.save(connection, record)
            return {"updated": True, "report": record}
