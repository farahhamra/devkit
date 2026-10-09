"""Cross-layer trace and impact queries over the live source index."""

from __future__ import annotations

import re
from typing import Any

from .modules import Modules, layer
from .project import DevkitError, Project
from .schema import Schema
from .source import SourceIndex


class Analysis:
    def __init__(self, project: Project) -> None:
        self.project = project
        self.schema = Schema(project)
        self.source = SourceIndex(project)
        self.modules = Modules(project, self.schema, self.source)

    def model_usage(self, name: str) -> list[dict[str, Any]]:
        model = self.schema.get(name, "model")
        delegate = model["name"][0].lower() + model["name"][1:]
        rows = []
        for call in self.source.calls:
            if re.search(r"(?:^|\.)" + re.escape(delegate) + r"\.", call["callee"]):
                rows.append(
                    {
                        **call,
                        "model": model["name"],
                        "evidence": "prisma_delegate_candidate",
                        "reason": f"Member call contains {delegate}; receiver type is not type-checked",
                    }
                )
        for ref in self.source.references:
            if ref["name"] == model["name"]:
                rows.append({**ref, "model": model["name"], "reason": "Model-name identifier reference"})
        return rows

    def model_impact(self, name: str) -> dict[str, Any]:
        model = self.schema.get(name, "model")
        refs = self.model_usage(name)
        owners = self.modules.owner(model["name"], model["app"])
        files = [f for owner in owners for f in self.modules.files(owner)]
        return {
            "model": self.modules.model_info(model),
            "references": refs,
            "relations": self.schema.relations(name),
            "module_files": files,
            "consumers": self.consumers({r["path"] for r in refs}),
            "limitations": ["Module files are context, not proof that each file uses this model."],
        }

    def consumers(self, paths: set[str]) -> list[dict[str, Any]]:
        rows = [row for path in paths for row in self.source.dependencies(path, reverse=True, depth=3)]
        return list({(r["path"], r["line"], r["resolved"]): r for r in rows}.values())

    def field_impact(self, target: str) -> dict[str, Any]:
        if "." not in target:
            raise DevkitError("Field target must be Model.field, for example Product.baseSellingPrice")
        model_name, field_name = target.split(".", 1)
        model = self.schema.get(model_name, "model")
        field = next((f for f in model["fields"] if f["name"] == field_name), None)
        if not field:
            raise DevkitError(f"Field {target} does not exist")
        module_files = {
            f["path"]
            for owner in self.modules.owner(model["name"], model["app"])
            for f in self.modules.files(owner)
        }
        usage_paths = {r["path"] for r in self.model_usage(model["name"])}
        refs = []
        for ref in self.source.references:
            if ref["name"] != field_name:
                continue
            confidence = "module_candidate" if ref["path"] in module_files else "name_only_candidate"
            if ref["path"] in usage_paths:
                confidence = "model_and_field_in_same_file"
            refs.append(
                {
                    **ref,
                    "evidence": confidence,
                    "layer": layer(ref["path"]),
                    "reason": "Field-name occurrence; the receiver's model identity requires review",
                }
            )
        return {
            "target": f"{model['name']}.{field_name}",
            "definition": {**field, "path": model["path"]},
            "references": refs,
            "consumers": self.consumers({r["path"] for r in refs if r["path"] in module_files | usage_paths}),
            "limitations": [
                "No TypeScript type checker or runtime data-flow analysis: matching field names are candidates, not confirmed model-field references."
            ],
        }

    def file_impact(self, value: str) -> dict[str, Any]:
        path = self.project.resolve(value)
        if path not in self.project.files():
            raise DevkitError(f"File is ignored or outside app scope: {value}")
        relative = self.project.rel(path)
        owners = self.modules.owners_of_file(relative)
        result: dict[str, Any] = {
            "path": relative,
            "modules": owners,
            "definitions": [s for s in self.source.symbols if s["path"] == relative],
            "consumers": self.consumers({relative}),
            "imports": self.source.files.get(relative, {}).get("imports", []),
        }
        if path.suffix == ".prisma":
            result["schema_models"] = [
                self.modules.model_info(m) for m in self.schema.models if m["path"] == relative
            ]
            result["references"] = [
                ref for m in result["schema_models"] for ref in self.model_usage(m["name"])
            ]
        result["limitations"] = [
            "Static import consumers are followed to depth 3; dynamic loading and non-import configuration links may be absent."
        ]
        return result

    def trace_route(self, query: str, method: str | None = None) -> dict[str, Any]:
        canonical = lambda value: value.rstrip("/") or "/"
        routes = [
            r
            for r in self.source.routes()
            if (canonical(r["route"]) == canonical(query) or canonical(r["local_route"]) == canonical(query))
            and (method is None or r["method"] in {method, "ALL"})
        ]
        for route in routes:
            deps = self.source.dependencies(route["path"], depth=3)
            paths = {route["path"], *(d["resolved"] for d in deps)}
            route["dependencies"] = deps
            route["definitions"] = [s for s in self.source.symbols if s["path"] in paths]
            route["prisma_candidates"] = [
                c
                for c in self.source.calls
                if c["path"] in paths and re.search(r"\b(?:prisma|tx|db)\.\w+\.\w+$", c["callee"])
            ]
        clients = [
            c
            for c in self.source.calls
            if any(canonical(v) == canonical(query) for v in c["literals"] if v is not None)
            and not any(c["path"] == r["path"] and c["line"] == r["line"] for r in routes)
        ]
        return {
            "query": query,
            "routes": routes,
            "client_candidates": clients,
            "limitations": [
                "Routes require statically named Express receivers, literal paths and resolvable imports.",
                "Dependency definitions and Prisma calls are file-level candidates, not a proven runtime call chain.",
                "Computed URLs, wrappers, type-level dispatch and dynamic route mounting may be unresolved.",
            ],
        }
