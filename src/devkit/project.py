"""Project configuration and the single, shared file selection policy."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

import pathspec
import yaml

DEFAULT_IGNORE = """# Devkit exclusions, relative to the ElectroERP project root.
# This is the only ignore file Devkit reads. Git ignore files are not consulted.
.git/
node_modules/
.venv/
venv/
__pycache__/
.next/
dist/
build/
coverage/
.pytest_cache/
.ruff_cache/
*.tsbuildinfo
*.log
.env
.env.*
!.env.example
!.env.sample
private/logs/
public/files/
backend/public/files/
backend/ai/memory/data/
devkit/.cache/
"""

SOURCE_EXTENSIONS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts"}


class DevkitError(Exception):
    """An actionable user-facing error."""


def _inside(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def default_config() -> dict[str, Any]:
    resource = Path(__file__).with_name("electroerp.yaml")
    return yaml.safe_load(resource.read_text(encoding="utf-8"))


def detect_root(cwd: Path) -> Path:
    # An explicit Devkit configuration wins over inferred source/Git boundaries.
    for candidate in (cwd, *cwd.parents):
        if (candidate / "devkit/electroerp.yaml").is_file():
            return candidate
    for candidate in (cwd, *cwd.parents):
        if (candidate / "backend").is_dir() and (candidate / "frontend").is_dir():
            return candidate
        if (candidate / ".git").exists():
            return candidate
    return cwd


class Project:
    def __init__(
        self, root: str | Path | None = None, config: str | Path | None = None, app: str = "all"
    ) -> None:
        self.root = Path(root).expanduser().resolve() if root else detect_root(Path.cwd().resolve())
        if not self.root.is_dir():
            raise DevkitError(f"Project root is not a directory: {self.root}")
        self.config_path = (
            Path(config).expanduser().resolve() if config else self.root / "devkit/electroerp.yaml"
        )
        self.config = default_config()
        if config and not self.config_path.is_file():
            raise DevkitError(f"Configuration file not found: {self.config_path}")
        if self.config_path.is_file():
            try:
                custom = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
            except (yaml.YAMLError, OSError) as exc:
                raise DevkitError(f"Cannot read configuration: {exc}") from exc
            if not isinstance(custom, dict):
                raise DevkitError("Configuration must be a YAML mapping.")
            for key, value in custom.items():
                if key in {"apps", "modules"}:
                    if not isinstance(value, dict):
                        raise DevkitError(f"Configuration {key} must be a mapping.")
                    self.config.setdefault(key, {}).update(value)
                else:
                    self.config[key] = value
        self.app = app
        self.warnings: list[str] = []
        self._files: list[Path] | None = None
        self._texts: dict[Path, str | None] = {}
        self._app_roots: dict[str, list[Path]] = {}
        self._app_for: dict[Path, str | None] = {}
        self.use_cache = True
        self._validate_config()
        config_dir = Path(os.environ.get("DEVKIT_CONFIG_DIR", str(self.config_path.parent))).expanduser()
        self.ignore_file = config_dir / ".ignore"
        self.feedback_directory = (config_dir / ".feedback").absolute()
        try:
            config_dir.mkdir(parents=True, exist_ok=True)
            try:
                with self.ignore_file.open("x", encoding="utf-8") as handle:
                    handle.write(DEFAULT_IGNORE)
            except FileExistsError:
                pass
            self.ignore = pathspec.GitIgnoreSpec.from_lines(
                self.ignore_file.read_text(encoding="utf-8").splitlines()
            )
        except OSError as exc:
            raise DevkitError(f"Cannot initialize/read Devkit ignore file {self.ignore_file}: {exc}") from exc
        if app != "all" and not self.app_available(app):
            raise DevkitError(
                f"App '{app}' has no available source root. Configure apps.{app}.roots in {self.config_path}."
            )

    def _validate_config(self) -> None:
        if self.config.get("version", 1) != 1:
            raise DevkitError("Unsupported manifest version; expected version: 1")
        apps = self.config.get("apps")
        if not isinstance(apps, dict):
            raise DevkitError("apps must be a mapping")
        for name, entry in apps.items():
            if name not in {"erp", "hub", "manager"}:
                raise DevkitError(f"Unknown app key: {name}; use erp, hub or manager")
            if not isinstance(entry, dict):
                raise DevkitError(f"apps.{name} must be a mapping")
            for key in ("roots", "schemas"):
                if not isinstance(entry.get(key, []), list) or not all(
                    isinstance(p, str) and p for p in entry.get(key, [])
                ):
                    raise DevkitError(f"apps.{name}.{key} must be a list of nonempty paths")
            for path in entry.get("roots", []) + entry.get("schemas", []):
                self.resolve(path, must_exist=False)
        for name, entry in self.config.get("modules", {}).items():
            if not isinstance(entry, dict):
                raise DevkitError(f"modules.{name} must be a mapping")
            if not isinstance(name, str) or not name:
                raise DevkitError("Module names must be nonempty strings")
            if entry.get("app", "erp") not in apps:
                raise DevkitError(f"Unknown app for modules.{name}")
            for key in ("models", "aliases", "paths", "required"):
                if key in entry and (
                    not isinstance(entry[key], list) or not all(isinstance(x, str) for x in entry[key])
                ):
                    raise DevkitError(f"modules.{name}.{key} must be a list of strings")
            for path in entry.get("paths", []) + entry.get("required", []):
                self.resolve(path, must_exist=False)
        shared = self.config.get("shared_models", [])
        if not isinstance(shared, list) or not all(isinstance(s, str) for s in shared):
            raise DevkitError("shared_models must be a list of names")
        aliases = self.config.get("import_aliases", {})
        if not isinstance(aliases, dict) or not all(
            isinstance(k, str) and k and isinstance(v, str) and v for k, v in aliases.items()
        ):
            raise DevkitError("import_aliases must map nonempty prefixes to project-relative directories")
        for target in aliases.values():
            self.resolve(target, must_exist=False)

    def rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def resolve(self, path: str | Path, must_exist: bool = True) -> Path:
        raw = Path(path)
        candidate = raw if raw.is_absolute() else self.root / raw
        # Refuse even internal symlinks for predictable identical traversal rules.
        if not _inside(candidate.absolute(), self.root):
            raise DevkitError(f"Path is outside the project root: {path}")
        for item in (candidate, *candidate.parents):
            if item == self.root:
                break
            if item.is_symlink():
                raise DevkitError(f"Symlink paths are not followed: {path}")
        resolved = candidate.resolve()
        if not _inside(resolved, self.root):
            raise DevkitError(f"Path is outside the project root: {path}")
        if must_exist and not resolved.exists():
            raise DevkitError(f"Path does not exist: {path}")
        return resolved

    def app_roots(self, app: str) -> list[Path]:
        if app in self._app_roots:
            return self._app_roots[app]
        entry = self.config["apps"].get(app)
        if entry is None:
            raise DevkitError(f"Unknown app: {app}")
        roots = [self.resolve(p, must_exist=False) for p in entry.get("roots", [])]
        self._app_roots[app] = roots
        return roots

    def app_available(self, app: str) -> bool:
        return any(p.is_dir() for p in self.app_roots(app))

    def app_for(self, path: Path) -> str | None:
        if path in self._app_for:
            return self._app_for[path]
        candidates = [
            (len(root.parts), app)
            for app in self.config["apps"]
            for root in self.app_roots(app)
            if _inside(path, root)
        ]
        result = max(candidates)[1] if candidates else None
        self._app_for[path] = result
        return result

    def allowed(self, path: Path, directory: bool = False) -> bool:
        # Durable reports may contain diagnostics. They are internal state, not
        # project source, and must not recursively feed searches/module analysis.
        if _inside(path, self.feedback_directory) or _inside(path, self.root / ".agents/.devkit-sync"):
            return False
        relative = self.rel(path) + ("/" if directory else "")
        return not self.ignore.match_file(relative)

    def in_scope(self, path: Path, directory: bool = False) -> bool:
        if self.app == "all":
            return True
        return any(
            _inside(path, root) or (directory and _inside(root, path)) for root in self.app_roots(self.app)
        )

    def entries(self, path: str | Path = ".", *, directories: bool = False) -> list[Path]:
        target = self.resolve(path)
        # Explicit targets cannot bypass ignore rules on themselves or ancestors.
        for ancestor in (target, *target.parents):
            if ancestor == self.root:
                break
            if not self.allowed(ancestor, ancestor.is_dir()):
                return []
        if not self.in_scope(target, target.is_dir()):
            raise DevkitError(f"Path is outside selected app '{self.app}': {path}")
        if target.is_file():
            return [target] if not directories else []
        found: list[Path] = []

        def on_error(exc: OSError) -> None:
            self.warnings.append(f"Cannot traverse {exc.filename}: {exc.strerror}")

        for current, dirs, files in os.walk(target, followlinks=False, onerror=on_error):
            parent = Path(current)
            dirs[:] = sorted(
                d
                for d in dirs
                if not (parent / d).is_symlink()
                and self.allowed(parent / d, True)
                and self.in_scope(parent / d, True)
            )
            if directories:
                found.extend(parent / d for d in dirs)
            else:
                found.extend(
                    parent / f
                    for f in sorted(files)
                    if not (parent / f).is_symlink()
                    and (parent / f).is_file()
                    and self.allowed(parent / f)
                    and self.in_scope(parent / f)
                )
        return sorted(found)

    def files(self) -> list[Path]:
        if self._files is None:
            self._files = self.entries()
        return self._files

    def text(self, path: Path) -> str | None:
        if path not in self._texts:
            try:
                # A FIFO or device must never block source discovery. Recheck after traversal.
                if not stat.S_ISREG(path.stat().st_mode):
                    self._texts[path] = None
                    return None
                with path.open("rb") as handle:
                    head = handle.read(8192)
                    if b"\x00" in head:
                        self._texts[path] = None
                        return None
                    data = head + handle.read()
                self._texts[path] = None if b"\x00" in data else data.decode("utf-8-sig")
            except UnicodeDecodeError:
                self._texts[path] = None
            except OSError as exc:
                self.warnings.append(f"Cannot read {self.rel(path)}: {exc.strerror}")
                self._texts[path] = None
        return self._texts[path]

    def schema_paths(self) -> list[Path]:
        eligible = set(self.files())
        paths = []
        for app, entry in self.config["apps"].items():
            if self.app != "all" and app != self.app:
                continue
            paths.extend(self.resolve(p, must_exist=False) for p in entry.get("schemas", []))
        return sorted({p for p in paths if p in eligible} | {p for p in eligible if p.suffix == ".prisma"})
