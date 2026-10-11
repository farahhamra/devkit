"""Project configuration and the single, shared file selection policy."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

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
    import yaml

    resource = Path(__file__).with_name("electroerp.yaml")
    return yaml.safe_load(resource.read_text(encoding="utf-8"))


def detect_root_info(cwd: Path) -> tuple[Path, str]:
    # An explicit Devkit configuration wins over inferred source/Git boundaries.
    for candidate in (cwd, *cwd.parents):
        if (candidate / "devkit/electroerp.yaml").is_file():
            return candidate, "manifest"
    for candidate in (cwd, *cwd.parents):
        if (candidate / "backend").is_dir() and (candidate / "frontend").is_dir():
            return candidate, "source_pair"
        if (candidate / ".git").exists():
            return candidate, "git_boundary"
    return cwd, "cwd_fallback"


def detect_root(cwd: Path) -> Path:
    return detect_root_info(cwd)[0]


def load_configuration(path: Path, explicit: bool = False) -> tuple[dict, dict]:
    """The shared default/override merge. Reading configuration never initializes storage."""
    import yaml

    class UniqueLoader(yaml.SafeLoader):
        def construct_mapping(self, node, deep=False):
            seen = set()
            for key_node, _ in node.value:
                if key_node.tag == "tag:yaml.org,2002:merge":
                    continue
                key = self.construct_object(key_node, deep=deep)
                try:
                    if key in seen:
                        raise yaml.constructor.ConstructorError(
                            None, None, "duplicate mapping key", key_node.start_mark
                        )
                    seen.add(key)
                except TypeError:
                    pass  # The standard constructor reports unsupported mapping keys.
            return super().construct_mapping(node, deep=deep)

    config = default_config()
    custom = {}
    if explicit and not path.is_file():
        raise DevkitError(f"Configuration file not found: {path}")
    if path.exists():
        if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            raise DevkitError("Configuration must be a regular UTF-8 file no larger than 2 MiB.")
        try:
            with path.open("rb") as handle:
                content = handle.read(2 * 1024 * 1024 + 1)
            if len(content) > 2 * 1024 * 1024:
                raise DevkitError("Configuration exceeds the 2 MiB limit.")
            custom = yaml.load(content.decode("utf-8-sig"), Loader=UniqueLoader)
            if custom is None:
                custom = {}
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
            # PyYAML's full exception can expose raw configuration values.
            reason = (
                "duplicate mapping key"
                if getattr(exc, "problem", None) == "duplicate mapping key"
                else "check indentation and value types"
            )
            raise DevkitError(f"Invalid configuration YAML{location}; {reason}.") from exc
        except RecursionError as exc:
            raise DevkitError("Configuration YAML is nested too deeply.") from exc
        except (OSError, UnicodeError) as exc:
            raise DevkitError(
                "Cannot read configuration as UTF-8; check its encoding and permissions."
            ) from exc
        if not isinstance(custom, dict):
            raise DevkitError("Configuration must be a YAML mapping.")
        for key, value in custom.items():
            if key in {"apps", "modules"}:
                if not isinstance(value, dict):
                    raise DevkitError(f"Configuration {key} must be a mapping.")
                config.setdefault(key, {}).update(value)
            else:
                config[key] = value
    return config, custom


class Configuration:
    """Read-only configuration shared by diagnostics and source-analysis projects."""

    def __init__(
        self, root: str | Path | None = None, config: str | Path | None = None, app: str = "all"
    ) -> None:
        self.root = Path(root).expanduser().resolve() if root else detect_root(Path.cwd().resolve())
        if not self.root.is_dir():
            raise DevkitError(f"Project root is not a directory: {self.root}")
        self.config_path = (
            Path(config).expanduser().resolve() if config else self.root / "devkit/electroerp.yaml"
        )
        self.config, self.overrides = load_configuration(self.config_path, explicit=bool(config))
        self.app = app
        self.warnings: list[str] = []
        self._validate_config()
        self.config_dir = (
            Path(os.environ.get("DEVKIT_CONFIG_DIR", str(self.config_path.parent))).expanduser().absolute()
        )
        self.ignore_file = self.config_dir / ".ignore"
        self.feedback_directory = self.config_dir / ".feedback"

    def _validate_config(self) -> None:
        if type(self.config.get("version", 1)) is not int or self.config.get("version", 1) != 1:
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
        modules = self.config.get("modules", {})
        if not isinstance(modules, dict):
            raise DevkitError("modules must be a mapping")
        for name, entry in modules.items():
            if not isinstance(entry, dict):
                raise DevkitError(f"modules.{name} must be a mapping")
            if not isinstance(name, str) or not name:
                raise DevkitError("Module names must be nonempty strings")
            if not isinstance(entry.get("app", "erp"), str) or entry.get("app", "erp") not in apps:
                raise DevkitError(f"Unknown app for modules.{name}")
            for key in ("models", "aliases", "paths", "required"):
                if key in entry and (
                    not isinstance(entry[key], list) or not all(isinstance(x, str) and x for x in entry[key])
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
            if item.is_symlink() or (
                item.exists() and getattr(item.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400
            ):
                raise DevkitError(f"Symlink paths are not followed: {path}")
        resolved = candidate.resolve()
        if not _inside(resolved, self.root):
            raise DevkitError(f"Path is outside the project root: {path}")
        if must_exist and not resolved.exists():
            raise DevkitError(f"Path does not exist: {path}")
        return resolved


class Project(Configuration):
    def __init__(self, root=None, config=None, app="all") -> None:
        import pathspec

        super().__init__(root, config, app)
        self._files: list[Path] | None = None
        self._texts: dict[Path, str | None] = {}
        self._app_roots: dict[str, list[Path]] = {}
        self._app_for: dict[Path, str | None] = {}
        self.use_cache = True
        try:
            self.config_dir.mkdir(parents=True, exist_ok=True)
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
