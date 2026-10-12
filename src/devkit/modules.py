"""ElectroERP ownership, conventions, and evidence-based module discovery."""

from __future__ import annotations

import re
from collections import defaultdict
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
        self._files_indexed = False

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
        if not self._files_indexed:
            self._index_files()
        return self._file_maps[name]

    def _index_files(self) -> None:
        """Scan paths and document identifiers once, then dispatch to matching modules."""
        aliases = defaultdict(set)
        terms = defaultdict(set)
        schemas = defaultdict(set)
        manifests = []
        document_patterns = []
        for name, info in self.entries.items():
            self._file_maps[name] = []
            names = [name, *info["aliases"]]
            for alias in names:
                aliases[normalize(alias)].add(name)
            document_patterns.append(
                (
                    name,
                    re.compile(
                        r"(?:^|[-_.])(?:" + "|".join(re.escape(a) for a in names) + r")(?:[-_.]|$)",
                        re.IGNORECASE,
                    ),
                )
            )
            for term in info["models"]:
                terms[term].add(name)
            for model in self.schema.models:
                if model["app"] == info["app"] and model["name"] in info["models"]:
                    terms[model["table"]].add(name)
                    schemas[model["path"]].add(name)
            manifests.extend((name, p.rstrip("/")) for p in info["paths"])
        # A combined regex preserves exact identifier boundaries, including mapped
        # SQL names that contain punctuation, without scanning once per model.
        pattern = (
            re.compile(
                r"(?<!\w)(?:"
                + "|".join(re.escape(t) for t in sorted(terms, key=lambda t: (-len(t), t)))
                + r")(?!\w)"
            )
            if terms
            else None
        )
        for path in self.project.files():
            app = self.project.app_for(path)
            relative = self.project.rel(path)
            matches = {}

            def add(
                module: str,
                reason: str,
                evidence: str,
                owned: bool,
                app=app,
                relative=relative,
                matches=matches,
            ) -> None:
                if app and app != self.entries[module]["app"]:
                    return
                row = matches.setdefault(
                    module,
                    {
                        "path": relative,
                        "layer": layer(relative),
                        "app": app or self.entries[module]["app"],
                        "reasons": [],
                        "evidence": evidence,
                        "ownership": "reference",
                    },
                )
                row["reasons"].append(reason)
                if owned:
                    row["ownership"] = "owned"
                    row["evidence"] = evidence

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
            basename = path.name.split(".")[0]
            owners = set().union(*(aliases.get(normalize(part), set()) for part in slots))
            has_slot_owner = bool(owners)
            if not has_slot_owner:
                owners.update(aliases.get(normalize(basename), set()))
            if path.suffix == ".md" and not has_slot_owner:
                owners.update(
                    module for module, expression in document_patterns if expression.search(basename)
                )
            for module in sorted(owners):
                add(module, "Module directory or document naming convention", "convention", True)
            for module, prefix in manifests:
                if relative == prefix or relative.startswith(prefix + "/"):
                    add(module, "Explicit module manifest path", "manifest", True)
            if path.suffix in {".sql", ".md"} and pattern:
                content = self.project.text(path) or ""
                mentions = defaultdict(list)
                for term in sorted(set(pattern.findall(content))):
                    for module in terms[term]:
                        mentions[module].append(term)
                for module, identifiers in sorted(mentions.items()):
                    add(
                        module,
                        "References schema identifiers: " + ", ".join(identifiers),
                        "text_reference",
                        False,
                    )
            for module in sorted(schemas.get(relative, set())):
                add(module, "Declares module-owned Prisma models", "schema_definition", True)
            for module, row in matches.items():
                self._file_maps[module].append(row)
        self._files_indexed = True

    def owners_of_file(self, path: str) -> list[str]:
        return [
            name
            for name in self.entries
            if any(f["path"] == path and f["ownership"] == "owned" for f in self.files(name))
        ]

    def _tool_namespace(self, path: str) -> tuple[str | None, str, str, str]:
        """Identify a tool's concrete feature/plugin, not its broad manifest owners.

        The backend prefix isolates separate source trees even within one app.
        Unknown layouts only pair same-directory siblings; a shared `ai` owner
        must never allow unrelated tools to satisfy each other's checks.
        """
        app = self.project.app_for(self.project.root / path)
        for prefix, kind in (
            ("backend/src/plugins/", "plugin"),
            ("backend/src/core/features/ai/tools/", "feature"),
            ("backend/ai/rag/tools/", "feature"),
            ("backend/src/core/features/", "feature"),
        ):
            match = re.search(r"(?:^|/)" + re.escape(prefix) + r"([^/]+)/", path)
            if not match:
                continue
            name = match.group(1)
            # RAG docs and centralized tool folders label plugins explicitly.
            if prefix in {"backend/ai/rag/tools/", "backend/src/core/features/ai/tools/"} and name.startswith(
                "plugin-"
            ):
                kind, name = "plugin", name[len("plugin-") :]
            modules = self._matching(name, app)
            if len(modules) > 1:
                break  # Ambiguous aliases cannot establish cross-directory identity.
            canonical = modules[0] if modules else name
            return app, path[: match.start()].rstrip("/"), kind, canonical
        return app, "", "directory", str(Path(path).parent)

    def _tool_companions(self) -> dict[str, list[str]]:
        """Index eligible files once; matching works in both directions across owners."""
        namespaces = {}
        index = defaultdict(list)
        for candidate in self.project.files():
            path = self.project.rel(candidate)
            if not path.endswith((".tool.ts", ".tool.md")):
                continue
            namespace = self._tool_namespace(path)
            namespaces[path] = namespace
            index[(namespace, candidate.name)].append(path)
        return {
            path: sorted(
                index.get(
                    (namespace, Path(path).with_suffix(".md" if path.endswith(".ts") else ".ts").name), []
                )
            )
            for path, namespace in namespaces.items()
        }

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
                    "owned_file_count": sum(f["ownership"] == "owned" for f in files),
                    "reference_file_count": sum(f["ownership"] == "reference" for f in files),
                    "available": bool(any(f["ownership"] == "owned" for f in files) or models),
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
        paths = {
            f["path"]
            for f in inspection["files"]
            if f["ownership"] == "owned" and f["layer"] not in {"prisma", "migrations"}
        }
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
        catalog = self.catalog() if value == "all" else []
        names = (
            [row["name"] for row in catalog if row["available"]] if value == "all" else [self.get_name(value)]
        )
        self.project.coverage.details["module_check_selection"] = {
            "requested": value,
            "checked_modules": names,
            "unavailable_modules_not_checked": [row["name"] for row in catalog if not row["available"]],
        }
        for row in catalog:
            if not row["available"]:
                self.project.coverage.record("modules_unavailable", row["name"])
        source = self.source or SourceIndex(self.project)
        checks = []
        if not names:
            raise DevkitError(
                "No available modules to check. Configure the project roots and module manifest."
            )
        registered_routes = source.routes()
        tool_companions = self._tool_companions()

        def add(module: str, rule: str, status: str, message: str, path: str | None = None) -> None:
            checks.append(
                {"module": module, "rule": rule, "status": status, "message": message, "path": path}
            )

        for name in names:
            info = self.entries[name]
            files = [f for f in self.files(name) if f["ownership"] == "owned"]
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
                        "warning" if parsed["errors"] else "pass",
                        "Parser could not fully analyze this file; not a compiler-verified syntax failure"
                        if parsed["errors"]
                        else "Tree-sitter parse",
                        path,
                    )
                    if parsed["errors"]:
                        checks[-1]["diagnostics"] = parsed["diagnostics"]
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
                                status = (
                                    "pass"
                                    if origin
                                    else ("fail" if target["exports"] and not target["errors"] else "warning")
                                )
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
                if path.endswith((".tool.ts", ".tool.md")):
                    documentation = path.endswith(".tool.ts")
                    kind = "documentation" if documentation else "implementation"
                    stem = Path(path).with_suffix(".md" if documentation else ".ts").name
                    companions = tool_companions.get(path, [])
                    status = "pass" if len(companions) == 1 else "warning" if companions else "fail"
                    message = (
                        f"Matching {kind}: " + companions[0]
                        if len(companions) == 1
                        else f"Ambiguous {kind} in the same app/tool namespace: " + ", ".join(companions)
                        if companions
                        else f"Missing {kind} {stem} in the same app/tool namespace (or excluded by .ignore)"
                    )
                    add(
                        name,
                        "ai_tool_" + kind,
                        status,
                        message,
                        path,
                    )
                    checks[-1]["companions"] = companions
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
            self.project.coverage.record("modules_checked", name)
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
