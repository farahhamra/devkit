"""Read-only configuration inspection and local installation diagnostics."""

from __future__ import annotations

import importlib
import os
import platform
import re
import shutil
import stat
import sys
import sysconfig
from importlib import metadata
from pathlib import Path

from . import __version__
from .agents import IGNORE, INTERNAL, AgentsContext, AgentSync
from .feedback import GITIGNORE, sanitize
from .project import DEFAULT_IGNORE, Configuration, DevkitError, detect_root_info

LIMIT = 2 * 1024 * 1024
APP_FIELDS = {"roots", "schemas"}
MODULE_FIELDS = {"app", "models", "aliases", "paths", "required"}
CONFIG_FIELDS = {"version", "apps", "modules", "shared_models", "import_aliases"}
DEPENDENCIES = {
    "PyYAML": "yaml",
    "pathspec": "pathspec",
    "tree-sitter": "tree_sitter",
    "tree-sitter-javascript": "tree_sitter_javascript",
    "tree-sitter-typescript": "tree_sitter_typescript",
}


def probe(path: Path) -> dict:
    """No file creation, directory listing, or DB connection; permissions are estimates."""
    path = path.absolute()
    nearest = None
    info = None
    try:
        for item in (path, *path.parents):
            try:
                current = item.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(current.st_mode) or getattr(current, "st_file_attributes", 0) & 0x400:
                return {
                    "path": str(path),
                    "state": "unsafe",
                    "reason": "Symlink/junction path is not followed",
                }
            if item != path and not stat.S_ISDIR(current.st_mode):
                return {"path": str(path), "state": "unsafe", "reason": "Parent is not a directory"}
            if nearest is None:
                nearest = item
            if item == path:
                info = current
        if info is None:
            return {
                "path": str(path),
                "state": "missing",
                "nearest_parent": str(nearest),
                "creatable_estimate": bool(nearest and os.access(nearest, os.W_OK | os.X_OK)),
            }
        kind = (
            "directory" if stat.S_ISDIR(info.st_mode) else "file" if stat.S_ISREG(info.st_mode) else "special"
        )
        access = os.R_OK | os.X_OK if kind == "directory" else os.R_OK
        return {
            "path": str(path),
            "state": kind,
            "readable_estimate": os.access(path, access),
            "writable_estimate": os.access(path, os.W_OK | os.X_OK if kind == "directory" else os.W_OK),
            "size": info.st_size,
            "hardlinked": kind == "file" and info.st_nlink > 1,
        }
    except OSError as exc:
        return {"path": str(path), "state": "inaccessible", "reason": exc.strerror or type(exc).__name__}


def read_text(path: Path) -> str:
    info = probe(path)
    if info["state"] != "file" or not info["readable_estimate"]:
        raise DevkitError(f"Not a readable regular file: {path}")
    if info["size"] > LIMIT:
        raise DevkitError(f"Diagnostic input exceeds the 2 MiB limit: {path}")
    with path.open("rb") as handle:
        content = handle.read(LIMIT + 1)
    if len(content) > LIMIT:
        raise DevkitError(f"Diagnostic input exceeds the 2 MiB limit: {path}")
    try:
        return content.decode("utf-8")
    except UnicodeError as exc:
        raise DevkitError(f"Diagnostic input must use UTF-8: {path}") from exc


class DiagnosticContext:
    def __init__(self, root=None, config=None, app="all") -> None:
        self.root, self.root_reason = (
            (Path(root).expanduser().resolve(), "explicit")
            if root
            else detect_root_info(Path.cwd().resolve())
        )
        if not self.root.is_dir():
            raise DevkitError(f"Project root is not a directory: {self.root}")
        self.app = app
        self.explicit_config = config
        self.config_path = (
            Path(config).expanduser().resolve() if config else self.root / "devkit/electroerp.yaml"
        )
        self.config_dir = (
            Path(os.environ.get("DEVKIT_CONFIG_DIR", str(self.config_path.parent))).expanduser().absolute()
        )
        self.warnings: list[str] = []
        self.use_cache = True

    def selection(self):
        manifest = probe(self.config_path)
        return {
            "root": str(self.root),
            "root_selection": self.root_reason,
            "manifest": {
                "path": str(self.config_path),
                "selection": "explicit" if self.explicit_config else "default",
                "exists": manifest["state"] == "file",
                "state": manifest["state"],
            },
            "configuration_directory": str(self.config_dir),
            "configuration_directory_source": "DEVKIT_CONFIG_DIR"
            if "DEVKIT_CONFIG_DIR" in os.environ
            else "manifest_directory",
            "ignore_file": str(self.config_dir / ".ignore"),
            "storage": {
                "cache": str(self.config_dir / ".cache/source.sqlite3"),
                "feedback": str(self.config_dir / ".feedback/reports.sqlite3"),
                "agent_sync": str(self.root / INTERNAL),
            },
        }


