"""Read Prisma declarations without executing generators or connecting to a database."""

from __future__ import annotations

import re
from typing import Any

from .project import DevkitError, Project

TOKEN = re.compile(
    r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|@@?[A-Za-z_][\w.]*|[A-Za-z_][\w]*|-?\d+(?:\.\d+)?|[^\s]'
)


def tokens(source: str) -> list[tuple[str, int]]:
    return [(m.group(), m.start()) for m in TOKEN.finditer(source) if not m.group().startswith(("//", "/*"))]


def attribute(text: str, name: str) -> str | None:
    ts = tokens(text)
    found = next(
        (i for i, (token, _) in enumerate(ts) if token == name and i + 1 < len(ts) and ts[i + 1][0] == "("),
        None,
    )
    if found is None:
        return None
    start = ts[found + 1][1] + 1
    level = 1
    for token, offset in tokens(text[start:]):
        if token == "(":
            level += 1
        elif token == ")":
            level -= 1
            if level == 0:
                return text[start : start + offset].strip()
    return None


def quoted(text: str | None) -> str | None:
    if text is None:
        return None
    match = re.match(r'\s*"((?:\\.|[^"\\])*)"', text)
    return match.group(1) if match else None


def names(text: str | None, key: str | None = None) -> list[str]:
    if not text:
        return []
    if key:
        match = re.search(r"\b" + key + r"\s*:\s*\[([^]]*)\]", text)
    else:
        match = re.search(r"\[([^]]*)\]", text)
    return re.findall(r"\b[A-Za-z_]\w*\b", match.group(1)) if match else []


def statements(body: str) -> list[tuple[str, int]]:
    """Split fields/attributes at top level; multiline attributes stay together."""
    ts = tokens(body)
    result = []
    start = 0
    i = 0
    while i < len(ts):
        start = i
        if ts[i][0].startswith("@@"):
            i += 1
        else:
            i += 1  # field or enum member
            if i < len(ts) and re.match(r"^[A-Za-z_]", ts[i][0]):
                # A type follows on the same statement; enum handled separately.
                i += 1
                if i < len(ts) and ts[i][0] == "?":
                    i += 1
                if i + 1 < len(ts) and ts[i][0] == "[" and ts[i + 1][0] == "]":
                    i += 2
        while i < len(ts):
            token = ts[i][0]
            if token == "(":
                depth = 1
                i += 1
                while i < len(ts) and depth:
                    depth += (ts[i][0] == "(") - (ts[i][0] == ")")
                    i += 1
            elif token.startswith("@") and not token.startswith("@@"):
                i += 1
            else:
                break
        end = ts[i][1] if i < len(ts) else len(body)
        result.append((body[ts[start][1] : end].strip(), ts[start][1]))
    return result


def parse_schema(source: str, path: str = "schema.prisma", app: str | None = None) -> list[dict[str, Any]]:
    ts = tokens(source)
    declarations = []
    i = 0
    while i < len(ts):
        if ts[i][0] not in {"model", "enum"}:
            i += 1
            continue
        kind, offset = ts[i]
        if i + 2 >= len(ts) or ts[i + 2][0] != "{":
            raise DevkitError(
                f"Malformed {kind} declaration at {path}:{source.count(chr(10), 0, offset) + 1}"
            )
        name = ts[i + 1][0]
        body_start = ts[i + 2][1] + 1
        j = i + 3
        while j < len(ts) and ts[j][0] != "}":
            j += 1
        if j == len(ts):
            raise DevkitError(f"Unclosed {kind} {name} in {path}")
        body = source[body_start : ts[j][1]]
        decl: dict[str, Any] = {
            "name": name,
            "kind": kind,
            "path": path,
            "app": app,
            "line": source.count("\n", 0, offset) + 1,
            "end_line": source.count("\n", 0, ts[j][1]) + 1,
        }
        if kind == "enum":
            enum_tokens = tokens(body)
            values = []
            k = 0
            while k < len(enum_tokens):
                value, position = enum_tokens[k]
                if re.fullmatch(r"[A-Za-z_]\w*", value):
                    values.append({"name": value, "line": source.count("\n", 0, body_start + position) + 1})
                k += 1
                while k < len(enum_tokens) and enum_tokens[k][0].startswith("@"):
                    k += 1
                    if k < len(enum_tokens) and enum_tokens[k][0] == "(":
                        while k < len(enum_tokens) and enum_tokens[k][0] != ")":
                            k += 1
                        k += 1
            decl["values"] = values
        else:
            decl.update(
                {"table": name, "fields": [], "indexes": [], "unique_constraints": [], "attributes": []}
            )
            for statement, position in statements(body):
                statement_tokens = tokens(statement)
                clean = " ".join(t for t, _ in statement_tokens)
                last_token, last_offset = statement_tokens[-1]
                location = {
                    "line": source.count("\n", 0, body_start + position) + 1,
                    "end_line": source.count(
                        "\n", 0, body_start + position + last_offset + len(last_token) - 1
                    )
                    + 1,
                }
                if clean.startswith("@@"):
                    decl["attributes"].append(clean)
                    if clean.startswith("@@map"):
                        decl["table"] = quoted(attribute(clean, "@@map")) or name
                    elif clean.startswith(("@@index", "@@fulltext")):
                        decl["indexes"].append({"fields": names(clean), "definition": clean, **location})
                    elif clean.startswith(("@@unique", "@@id")):
                        decl["unique_constraints"].append(
                            {"fields": names(clean), "definition": clean, **location}
                        )
                    continue
                field_tokens = tokens(statement)
                if len(field_tokens) < 2:
                    raise DevkitError(f"Malformed field in {path}: {name}: {statement}")
                field_name, field_type = field_tokens[0][0], field_tokens[1][0]
                relation = attribute(clean, "@relation")
                delete = re.search(r"onDelete\s*:\s*(\w+)", relation or "")
                update = re.search(r"onUpdate\s*:\s*(\w+)", relation or "")
                field = {
                    "name": field_name,
                    "type": field_type,
                    "column": quoted(attribute(clean, "@map")) or field_name,
                    "optional": len(field_tokens) > 2 and field_tokens[2][0] == "?",
                    "list": len(field_tokens) > 2 and field_tokens[2][0] == "[",
                    "id": any(t == "@id" for t, _ in field_tokens),
                    "unique": any(t == "@unique" for t, _ in field_tokens),
                    "default": attribute(clean, "@default"),
                    "attributes": clean,
                    **location,
                    "relation_name": quoted(relation),
                    "foreign_keys": names(relation, "fields"),
                    "references": names(relation, "references"),
                    "on_delete": delete.group(1) if delete else None,
                    "on_update": update.group(1) if update else None,
                }
                decl["fields"].append(field)
        declarations.append(decl)
        i = j + 1
    return declarations


