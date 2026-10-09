"""ElectroERP ownership, conventions, and evidence-based module discovery."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .project import DevkitError, Project
from .schema import Schema
from .source import SourceIndex


def normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def layer(path: str) -> str:
    p = path.casefold()
    if "/migrations/" in p:
        return "migrations"
    if "/tests/" in p or re.search(r"\.(test|spec)\.", p):
        return "tests"
    if p.endswith(".prisma"):
        return "prisma"
    if "/ai/" in p or p.endswith((".tool.ts", ".tool.md")):
        return "ai"
    if p.endswith(".md"):
        return "docs"
    if "frontend/" in p:
        return "frontend"
    if "backend/" in p:
        return "backend"
    return "other"


class Modules:
    def __init__(self, project: Project, schema: Schema, source: SourceIndex | None = None) -> None:
        self.project, self.schema, self.source = project, schema, source
        self.entries = {
            name: {"app": "erp", "models": [], "aliases": [], "paths": [], **entry}
            for name, entry in project.config["modules"].items()
        }
        # Discover project additions using the established directory convention.
        for path in project.files():
            relative = project.rel(path)
            match = re.search(
                r"(?:backend/src/core/features|frontend/src/app/core|backend/src/plugins|frontend/src/app/plugins)/([^/]+)/",
                relative,
            )
            if match:
                name = match.group(1)
                app = project.app_for(path) or "erp"
                if not self._matching(name, app):
                    key = name if name not in self.entries else f"{app}:{name}"
                    self.entries[key] = {
                        "app": app,
                        "models": [],
                        "aliases": [name],
                        "paths": [],
                        "discovered": True,
                    }
        self._file_maps: dict[str, list[dict[str, Any]]] = {}

    def _matching(self, value: str, app: str | None = None) -> list[str]:
        needle = normalize(value)
        return [
            name
            for name, info in self.entries.items()
            if (app is None or info["app"] == app)
            and needle in {normalize(n) for n in [name, *info["aliases"]]}
        ]

    def get_name(self, value: str) -> str:
        matches = self._matching(value, self.project.app if self.project.app != "all" else None)
        if not matches:
            raise DevkitError(f"Unknown module '{value}'. Use devkit module list.")
        if len(matches) > 1:
            raise DevkitError(
                f"Ambiguous module '{value}': {', '.join(matches)}. Use an exact name or --app."
            )
        return matches[0]

    def owner(self, model: str, app: str | None = None) -> list[str]:
        return [
            name
            for name, info in self.entries.items()
            if model in info["models"] and (app is None or info["app"] == app)
        ]

    def model_info(self, model: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": model["name"],
            "table": model.get("table"),
            "kind": model["kind"],
            "module": self.owner(model["name"], model["app"]),
            "app": model["app"],
            "path": model["path"],
            "line": model["line"],
        }

    def files(self, name: str) -> list[dict[str, Any]]:
        if name in self._file_maps:
            return self._file_maps[name]
        info = self.entries[name]
        aliases = [name, *info["aliases"]]
        keys = {normalize(n) for n in aliases}
        model_names = set(info["models"])
        model_rows = [m for m in self.schema.models if m["name"] in model_names and m["app"] == info["app"]]
        terms = model_names | {m["table"] for m in model_rows}
        result = []
        for path in self.project.files():
            app = self.project.app_for(path)
            if app and app != info["app"]:
                continue
            relative = self.project.rel(path)
            reasons = []
            evidence = "convention"
            if any(
                relative == p.rstrip("/") or relative.startswith(p.rstrip("/") + "/") for p in info["paths"]
            ):
                reasons.append("Explicit module manifest path")
                evidence = "manifest"
            # Match module slots, not arbitrary deep folder names (e.g. POS categories
            # must not become product-category ownership merely by sharing a word).
            slots = []
            for prefix in (
                "backend/src/core/features/ai/tools/",
                "backend/src/core/features/",
                "frontend/src/app/core/",
                "backend/src/plugins/",
                "frontend/src/app/plugins/",
                "backend/ai/rag/tools/",
                "backend/ai/rag/features/",
                "backend/ai/rag/plugins/",
                "backend/ai/rag/troubleshooting/",
                "backend/ai/rag/workflows/",
                "backend/ai/rag/ui/forms/",
                "backend/tests/unit/",
                "backend/tests/integration/",
                "backend/tests/e2e/",
            ):
                if prefix in relative:
                    slots.append(relative.split(prefix, 1)[1].split("/", 1)[0].split(".")[0])
            path_keys = {normalize(part) for part in slots}
            basename = path.name.split(".")[0]
            document_match = path.suffix == ".md" and any(
                re.search(r"(?:^|[-_.])" + re.escape(alias) + r"(?:[-_.]|$)", basename, re.IGNORECASE)
                for alias in aliases
            )
            if path_keys & keys or normalize(basename) in keys or document_match:
                reasons.append("Module directory or document naming convention")
            # Related prose/migrations can be evidenced by a model/table identifier.
            if path.suffix in {".sql", ".md"} and terms:
                content = self.project.text(path) or ""
                mentioned = sorted(
                    term for term in terms if re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", content)
                )
                if mentioned:
                    reasons.append("References schema identifiers: " + ", ".join(mentioned))
                    evidence = "text_reference"
            if path.suffix == ".prisma" and any(m["path"] == relative for m in model_rows):
                reasons.append("Declares module-owned Prisma models")
                evidence = "schema_definition"
            if reasons:
                result.append(
                    {
                        "path": relative,
                        "layer": layer(relative),
                        "app": app or info["app"],
                        "reasons": reasons,
                        "evidence": evidence,
                    }
                )
        self._file_maps[name] = result
        return result

    def owners_of_file(self, path: str) -> list[str]:
        return [name for name in self.entries if any(f["path"] == path for f in self.files(name))]

    def catalog(self) -> list[dict[str, Any]]:
        rows = []
        for name, info in sorted(self.entries.items()):
            if self.project.app != "all" and info["app"] != self.project.app:
                continue
            files = self.files(name)
            models = [
                m for m in self.schema.models if m["name"] in info["models"] and m["app"] == info["app"]
            ]
            rows.append(
                {
                    "name": name,
                    "app": info["app"],
                    "aliases": info["aliases"],
                    "layers": sorted({f["layer"] for f in files}),
                    "file_count": len(files),
                    "model_count": len(models),
                    "available": bool(files or models),
                }
            )
        return rows

    def inspect(self, name: str, scope: str = "all", related: str = "direct") -> dict[str, Any]:
        name = self.get_name(name)
        info = self.entries[name]
        models = [m for m in self.schema.models if m["name"] in info["models"] and m["app"] == info["app"]]
        owned = {m["name"] for m in models}
        edges = [e for e in self.schema.edges() if e["app"] == info["app"]]
        dependencies = [e for e in edges if e["source"] in owned and e["target"] not in owned]
        consumers = [e for e in edges if e["target"] in owned and e["source"] not in owned]
        enum_names = {f["type"] for m in models for f in m["fields"] if f["kind"] == "enum"}
        files = self.files(name)
        scopes = {"prisma": {"prisma", "migrations"}, "docs": {"docs"}}
        if scope != "all":
            files = [f for f in files if f["layer"] in scopes.get(scope, {scope})]
        return {
            "module": name,
            "app": info["app"],
            "aliases": info["aliases"],
            "models": [self.model_info(m) for m in models],
            "enums": sorted(enum_names),
            "missing_models": sorted(set(info["models"]) - owned),
            "dependencies": dependencies if related == "direct" else [],
            "consumers": consumers if related == "direct" else [],
            "files": files,
            "ownership": "Models use the manifest; files use explicit paths and labelled conventions/references.",
        }

    def impact(self, name: str) -> dict[str, Any]:
        inspection = self.inspect(name)
        paths = {f["path"] for f in inspection["files"] if f["layer"] not in {"prisma", "migrations"}}
        source = self.source or SourceIndex(self.project)
        imports = [
            i for p in paths for i in source.dependencies(p, reverse=True, depth=3) if i["path"] not in paths
        ]
        imports = list({(i["path"], i["line"], i["resolved"]): i for i in imports}.values())
        consumers = sorted(
            {owner for e in inspection["consumers"] for owner in self.owner(e["source"], e["app"])}
        )
        return {
            "module": inspection["module"],
            "models": inspection["models"],
            "affected_modules": consumers,
            "schema_relations": inspection["consumers"],
            "source_consumers": imports,
            "affected_apps": sorted(
                {inspection["app"]}
                | {app for row in imports if (app := self.project.app_for(self.project.root / row["path"]))}
            ),
            "coupled_files": [f for f in inspection["files"] if f["layer"] in {"ai", "docs", "tests"}],
            "validation_areas": [
                "Type-check affected backends and frontends",
                "Run affected module tests",
                "Verify organization scoping and permissions",
                "Review migrations if schema changed",
            ],
            "limitations": [
                "Static import dependencies are bounded to depth 3; dynamic consumers may be missing."
            ],
        }

    def check(self, value: str) -> dict[str, Any]:
        names = (
            [row["name"] for row in self.catalog() if row["available"]]
            if value == "all"
            else [self.get_name(value)]
        )
        source = self.source or SourceIndex(self.project)
        checks = []
        if not names:
            raise DevkitError(
                "No available modules to check. Configure the project roots and module manifest."
            )
        registered_routes = source.routes()

        def add(module: str, rule: str, status: str, message: str, path: str | None = None) -> None:
            checks.append(
                {"module": module, "rule": rule, "status": status, "message": message, "path": path}
            )

        for name in names:
            info = self.entries[name]
            files = self.files(name)
            paths = {f["path"] for f in files}
            inspection = self.inspect(name)
            if not files and len(info["models"]) == len(inspection["missing_models"]):
                add(name, "module_available", "fail", "No source or schema definitions found")
            for missing in inspection["missing_models"]:
                add(
                    name, "model_exists", "fail", f"Manifest model {missing} is absent from available schemas"
                )
            for m in inspection["models"]:
                owners = self.owner(m["name"], info["app"])
                add(
                    name,
                    "model_ownership",
                    "pass" if len(owners) == 1 else "fail",
                    f"{m['name']} owners: {', '.join(owners)}",
                    m["path"],
                )
            available = {self.project.rel(p) for p in self.project.files()}
            for required in info.get("required", []):
                add(
                    name,
                    "required_path",
                    "pass" if required in available else "fail",
                    "Required path exists"
                    if required in available
                    else "Required path is missing or ignored",
                    required,
                )
            for configured in info["paths"]:
                exists = any(p == configured or p.startswith(configured.rstrip("/") + "/") for p in available)
                add(
                    name,
                    "manifest_path",
                    "pass" if exists else "warning",
                    "Configured path exists" if exists else "Configured optional path is missing or ignored",
                    configured,
                )
            for path in sorted(paths):
                if path in source.files:
                    parsed = source.files[path]
                    add(
                        name,
                        "source_parse",
                        "fail" if parsed["errors"] else "pass",
                        "Tree-sitter parse",
                        path,
                    )
                    for imp in parsed["imports"]:
                        if (imp["specifier"] or "").startswith((".", "@/")):
                            add(
                                name,
                                "local_import",
                                "pass" if imp["resolved"] else "fail",
                                f"Import {imp['specifier']}"
                                + (
                                    f" resolves to {imp['resolved']}"
                                    if imp["resolved"]
                                    else " is unresolved or ignored"
                                ),
                                path,
                            )
                        if imp["resolved"] and imp["kind"] == "import":
                            target = source.files[imp["resolved"]]
                            for local, imported in imp["bindings"].items():
                                if imported == "*":
                                    continue
                                origin = source.export_origin(imp["resolved"], imported)
                                # CJS and ambient declarations can lack a static export map.
                                status = "pass" if origin else ("fail" if target["exports"] else "warning")
                                add(
                                    name,
                                    "imported_export",
                                    status,
                                    f"{imported} as {local} from {imp['resolved']}"
                                    + (" resolves" if origin else " has no resolved export"),
                                    path,
                                )
                    if "/hooks/" in path:
                        links = [
                            i for i in source.dependencies(path, depth=2) if i["resolved"].endswith(".api.ts")
                        ]
                        add(
                            name,
                            "frontend_api_link",
                            "pass" if links else "warning",
                            "Resolved hook-to-API imports"
                            if links
                            else "No conventional API import found within depth 2",
                            path,
                        )
                    for route in registered_routes:
                        if route["path"] != path:
                            continue
                        defined = {s["name"] for s in source.symbols if s["path"] == path}
                        imported_names = {local for i in parsed["imports"] for local in i["bindings"]}
                        for handler in route["handlers"]:
                            binding = re.match(r"^([A-Za-z_$][\w$]*)(?:[.(]|$)", handler)
                            if not binding:
                                continue
                            valid = binding.group(1) in defined | imported_names
                            add(
                                name,
                                "route_handler_binding",
                                "pass" if valid else "warning",
                                f"Handler {handler}: "
                                + (
                                    "binding exists"
                                    if valid
                                    else "binding unresolved; inspect runtime wiring"
                                ),
                                path,
                            )
                if path.endswith(".routes.ts"):
                    base = path[: -len(".routes.ts")]
                    for suffix in (".controller.ts", ".service.ts", ".types.ts", ".validators.ts"):
                        expected = base + suffix
                        # Conventions can vary; only explicit required paths are errors.
                        add(
                            name,
                            "backend_layer",
                            "pass" if expected in paths else "warning",
                            f"Expected companion: {expected}",
                            path,
                        )
                if path.endswith(".tool.ts"):
                    stem = Path(path).name[:-3] + ".md"
                    docs = [p for p in paths if Path(p).name == stem]
                    add(
                        name,
                        "ai_tool_documentation",
                        "pass" if docs else "fail",
                        "Matching documentation: " + ", ".join(docs)
                        if docs
                        else f"Missing documentation {stem}",
                        path,
                    )
                if path.endswith(".tool.md"):
                    stem = Path(path).name[:-3] + ".ts"
                    impl = [p for p in paths if Path(p).name == stem]
                    add(
                        name,
                        "ai_tool_implementation",
                        "pass" if impl else "fail",
                        "Matching implementation: " + ", ".join(impl)
                        if impl
                        else f"Missing implementation {stem}",
                        path,
                    )
            delegates = {
                m["name"][0].lower() + m["name"][1:]: m["name"]
                for m in self.schema.models
                if m["app"] == info["app"]
            }
            for call in source.calls:
                if call["path"] not in paths:
                    continue
                match = re.search(
                    r"\b(prisma|tx|db)\.(\w+)\.(findMany|findFirst|findUnique|create|update|delete|count|aggregate|groupBy|upsert|createMany|updateMany|deleteMany)(?:OrThrow)?$",
                    call["callee"],
                )
                if match:
                    receiver, delegate, _ = match.groups()
                    exists = delegate in delegates
                    add(
                        name,
                        "prisma_reference",
                        "pass" if exists else ("fail" if receiver == "prisma" else "warning"),
                        f"Prisma-convention delegate {delegate}: "
                        + (delegates[delegate] if exists else "no matching model in available schemas"),
                        call["path"],
                    )
        return {
            "modules": names,
            "checks": checks,
            "summary": {
                status: sum(c["status"] == status for c in checks) for status in ("pass", "fail", "warning")
            },
            "limitations": [
                "Checks are static. They do not prove runtime route behavior, permissions, or API contract compatibility."
            ],
        }
