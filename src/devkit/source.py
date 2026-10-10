"""Tree-sitter source index. Static evidence is explicit; unresolved code stays unresolved."""

from __future__ import annotations

import hashlib
import os
import re
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import tree_sitter_javascript as javascript
import tree_sitter_typescript as typescript
from tree_sitter import Language, Node, Parser

from .cache import ParseCache
from .project import SOURCE_EXTENSIONS, Project


def walk(node: Node) -> Iterator[Node]:
    pending = [node]
    while pending:
        current = pending.pop()
        yield current
        pending.extend(reversed(current.named_children))


def text(node: Node | None) -> str:
    return node.text.decode("utf-8") if node is not None else ""


def literal(node: Node | None) -> str | None:
    if (
        node is not None
        and node.type in {"string", "template_string"}
        and not any(c.type == "template_substitution" for c in node.named_children)
    ):
        return text(node)[1:-1]
    return None


def match_name(query: str, value: str) -> bool:
    return query in value if any(c.isupper() for c in query) else query.casefold() in value.casefold()


class SourceIndex:
    def __init__(self, project: Project, paths: list[Path] | None = None) -> None:
        self.project = project
        self.files: dict[str, dict[str, Any]] = {}
        self.symbols: list[dict[str, Any]] = []
        self.references: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []
        self.forward: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.reverse: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.stats = {"parsed_files": 0, "cached_files": 0}
        cache = ParseCache(project)
        parsers = {
            "js": Parser(Language(javascript.language())),
            "ts": Parser(Language(typescript.language_typescript())),
            "tsx": Parser(Language(typescript.language_tsx())),
        }
        selected = paths if paths is not None else project.files()
        for path in selected:
            if path.suffix not in SOURCE_EXTENSIONS:
                continue
            relative = project.rel(path)
            try:
                stamp = cache.stamp(path)
            except OSError as exc:
                project.warnings.append(f"Cannot stat {relative}: {exc}")
                continue
            content = project.text(path)
            if content is None:
                continue
            content_bytes = content.encode("utf-8")
            # Hash live content even on a metadata hit: Windows ctime can be birth
            # time and tools may preserve mtime/size while changing a file.
            identity = stamp + ":" + hashlib.sha256(content_bytes).hexdigest()
            cached = cache.get(relative, identity)
            if cached is not None:
                self.files[relative] = cached["info"]
                self.symbols.extend(cached["symbols"])
                self.references.extend(cached["references"])
                self.calls.extend(cached["calls"])
                project.warnings.extend(cached["warnings"])
                self.stats["cached_files"] += 1
                continue
            lang = (
                "tsx" if path.suffix == ".tsx" else "ts" if path.suffix in {".ts", ".mts", ".cts"} else "js"
            )
            tree = parsers[lang].parse(content_bytes)
            self.stats["parsed_files"] += 1
            offsets = (len(self.symbols), len(self.references), len(self.calls), len(project.warnings))
            info = {
                "path": relative,
                "imports": [],
                "exports": {},
                "routers": [],
                "initializers": {},
                "errors": tree.root_node.has_error,
                "diagnostics": [],
            }
            self.files[relative] = info
            if tree.root_node.has_error:
                for node in walk(tree.root_node):
                    if not (node.is_error or node.is_missing):
                        continue
                    snippet = content_bytes[
                        node.start_byte : node.start_byte + min(120, node.end_byte - node.start_byte)
                    ].decode("utf-8", errors="replace")
                    hint = (
                        "Possible JSX ampersand grammar limitation"
                        if lang == "tsx" and snippet.startswith("&")
                        else "Not verified by the TypeScript compiler"
                    )
                    info["diagnostics"].append(
                        {
                            "line": node.start_point.row + 1,
                            "column": node.start_point.column + 1,
                            "end_line": node.end_point.row + 1,
                            "end_column": node.end_point.column + 1,
                            "kind": "missing_node" if node.is_missing else "parser_error",
                            "snippet": snippet,
                            "hint": hint,
                        }
                    )
                first = (
                    info["diagnostics"][0]
                    if info["diagnostics"]
                    else {"line": 1, "column": 1, "hint": "Unverified parser error"}
                )
                project.warnings.append(
                    f"Parse errors in {relative}:{first['line']}:{first['column']}; source analysis is partial. {first['hint']}."
                )
            self._content = content_bytes
            self._parse(tree.root_node, info)
            try:
                unchanged = cache.stamp(path) == stamp
            except OSError:
                unchanged = False
            if unchanged:
                cache.put(
                    relative,
                    identity,
                    {
                        "info": info,
                        "symbols": self.symbols[offsets[0] :],
                        "references": self.references[offsets[1] :],
                        "calls": self.calls[offsets[2] :],
                        "warnings": project.warnings[offsets[3] :],
                    },
                )
        cache.finish(set(self.files) if paths is None else None)
        self._resolve_imports(resolve=paths is None)

    def _preview(self, node: Node | None, limit: int = 240) -> str:
        if node is None:
            return ""
        value = self._content[node.start_byte : min(node.end_byte, node.start_byte + limit)].decode(
            "utf-8", errors="replace"
        )
        return value + ("…" if node.end_byte - node.start_byte > limit else "")

    def _parse(self, root: Node, info: dict[str, Any]) -> None:
        path = info["path"]
        kinds = {
            "function_declaration": "function",
            "generator_function_declaration": "function",
            "class_declaration": "class",
            "interface_declaration": "interface",
            "type_alias_declaration": "type",
            "enum_declaration": "enum",
            "method_definition": "method",
            "abstract_class_declaration": "class",
            "function_signature": "function",
        }
        for node in walk(root):
            line, column = node.start_point
            location = {"path": path, "line": line + 1, "column": column + 1}
            name_node = node.child_by_field_name("name")
            kind = kinds.get(node.type)
            if node.type == "variable_declarator":
                value = node.child_by_field_name("value")
                if name_node and value and value.type == "call_expression":
                    info["initializers"][text(name_node)] = self._preview(
                        value.child_by_field_name("function")
                    )
                kind = (
                    "function"
                    if value and value.type in {"arrow_function", "function_expression", "generator_function"}
                    else "variable"
                )
            if kind and name_node:
                current = node
                exported = False
                while current.parent and current.parent.type in {
                    "lexical_declaration",
                    "variable_declaration",
                    "export_statement",
                }:
                    current = current.parent
                    exported |= current.type == "export_statement"
                symbol = {
                    **location,
                    "name": text(name_node),
                    "kind": kind,
                    "end_line": node.end_point.row + 1,
                    "exported": exported,
                    "signature": self._preview(node).splitlines()[0],
                    "evidence": "definition",
                }
                self.symbols.append(symbol)
                if exported:
                    info["exports"][
                        "default" if "default" in [c.type for c in current.children] else symbol["name"]
                    ] = symbol["name"]
            if node.type == "import_statement":
                spec = literal(node.child_by_field_name("source"))
                bindings = {}
                clause = next((c for c in node.named_children if c.type == "import_clause"), None)
                if clause:
                    for child in clause.named_children:
                        if child.type == "identifier":
                            bindings[text(child)] = "default"
                        elif child.type == "namespace_import":
                            bindings[text(child.named_children[-1])] = "*"
                        elif child.type == "named_imports":
                            for item in child.named_children:
                                original = item.child_by_field_name("name")
                                alias = item.child_by_field_name("alias")
                                if original:
                                    bindings[text(alias or original)] = text(original)
                info["imports"].append(
                    {**location, "specifier": spec, "bindings": bindings, "kind": "import"}
                )
            if node.type == "export_statement":
                spec = literal(node.child_by_field_name("source"))
                clause = next((c for c in node.named_children if c.type == "export_clause"), None)
                bindings = {}
                if clause:
                    for item in clause.named_children:
                        original = item.child_by_field_name("name")
                        alias = item.child_by_field_name("alias")
                        if original:
                            bindings[text(alias or original)] = text(original)
                            info["exports"][text(alias or original)] = text(original)
                elif not spec:
                    value = node.child_by_field_name("value")
                    if value and value.type == "identifier":
                        info["exports"]["default"] = text(value)
                if spec:
                    info["imports"].append(
                        {**location, "specifier": spec, "bindings": bindings, "kind": "reexport"}
                    )
            if node.type == "call_expression":
                function = node.child_by_field_name("function")
                arguments = node.child_by_field_name("arguments")
                args = arguments.named_children if arguments else []
                call = {
                    **location,
                    "callee": self._preview(function),
                    "arguments": [self._preview(a) for a in args],
                    "arguments_truncated": any(a.end_byte - a.start_byte > 240 for a in args),
                    "literals": [literal(a) for a in args],
                    "end_line": node.end_point.row + 1,
                    "evidence": "parsed_call",
                }
                self.calls.append(call)
                if call["callee"] in {"require", "import"}:
                    spec = literal(args[0]) if args else None
                    if spec:
                        bindings = {}
                        if node.parent and node.parent.type == "variable_declarator":
                            binding = node.parent.child_by_field_name("name")
                            if binding and binding.type == "identifier":
                                bindings[text(binding)] = "*"
                            elif binding:
                                for child in binding.named_children:
                                    if child.type == "shorthand_property_identifier_pattern":
                                        bindings[text(child)] = text(child)
                                    elif child.type == "pair_pattern":
                                        bindings[text(child.child_by_field_name("value"))] = text(
                                            child.child_by_field_name("key")
                                        )
                        info["imports"].append(
                            {**location, "specifier": spec, "bindings": bindings, "kind": "require"}
                        )
                    else:
                        self.project.warnings.append(
                            f"Dynamic import cannot be resolved at {path}:{line + 1}"
                        )
            if node.type in {
                "identifier",
                "property_identifier",
                "type_identifier",
                "shorthand_property_identifier",
            }:
                self.references.append({**location, "name": text(node), "evidence": "parsed_identifier"})
            if node.type == "member_expression":
                self.references.append(
                    {
                        **location,
                        "name": text(node.child_by_field_name("property")),
                        "expression": self._preview(node),
                        "object": self._preview(node.child_by_field_name("object")),
                        "evidence": "parsed_member",
                    }
                )
        factories = set()
        for imported in info["imports"]:
            if imported["specifier"] == "express":
                for local, original in imported["bindings"].items():
                    if original in {"default", "*", "Router"}:
                        factories.add(local)
                        if original != "Router":
                            factories.add(local + ".Router")
        info["routers"] = [name for name, factory in info["initializers"].items() if factory in factories]

    def resolve_import(self, source: str, specifier: str) -> str | None:
        root = self.project.root
        if specifier.startswith("."):
            target = (root / source).parent / specifier
        elif specifier.startswith("@/"):
            # ElectroERP frontend convention. Other aliases must be declared explicitly.
            app = self.project.app_for(root / source)
            app_roots = self.project.app_roots(app) if app else []
            owner = next(
                (
                    p
                    for p in sorted(app_roots, key=lambda p: len(p.parts), reverse=True)
                    if (root / source).is_relative_to(p)
                ),
                root,
            )
            target = owner / "src" / specifier[2:]
        else:
            aliases = self.project.config.get("import_aliases", {})
            alias = next((a for a in sorted(aliases, key=len, reverse=True) if specifier.startswith(a)), None)
            if not alias:
                return None
            target = root / aliases[alias] / specifier[len(alias) :]
        # Candidate membership in the already-vetted file catalog excludes symlinks.
        target = Path(os.path.abspath(target))
        if not target.is_relative_to(root):
            return None
        candidates = [target]
        if target.suffix in {".js", ".mjs", ".cjs"}:
            candidates.extend(target.with_suffix(ext) for ext in (".ts", ".tsx", ".mts", ".cts"))
        candidates.extend(Path(str(target) + ext) for ext in sorted(SOURCE_EXTENSIONS))
        candidates.extend(target / ("index" + ext) for ext in sorted(SOURCE_EXTENSIONS))
        return next(
            (
                p.relative_to(root).as_posix()
                for p in candidates
                if p.relative_to(root).as_posix() in self.files
            ),
            None,
        )

    def _resolve_imports(self, resolve: bool = True) -> None:
        symbols = defaultdict(list)
        for symbol in self.symbols:
            symbols[(symbol["path"], symbol["name"])].append(symbol)
        for info in self.files.values():
            for item in info["imports"]:
                if not resolve:
                    continue
                item["resolved"] = self.resolve_import(info["path"], item["specifier"] or "")
                item["evidence"] = "resolved_import" if item["resolved"] else "unresolved_import"
                if item["resolved"]:
                    self.forward[item["path"]].append(item)
                    self.reverse[item["resolved"]].append(item)
                if not item["resolved"] and (item["specifier"] or "").startswith((".", "@/")):
                    self.project.warnings.append(
                        f"Unresolved local import {item['specifier']} at {info['path']}:{item['line']}"
                    )
            for exported in info["exports"].values():
                for symbol in symbols[(info["path"], exported)]:
                    symbol["exported"] = True

    def export_origin(
        self, path: str, exported: str, seen: set[tuple[str, str]] | None = None
    ) -> tuple[str, str] | None:
        """Resolve named/default exports across local barrels; terminate export cycles."""
        seen = set() if seen is None else seen
        if (path, exported) in seen or path not in self.files:
            return None
        seen = seen | {(path, exported)}
        info = self.files[path]
        local = info["exports"].get(exported)
        for imp in info["imports"]:
            if not imp["resolved"]:
                continue
            if imp["kind"] == "reexport":
                imported = imp["bindings"].get(exported)
                if imported:
                    return self.export_origin(imp["resolved"], imported, seen)
                if not imp["bindings"] and exported != "default":
                    result = self.export_origin(imp["resolved"], exported, seen)
                    if result:
                        return result
            elif local and local in imp["bindings"]:
                return self.export_origin(imp["resolved"], imp["bindings"][local], seen)
        if local:
            return path, local
        return None

    def dependencies(self, path: str, reverse: bool = False, depth: int = 1) -> list[dict[str, Any]]:
        adjacency = self.reverse if reverse else self.forward
        frontier = {path}
        visited = {path}
        seen = set()
        result = []
        for distance in range(1, depth + 1):
            following = set()
            for item in (item for node in sorted(frontier) for item in adjacency.get(node, [])):
                source, target = (
                    (item["resolved"], item["path"]) if reverse else (item["path"], item["resolved"])
                )
                if source not in frontier:
                    continue
                key = (item["path"], item["line"], item["resolved"])
                if key not in seen:
                    result.append({**item, "depth": distance})
                    seen.add(key)
                following.add(target)
            frontier = following - visited
            visited |= following
        return result

    def symbol_trace(self, name: str) -> dict[str, Any]:
        definitions = [s for s in self.symbols if s["name"] == name]
        imports = []
        aliases = {(s["path"], s["name"]) for s in definitions}
        exports = []
        for info in self.files.values():
            for exported in info["exports"]:
                origin = self.export_origin(info["path"], exported)
                if origin and origin[1] == name:
                    exports.append(
                        {
                            "path": info["path"],
                            "exported_name": exported,
                            "definition_path": origin[0],
                            "definition_name": origin[1],
                            "evidence": "resolved_export",
                        }
                    )
            for item in info["imports"]:
                for local, imported in item["bindings"].items():
                    origin = self.export_origin(item["resolved"], imported) if item["resolved"] else None
                    if imported == name or (origin and origin[1] == name):
                        imports.append({**item, "local_name": local, "imported_name": imported})
                        aliases.add((item["path"], local))
        refs = [r for r in self.references if r["name"] == name or (r["path"], r["name"]) in aliases]
        return {
            "definitions": definitions,
            "imports": imports,
            "exports": exports,
            "references": refs,
            "limitations": [
                "Identifier references are candidates, not type-checked symbol identity; shadowing and dynamic dispatch may be unresolved."
            ],
        }

    def routes(self) -> list[dict[str, Any]]:
        methods = {"get", "post", "put", "patch", "delete", "options", "head", "all"}
        mounts = []
        routes = []
        for call in self.calls:
            match = re.match(r"^([\w$]+)\.(\w+)$", call["callee"])
            if not match:
                continue
            receiver, method = match.groups()
            if receiver not in self.files[call["path"]]["routers"]:
                continue
            first = call["literals"][0] if call["literals"] else None
            if method in methods and first is not None:
                routes.append(
                    {
                        **call,
                        "method": method.upper(),
                        "local_route": first,
                        "route": first,
                        "router": receiver,
                        "handlers": call["arguments"][1:],
                    }
                )
            elif method == "use":
                prefix = first if first is not None else ""
                for arg in call["arguments"][1 if first is not None else 0 :]:
                    root_binding = arg.split(".")[0]
                    imp = next(
                        (
                            i
                            for i in self.files[call["path"]]["imports"]
                            if root_binding in i["bindings"] and i["resolved"]
                        ),
                        None,
                    )
                    if imp:
                        mounts.append(
                            {
                                "parent": call["path"],
                                "child": imp["resolved"],
                                "prefix": prefix,
                                "line": call["line"],
                            }
                        )

        def prefixes(path: str, seen: set[str]) -> list[tuple[str, list[dict[str, Any]]]]:
            if path in seen:
                return [("", [])]
            parents = [m for m in mounts if m["child"] == path]
            if not parents:
                return [("", [])]
            return [
                (prefix.rstrip("/") + "/" + m["prefix"].strip("/"), chain + [m])
                for m in parents
                for prefix, chain in prefixes(m["parent"], seen | {path})
            ]

        expanded = []
        for route in routes:
            for prefix, chain in prefixes(route["path"], set()):
                full = re.sub(r"/+", "/", prefix.rstrip("/") + "/" + route["local_route"].lstrip("/"))
                expanded.append(
                    {
                        **route,
                        "route": full,
                        "mounts": chain,
                        "evidence": "static_route",
                        "limitations": "Static Express calls and imported router mounts; dynamic mounts may be incomplete.",
                    }
                )
        return expanded