class Schema:
    def __init__(self, project: Project) -> None:
        self.project = project
        selected = project.schema_paths()
        self.paths = []
        self.declarations = []
        for path in selected:
            relative = project.rel(path)
            content = project.text(path)
            if content is None:
                project.coverage.gap("schema_unreadable", relative, stage="schema")
                continue
            try:
                declarations = parse_schema(content, relative, project.app_for(path))
            except DevkitError:
                project.coverage.gap("schema_parse_error", relative, stage="schema")
                raise
            self.paths.append(path)
            self.declarations.extend(declarations)
            project.coverage.record("schema_analyzed", relative)
        project.coverage.finish("schema")
        project.coverage.details["schema_declarations"] = len(self.declarations)
        self.models = [d for d in self.declarations if d["kind"] == "model"]
        for model in self.models:
            types = {d["name"]: d["kind"] for d in self.declarations if d["path"] == model["path"]}
            for field in model["fields"]:
                field["kind"] = types.get(field["type"], "scalar")
                if field["kind"] == "model":
                    # Prisma relation properties are virtual; the FK scalar owns the DB column.
                    field["column"] = None

    def get(self, name: str, kind: str | None = None) -> dict[str, Any]:
        found = [
            d
            for d in self.declarations
            if d["name"].casefold() == name.casefold() and (kind is None or d["kind"] == kind)
        ]
        if not found:
            raise DevkitError(f"Unknown {kind or 'schema declaration'} '{name}'. Use devkit schema list.")
        if len(found) > 1:
            raise DevkitError(
                f"Ambiguous schema name '{name}'; select an --app. Sources: "
                + ", ".join(d["path"] for d in found)
            )
        return found[0]

    def edges(self) -> list[dict[str, Any]]:
        result = []
        for model in self.models:
            for field in model["fields"]:
                if field["kind"] != "model":
                    continue
                # A relation is emitted once from its foreign-key-owning side.
                if not field["foreign_keys"]:
                    continue
                target = next(
                    (m for m in self.models if m["path"] == model["path"] and m["name"] == field["type"]),
                    None,
                )
                opposite = (
                    []
                    if not target
                    else [
                        f
                        for f in target["fields"]
                        if f["type"] == model["name"]
                        and f["relation_name"] == field["relation_name"]
                        and not f["foreign_keys"]
                    ]
                )
                cardinality = "many-to-one" if any(f["list"] for f in opposite) else "one-to-one"
                result.append(
                    {
                        "source": model["name"],
                        "target": field["type"],
                        "source_table": model["table"],
                        "target_table": target["table"] if target else None,
                        "field": field["name"],
                        "inverse_fields": [f["name"] for f in opposite],
                        "cardinality": cardinality,
                        "optional": field["optional"],
                        "foreign_keys": field["foreign_keys"],
                        "references": field["references"],
                        "on_delete": field["on_delete"],
                        "on_update": field["on_update"],
                        "relation_name": field["relation_name"],
                        "path": model["path"],
                        "line": field["line"],
                        "app": model["app"],
                    }
                )
        return result

    def relations(
        self, name: str, depth: int = 1, direction: str = "both", include_shared: bool = False
    ) -> list[dict[str, Any]]:
        model = self.get(name, "model")
        edges = [e for e in self.edges() if e["path"] == model["path"]]
        shared = set(self.project.config.get("shared_models", []))
        visited = {model["name"]}
        frontier = set(visited)
        result = []
        seen = set()
        for distance in range(1, depth + 1):
            following = set()
            for edge in edges:
                outgoing = direction in {"outgoing", "both"} and edge["source"] in frontier
                incoming = direction in {"incoming", "both"} and edge["target"] in frontier
                if outgoing or incoming:
                    key = (edge["source"], edge["field"])
                    if key not in seen:
                        result.append({**edge, "depth": distance})
                        seen.add(key)
                    following.add(edge["target"] if outgoing else edge["source"])
            frontier = following - visited
            visited |= following
            if not include_shared:
                frontier -= shared
        return result