class Diagnostics:
    def __init__(self, context: DiagnosticContext) -> None:
        self.context = context
        self.checks: list[dict] = []

    def add(self, code, status, message, remediation=None, **details):
        self.checks.append(
            {
                "id": code,
                "status": status,
                "message": message,
                **({"remediation": remediation} if remediation else {}),
                **({"details": details} if details else {}),
            }
        )

    def public(self, data):
        """Never include unknown configuration values or raw YAML/agent/source contents."""
        changed = False

        def visit(value):
            nonlocal changed
            if isinstance(value, str):
                result, redacted = sanitize(value)
                changed |= redacted
                return result
            if isinstance(value, dict):
                return {visit(str(k)): visit(v) for k, v in value.items()}
            if isinstance(value, list):
                return [visit(v) for v in value]
            return value

        result = visit(data)
        if changed:
            self.context.warnings.append(
                "Diagnostic text was sanitized/redacted. Review paths before sharing; redaction is best-effort."
            )
        return result

    def load(self):
        return Configuration(self.context.root, self.context.explicit_config, self.context.app)

    def ignore(self, config):
        import pathspec

        info = probe(config.ignore_file)
        text = DEFAULT_IGNORE if info["state"] == "missing" else read_text(config.ignore_file)
        spec = pathspec.GitIgnoreSpec.from_lines(text.splitlines())
        return spec, {
            "path": str(config.ignore_file),
            "source": "bundled_defaults_not_created" if info["state"] == "missing" else "existing_file",
            "patterns": [line for line in text.splitlines() if line.strip() and not line.startswith("#")],
            "internal_exclusions": [str(config.feedback_directory), str(config.root / INTERNAL)],
        }

    @staticmethod
    def allowed(config, spec, path):
        if (
            path == config.feedback_directory
            or config.feedback_directory in path.parents
            or path == config.root / INTERNAL
            or config.root / INTERNAL in path.parents
        ):
            return False
        for candidate in (path, *path.parents):
            if candidate == config.root:
                break
            relative = config.rel(candidate) + ("/" if candidate.is_dir() else "")
            if spec.match_file(relative):
                return False
        return True

    @staticmethod
    def unknown_fields(config):
        unknown = [str(k) for k in config.config if k not in CONFIG_FIELDS]
        for group, fields in (("apps", APP_FIELDS), ("modules", MODULE_FIELDS)):
            for name, entry in config.config[group].items():
                unknown.extend(f"{group}.{name}.{key}" for key in entry if key not in fields)
        return sorted(unknown)

    def inspect(self):
        try:
            config = self.load()
            _, ignore = self.ignore(config)
        except (DevkitError, OSError, ImportError, ValueError) as exc:
            raise DevkitError(sanitize(str(exc))[0]) from exc
        origins = lambda group, name: (
            "manifest_override" if name in config.overrides.get(group, {}) else "bundled_default"
        )
        data = {
            "selection": self.context.selection(),
            "merge_policy": "Apps/modules merge by name; each supplied entry replaces the whole default entry. Other supplied top-level values replace their defaults.",
            "top_level_sources": {
                key: "manifest_override" if key in config.overrides else "bundled_default"
                for key in ("version", "shared_models", "import_aliases")
            },
            "effective": {
                "version": config.config.get("version", 1),
                "apps": [
                    {
                        "name": name,
                        "selected": self.context.app in {"all", name},
                        "source": origins("apps", name),
                        "roots": entry.get("roots", []),
                        "schemas": entry.get("schemas", []),
                    }
                    for name, entry in config.config["apps"].items()
                ],
                "modules": [
                    {
                        "name": name,
                        "source": origins("modules", name),
                        "app": entry.get("app", "erp"),
                        **{key: entry.get(key, []) for key in ("models", "aliases", "paths", "required")},
                    }
                    for name, entry in config.config["modules"].items()
                ],
                "shared_models": config.config.get("shared_models", []),
                "import_aliases": [
                    {"prefix": prefix, "path": path}
                    for prefix, path in config.config.get("import_aliases", {}).items()
                ],
            },
            "ignore": ignore,
            "unrecognized_fields": self.unknown_fields(config),
            "limitations": [
                "Inspection shows configured mappings, not convention-discovered modules or proven file ownership. Use module inspect for ownership evidence.",
                "Unknown field values are omitted. No environment dump, source scan or filesystem initialization is performed.",
                "Relative --config and DEVKIT_CONFIG_DIR paths resolve from the current working directory; source paths resolve from the project root. DEVKIT_CONFIG_DIR does not select the manifest.",
                "Inspection does not test configured paths for availability; run config validate or doctor.",
            ],
        }
        if data["unrecognized_fields"]:
            self.context.warnings.append(
                "Unrecognized configuration fields are not used by supported analysis settings; their values are omitted."
            )
        return self.public(data), 0

    def configuration_checks(self):
        self.add(
            "project.root",
            "pass",
            "Selected project root",
            path=str(self.context.root),
            selected_by=self.context.root_reason,
        )
        try:
            config = self.load()
        except (DevkitError, OSError, ImportError, ValueError) as exc:
            self.add(
                "configuration.load",
                "fail",
                str(exc),
                "Check --root / --config, YAML structure and installed PyYAML. No defaults were substituted for a broken explicit manifest.",
            )
            return
        self.add(
            "configuration.load",
            "pass",
            "Configuration loaded and structurally validated",
            source="manifest_plus_defaults" if config.config_path.is_file() else "bundled_defaults",
            manifest=str(config.config_path),
        )
        for candidate in (
            self.context.root / ".agents/devkit/electroerp.yaml",
            self.context.root / "electroerp.yaml",
        ):
            if not self.context.explicit_config and candidate.is_file() and candidate != config.config_path:
                self.add(
                    "configuration.unselected_manifest",
                    "warning",
                    "Another manifest exists but is not selected",
                    "Use --config with this path if it is the intended override; config-directory overrides do not select manifests.",
                    path=str(candidate),
                )
        if self.context.root.name == "devkit" and self.context.root.parent.name == ".agents":
            self.add(
                "project.nested_checkout",
                "warning",
                "The selected root looks like the nested Devkit checkout",
                "Pass --root for ElectroERP, not its .agents/devkit checkout.",
                possible_project=str(self.context.root.parent.parent),
            )
        unknown = self.unknown_fields(config)
        if unknown:
            self.add(
                "configuration.unrecognized_fields",
                "warning",
                "Unrecognized configuration fields are ignored by supported analysis settings",
                "Check spelling or remove unused settings; no unknown values are displayed.",
                fields=unknown,
            )
        try:
            spec, ignore = self.ignore(config)
            self.add(
                "configuration.ignore",
                "pass",
                "Ignore rules loaded read-only",
                source=ignore["source"],
                path=ignore["path"],
            )
        except (DevkitError, OSError, ImportError, ValueError) as exc:
            self.add(
                "configuration.ignore",
                "fail",
                str(exc),
                "Repair the configured .ignore encoding/patterns/permissions or reinstall pathspec. No file was created.",
            )
            spec = None

        configured = set()
        for app, entry in config.config["apps"].items():
            if self.context.app not in {"all", app}:
                continue
            roots, schemas = entry.get("roots", []), entry.get("schemas", [])
            if not roots and not schemas:
                explicit = self.context.app == app
                self.add(
                    f"app.{app}",
                    "fail" if explicit else "pass",
                    "Explicitly selected app has no configured roots"
                    if explicit
                    else "App is unconfigured; optional in --app all",
                    f"Set apps.{app}.roots in the manifest." if explicit else None,
                    configured=False,
                )
                continue
            configured.add(app)
            if not roots:
                self.add(
                    f"app.{app}.roots",
                    "fail",
                    "App has schemas but no configured source roots",
                    f"Set apps.{app}.roots.",
                )
            for raw in roots:
                self.path_check(
                    config,
                    spec,
                    f"app.{app}.root",
                    raw,
                    "directory",
                    "fail",
                    f"Correct apps.{app}.roots or select the intended --root. Unconfigured optional apps should use empty lists.",
                )
            for raw in schemas:
                self.path_check(
                    config,
                    spec,
                    f"app.{app}.schema",
                    raw,
                    "file",
                    "fail",
                    f"Correct apps.{app}.schemas and .ignore; point to the live Prisma schema, not a database connection.",
                )
                if roots and not any(
                    (
                        config.resolve(raw, False) == config.resolve(r, False)
                        or config.resolve(r, False) in config.resolve(raw, False).parents
                    )
                    for r in roots
                ):
                    self.add(
                        f"app.{app}.schema_scope",
                        "warning",
                        "Configured schema is outside this app's source roots; app-scoped discovery will omit it",
                        "Include the schema directory in the app roots if it belongs to this app.",
                        path=raw,
                    )
            if not schemas:
                self.add(
                    f"app.{app}.schemas",
                    "pass",
                    "No Prisma schema paths configured; this is allowed for non-Prisma apps",
                    configured=False,
                )
        if not configured and self.context.app == "all":
            self.add(
                "apps.none_configured",
                "warning",
                "No application roots are configured; generic search may work but app/schema coverage is not established",
                "Configure the applications you want to analyze.",
            )
        aliases: dict[tuple[str, str], set[str]] = {}
        for name, entry in config.config["modules"].items():
            app = entry.get("app", "erp")
            if app not in configured:
                continue
            for alias in [name, *entry.get("aliases", [])]:
                normalized = re.sub(r"[^a-z0-9]", "", alias.casefold())
                aliases.setdefault((app, normalized), set()).add(name)
            for raw in entry.get("required", []):
                self.path_check(
                    config,
                    spec,
                    "module.required",
                    raw,
                    "file",
                    "fail",
                    "Restore the required file or correct the module's required paths.",
                    module=name,
                )
            if name in config.overrides.get("modules", {}):
                for raw in entry.get("paths", []):
                    self.path_check(
                        config,
                        spec,
                        "module.path",
                        raw,
                        None,
                        "warning",
                        "Check this explicit module ownership path; missing optional layers need not be defects.",
                        module=name,
                    )
        for (app, alias), owners in aliases.items():
            if len(owners) > 1:
                self.add(
                    "module.alias_collision",
                    "warning",
                    "A normalized module alias is ambiguous",
                    "Use a unique canonical module name or adjust the aliases.",
                    app=app,
                    alias=alias,
                    modules=sorted(owners),
                )
        for prefix, raw in config.config.get("import_aliases", {}).items():
            self.path_check(
                config,
                spec,
                "import_alias.path",
                raw,
                None,
                "warning",
                "Correct this alias target if it should resolve in this checkout.",
                prefix=prefix,
            )

    def path_check(self, config, spec, code, raw, kind, failure, remediation, **details):
        path = config.resolve(raw, False)
        info = probe(path)
        valid = info["state"] in ({kind} if kind else {"file", "directory"}) and info.get(
            "readable_estimate", False
        )
        ignored = spec is not None and not self.allowed(config, spec, path)
        self.add(
            code,
            "pass" if valid and not ignored else failure,
            "Configured path is accessible"
            if valid and not ignored
            else "Configured path is missing, inaccessible, the wrong type, or excluded",
            remediation if not valid or ignored else None,
            configured_path=raw,
            ignored=ignored if spec is not None else None,
            **info,
            **details,
        )

    def runtime_checks(self):
        command = shutil.which("devkit")
        try:
            installed = metadata.version("electroerp-devkit")
        except metadata.PackageNotFoundError:
            installed = None
        runtime = {
            "devkit_version": __version__,
            "distribution_version": installed,
            "package_path": str(Path(__file__).resolve().parent),
            "python_executable": sys.executable,
            "python_version": platform.python_version(),
            "entrypoint": sys.argv[0],
            "path_command": command,
        }
        self.add(
            "runtime.python",
            "pass" if sys.version_info >= (3, 11) else "fail",
            "Python 3.11+ is required",
            "Install Devkit with a supported interpreter." if sys.version_info < (3, 11) else None,
            executable=sys.executable,
            version=runtime["python_version"],
        )
        self.add(
            "runtime.distribution",
            "pass" if installed == __version__ else "warning",
            "Installed distribution metadata matches running code"
            if installed == __version__
            else "Distribution metadata is missing or differs from running code",
            "Update/reinstall using the installer and interpreter that own this devkit command."
            if installed != __version__
            else None,
            code_version=__version__,
            distribution_version=installed,
        )
        scripts = Path(sysconfig.get_path("scripts")).resolve()
        same_environment = bool(command and Path(command).resolve().parent == scripts)
        self.add(
            "runtime.command",
            "pass" if same_environment else "warning",
            "PATH command is in this interpreter's scripts directory"
            if same_environment
            else "PATH command is missing or outside this interpreter's scripts directory",
            "Check your PATH/installer environment. This is a location hint, not proof of a version mismatch; python -m devkit may be intentional."
            if not same_environment
            else None,
            path_command=command,
            expected_scripts=str(scripts),
        )
        loaded = {}
        for distribution, module in DEPENDENCIES.items():
            try:
                loaded[module] = importlib.import_module(module)
                try:
                    version = metadata.version(distribution)
                except metadata.PackageNotFoundError:
                    version = None
                self.add("dependency." + distribution, "pass", "Dependency imported", version=version)
            except (ImportError, OSError, RuntimeError, ValueError) as exc:
                self.add(
                    "dependency." + distribution,
                    "fail",
                    f"Dependency could not load ({type(exc).__name__})",
                    "Reinstall this Devkit version in the active Python environment; preserve caches/feedback/backups.",
                )
        for grammar, module, factory, sample in (
            ("javascript", "tree_sitter_javascript", "language", b"export const ok = 1;"),
            ("typescript", "tree_sitter_typescript", "language_typescript", b"export const ok: number = 1;"),
            ("tsx", "tree_sitter_typescript", "language_tsx", b"export const View = () => <div>ok</div>;"),
        ):
            if "tree_sitter" not in loaded or module not in loaded:
                self.add(
                    "parser." + grammar, "warning", "Parser smoke test not run because a dependency failed"
                )
                continue
            try:
                tree_sitter = loaded["tree_sitter"]
                parser = tree_sitter.Parser(tree_sitter.Language(getattr(loaded[module], factory)()))
                tree = parser.parse(sample)
                if tree.root_node.has_error or tree.root_node.end_byte != len(sample):
                    raise ValueError("Smoke sample failed")
                self.add(
                    "parser." + grammar,
                    "pass",
                    "Bundled grammar parsed a synthetic smoke sample; no project source was parsed",
                )
            except (AttributeError, ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
                self.add(
                    "parser." + grammar,
                    "fail",
                    f"Parser API/grammar smoke test failed ({type(exc).__name__})",
                    "Reinstall compatible Devkit dependencies in the active environment.",
                )
        return runtime

    def storage_checks(self):
        directories = {
            "configuration": self.context.config_dir,
            "cache": self.context.config_dir / ".cache",
            "feedback": self.context.config_dir / ".feedback",
            "agent_sync": self.context.root / INTERNAL,
        }
        for name, path in directories.items():
            if name == "cache" and not self.context.use_cache:
                self.add(
                    "storage.cache",
                    "pass",
                    "Source cache is disabled by --no-cache; its write access was not checked",
                )
                continue
            info = probe(path)
            available = (
                info.get("creatable_estimate")
                if info["state"] == "missing"
                else info["state"] == "directory"
                and info.get("readable_estimate")
                and info.get("writable_estimate")
            )
            self.add(
                "storage." + name,
                "pass" if available else "warning" if name == "cache" else "fail",
                "Directory is absent but appears creatable; nothing initialized"
                if available and info["state"] == "missing"
                else "Directory access estimates passed"
                if available
                else "Directory access is unavailable or unsafe",
                "Check this location and permissions. No write test, deletion or repair was attempted; permission estimates do not verify Windows ACLs, locks or available disk space."
                if not available
                else None,
                **info,
            )
            filename = {
                "cache": "source.sqlite3",
                "feedback": "reports.sqlite3",
                "agent_sync": "state.json",
            }.get(name)
            if not filename or info["state"] in {"unsafe", "inaccessible", "file", "special"}:
                continue
            file_info = probe(path / filename)
            if file_info["state"] != "missing":
                readable = (
                    file_info["state"] == "file"
                    and file_info.get("readable_estimate")
                    and file_info.get("writable_estimate")
                    and not file_info.get("hardlinked")
                )
                self.add(
                    "storage." + name + ".file",
                    "pass" if readable else "warning" if name == "cache" else "fail",
                    "Storage file permission estimates passed; contents were not read"
                    if readable
                    else "Storage file is inaccessible, non-regular, hardlinked or read-only",
                    "Check permissions and keep durable feedback/sync backups. Doctor never opens databases or resets data."
                    if not readable
                    else None,
                    **file_info,
                )
            if name in {"cache", "feedback"}:
                for suffix in ("-journal", "-wal", "-shm"):
                    sidecar = probe(path / (filename + suffix))
                    if sidecar["state"] not in {"file", "missing"}:
                        self.add(
                            "storage." + name + ".sidecar",
                            "warning" if name == "cache" else "fail",
                            "SQLite sidecar path is unsafe or inaccessible",
                            "Inspect this path without deleting durable data. No database was opened.",
                            **sidecar,
                        )
            if name in {"feedback", "agent_sync"} and info["state"] == "directory":
                exclusion = path / ".gitignore"
                try:
                    text = None if probe(exclusion)["state"] == "missing" else read_text(exclusion)
                    expected = GITIGNORE if name == "feedback" else IGNORE
                    self.add(
                        "storage." + name + ".ignore",
                        "warning" if text is None else "pass" if text == expected else "fail",
                        "Storage Git exclusions are missing"
                        if text is None
                        else "Storage Git exclusions match"
                        if text == expected
                        else "Customized storage Git exclusions prevent future writes",
                        "Restore the exact documented storage-local .gitignore; do not overwrite your project-wide ignore file."
                        if text != expected
                        else None,
                        path=str(exclusion),
                    )
                except (DevkitError, OSError, ValueError) as exc:
                    self.add(
                        "storage." + name + ".ignore",
                        "fail",
                        str(exc),
                        "Check storage-local .gitignore permissions/type; no file was rewritten.",
                    )
        lock = probe(self.context.root / INTERNAL / "sync.lock")
        if lock["state"] != "missing":
            self.add(
                "storage.agent_sync.lock",
                "warning",
                "An agent-sync lock exists or cannot be inspected",
                "Wait for the writer; after a crash verify no sync is running before deliberately removing a stale lock.",
                **lock,
            )

    def agent_checks(self):
        try:
            changes, before, after = AgentSync(AgentsContext(self.context.root)).plan()
            for change in changes:
                state = {
                    "create": "missing",
                    "update": "outdated",
                    "migrate": "legacy",
                    "conflict": "locally_edited_or_unmanaged",
                    "unchanged": "current",
                }[change.action]
                self.add(
                    "agents.template",
                    "pass" if state == "current" else "warning",
                    "Agent instruction status: " + state,
                    "Review devkit agents sync --dry-run with this --root. Do not force-replace local edits without approval."
                    if state != "current"
                    else None,
                    path=change.path,
                    state=state,
                    reason=change.reason or None,
                )
            if before != after:
                self.add(
                    "agents.registration",
                    "warning",
                    "Agent template registration/version is missing or outdated",
                    "Review and explicitly run devkit agents sync; doctor does not synchronize files.",
                )
        except (DevkitError, OSError, ValueError) as exc:
            self.add(
                "agents.state",
                "fail",
                str(exc),
                "Inspect agent-sync state/templates/permissions. Preserve backups; doctor did not reset anything.",
            )

    def validate(self, doctor=False):
        runtime = self.runtime_checks() if doctor else None
        self.configuration_checks()
        if doctor:
            self.storage_checks()
            self.agent_checks()
        summary = {
            status: sum(c["status"] == status for c in self.checks) for status in ("pass", "fail", "warning")
        }
        data = {
            "selection": self.context.selection(),
            "checks": self.checks,
            "summary": summary,
            "read_only": True,
            "limitations": [
                "Permission checks are estimates, not write tests. Filesystem atime may change on reads; no project file is created, changed or deleted.",
                "No repository-wide scan, Prisma syntax/type validation, application tests, database connection, lock acquisition, migration, network request or automatic repair is performed.",
                "Configured paths are checked only for selected apps; global manifest structure/path safety is validated for all entries. Optional unconfigured apps are not failures under --app all.",
            ],
            **({"runtime": runtime} if runtime else {}),
        }
        return self.public(data), 1 if summary["fail"] else 0
