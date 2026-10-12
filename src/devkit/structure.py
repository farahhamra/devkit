"""Lexical structure from AST ancestry, never guessed from overlapping lines."""

from __future__ import annotations

CALLABLES = {"arrow_function", "function_expression", "generator_function"}
CLASSES = {"class", "class_expression"}
KINDS = {
    "function_declaration": "function",
    "generator_function_declaration": "function",
    "function_signature": "function",
    "class_declaration": "class",
    "abstract_class_declaration": "class",
    "interface_declaration": "interface",
    "type_alias_declaration": "type",
    "enum_declaration": "enum",
    "method_definition": "method",
    "method_signature": "method",
    "abstract_method_signature": "method",
    "property_signature": "property",
    "enum_assignment": "enum_member",
    **{name: "function" for name in CALLABLES},
    **{name: "class" for name in CLASSES},
}


def span(node, content: bytes, path: str) -> dict:
    """Inclusive 1-based lines; 1-based UTF-8 byte columns (end exclusive)."""
    end_line, end_column = node.end_point.row + 1, node.end_point.column + 1
    if node.end_point.column == 0 and node.end_point.row > node.start_point.row:
        end_line -= 1
        end = node.end_byte - 1  # Final LF belongs to the previous line.
        if end and content[end - 1 : end] == b"\r":
            end -= 1
        end_column = end - content.rfind(b"\n", 0, end)
    return {
        "path": path,
        "line": node.start_point.row + 1,
        "column": node.start_point.column + 1,
        "end_line": end_line,
        "end_column": end_column,
        "start_byte": node.start_byte,
        "end_byte": node.end_byte,
    }


def valid_span(row, path):
    return (
        isinstance(row, dict)
        and row.get("path") == path
        and all(
            type(row.get(k)) is int
            for k in ("line", "end_line", "column", "end_column", "start_byte", "end_byte")
        )
        and 1 <= row["line"] <= row["end_line"]
        and min(row["column"], row["end_column"]) >= 1
        and 0 <= row["start_byte"] <= row["end_byte"]
        and (row["end_line"] > row["line"] or row["end_column"] >= row["column"])
    )


def outline(root, content: bytes, path: str) -> list[dict]:
    rows = []
    pending = [(root, None)]
    represented_values = set()
    by_id = {}
    while pending:
        node, parent = pending.pop()
        kind = KINDS.get(node.type)
        name_node = node.child_by_field_name("name")
        value = node.child_by_field_name("value")
        if node.type in {"variable_declarator", "public_field_definition", "field_definition", "pair"}:
            if node.type == "field_definition":
                name_node = node.child_by_field_name("property")
            if node.type == "pair":
                name_node = node.child_by_field_name("key")
            if node.type != "pair" or (value and value.type in CALLABLES | CLASSES):
                kind = (
                    "function"
                    if value and value.type in CALLABLES
                    else "class"
                    if value and value.type in CLASSES
                    else "variable"
                )
                if value and value.type in CALLABLES | CLASSES:
                    represented_values.add(value.id)
        if node.id in represented_values:
            kind = None  # The binding and its function/class value describe one construct.
        if kind and node.end_byte > node.start_byte:
            location = span(node, content, path)
            line, column = location["line"], location["column"]
            name = name_node.text.decode("utf-8") if name_node else f"(anonymous@{line}:{column})"
            identifier = f"{kind}@{line}:{column}-{location['end_line']}:{location['end_column']}"
            current = node
            exported = False
            while current.parent and current.parent.type in {
                "lexical_declaration",
                "variable_declaration",
                "export_statement",
            }:
                current = current.parent
                exported |= current.type == "export_statement"
            row = {
                "id": identifier,
                "name": name,
                "kind": kind,
                **location,
                "parent_id": parent,
                "children_ids": [],
                "qualified_name": f"{by_id[parent]['qualified_name']}.{name}" if parent else name,
                "exported": exported,
                "anonymous": name_node is None,
                "evidence": "syntax_tree",
            }
            if parent:
                by_id[parent]["children_ids"].append(identifier)
            by_id[identifier] = row
            rows.append(row)
            parent = identifier
        pending.extend((child, parent) for child in reversed(node.named_children))
    return rows


def valid_outline(rows, path):
    """Validate optional cached structure before using spans or relationships."""
    if not isinstance(rows, list):
        return False
    by_id = {}
    for row in rows:
        if not isinstance(row, dict) or not all(
            isinstance(row.get(k), str) for k in ("id", "name", "kind", "qualified_name", "evidence")
        ):
            return False
        if not valid_span(row, path) or row["id"] in by_id:
            return False
        if row["start_byte"] == row["end_byte"] or not all(
            type(row.get(k)) is bool for k in ("exported", "anonymous")
        ):
            return False
        if not isinstance(row.get("children_ids"), list) or not all(
            isinstance(x, str) for x in row["children_ids"]
        ):
            return False
        if len(set(row["children_ids"])) != len(row["children_ids"]):
            return False
        parent = row.get("parent_id")
        if parent is not None:
            if not isinstance(parent, str) or parent not in by_id:
                return False
            outer = by_id[parent]
            if not (outer["start_byte"] <= row["start_byte"] < row["end_byte"] <= outer["end_byte"]):
                return False
            if row["id"] not in outer["children_ids"]:
                return False
        by_id[row["id"]] = row
    return all(
        child in by_id and by_id[child].get("parent_id") == row["id"]
        for row in rows
        for child in row["children_ids"]
    )
