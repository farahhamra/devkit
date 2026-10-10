"""Disposable JSON summaries; never deserialize executable objects from a project."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from importlib.metadata import version
from pathlib import Path

from .project import Project

# Bump when the extracted record format or parser behavior changes.
CACHE_VERSION = 1


class ParseCache:
    def __init__(self, project: Project) -> None:
        self.project = project
        self.connection = None
        self.pending = []
        self.scope = str(project.root) + ":" + project.app
        if not project.use_cache:
            return
        settings = {
            "version": CACHE_VERSION,
            "config": project.config,
            "ignore": project.ignore_file.read_text(encoding="utf-8"),
            "parsers": [
                version(p) for p in ("tree-sitter", "tree-sitter-typescript", "tree-sitter-javascript")
            ],
        }
        self.generation = hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()
        try:
            directory = project.ignore_file.parent / ".cache"
            if directory.is_symlink() or (directory / "source.sqlite3").is_symlink():
                raise OSError("Cache symlink paths are not followed")
            directory.mkdir(exist_ok=True)
            self.connection = sqlite3.connect(directory / "source.sqlite3", timeout=0.2)
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS sources (scope TEXT, path TEXT, generation TEXT, "
                "stamp TEXT, data TEXT, PRIMARY KEY (scope, path))"
            )
        except (OSError, sqlite3.Error) as exc:
            self.disable(exc)

    def disable(self, exc: Exception) -> None:
        self.project.warnings.append(f"Source cache unavailable; using live analysis: {exc}")
        if self.connection:
            self.connection.close()
        self.connection = None

    @staticmethod
    def stamp(path: Path) -> str:
        stat = path.stat()
        return f"{stat.st_mtime_ns}:{stat.st_ctime_ns}:{stat.st_size}:{stat.st_ino}"

    def get(self, relative: str, stamp: str) -> dict | None:
        if self.connection is None:
            return None
        try:
            row = self.connection.execute(
                "SELECT data FROM sources WHERE scope=? AND path=? AND generation=? AND stamp=?",
                (self.scope, relative, self.generation, stamp),
            ).fetchone()
            if row:
                digest, separator, payload = row[0].partition("\n")
                if not separator or hashlib.sha256(payload.encode()).hexdigest() != digest:
                    raise ValueError("cached summary checksum mismatch")
                result = json.loads(payload)
                if (
                    not isinstance(result, dict)
                    or not all(
                        isinstance(result.get(k), list)
                        for k in ("symbols", "references", "calls", "warnings")
                    )
                    or not isinstance(result.get("info"), dict)
                ):
                    raise ValueError("invalid cached record")
                info = result["info"]
                if (
                    info.get("path") != relative
                    or not isinstance(info.get("imports"), list)
                    or not isinstance(info.get("exports"), dict)
                ):
                    raise ValueError("invalid cached source metadata")
                # Treat invalid JSON summaries as misses, not executable/trusted input.
                if not all(
                    isinstance(r, dict) and r.get("path") == relative and "name" in r
                    for r in result["symbols"] + result["references"]
                ):
                    raise ValueError("invalid cached locations")
                if not all(
                    isinstance(r, dict) and r.get("path") == relative and "callee" in r
                    for r in result["calls"]
                ):
                    raise ValueError("invalid cached calls")
                return result
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            self.project.warnings.append(f"Ignoring corrupt cache entry for {relative}: {exc}")
        except sqlite3.Error as exc:
            self.disable(exc)
        return None

    def put(self, relative: str, stamp: str, data: dict) -> None:
        if self.connection:
            # Serialize before import resolution mutates the live summaries.
            payload = json.dumps(data)
            checked = hashlib.sha256(payload.encode()).hexdigest() + "\n" + payload
            self.pending.append((self.scope, relative, self.generation, stamp, checked))

    def finish(self, live_paths: set[str] | None = None) -> None:
        if self.connection is None:
            return
        try:
            with self.connection:
                self.connection.executemany(
                    "INSERT OR REPLACE INTO sources VALUES (?, ?, ?, ?, ?)", self.pending
                )
                if live_paths is not None:
                    obsolete = [
                        (self.scope, row[0])
                        for row in self.connection.execute(
                            "SELECT path FROM sources WHERE scope=?", (self.scope,)
                        )
                        if row[0] not in live_paths
                    ]
                    self.connection.executemany("DELETE FROM sources WHERE scope=? AND path=?", obsolete)
        except sqlite3.Error as exc:
            self.disable(exc)
        finally:
            if self.connection:
                self.connection.close()
                self.connection = None
