"""Command catalog, argument parsing, output contract and entry point."""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import sys
from typing import Any

from . import __version__
from .analysis import Analysis
from .modules import Modules
from .project import DevkitError, Project
from .schema import Schema
from .source import SourceIndex, match_name

EXAMPLES = {
    "help": ["devkit help", "devkit help module inspect", "devkit help --json"],
    "search": [
        'devkit search "organizationId" backend/src',
        'devkit search "use[A-Z].*Products" frontend --regex --json',
    ],
    "find": ['devkit find "invoice"', 'devkit find "product.*\\.api\\.ts$" --regex'],
    "tree": ["devkit tree --depth 3", "devkit tree frontend/src --dirs-only"],
    "symbols": [
        "devkit symbols backend/src --exported-only",
        "devkit symbols --name Product --kind interface",
    ],
    "module list": ["devkit module list --app erp"],
    "module inspect": [
        "devkit module inspect products",
        "devkit module inspect inventory --scope prisma --json",
    ],
    "module impact": ["devkit module impact products --json"],
    "module check": ["devkit module check all", "devkit module check products"],
    "schema list": ["devkit schema list --kind enum", "devkit schema list --module products"],
    "schema inspect": ["devkit schema inspect Product", "devkit schema inspect InvoiceStatus --json"],
    "schema relations": [
        "devkit schema relations Product --depth 2",
        "devkit schema relations Product --direction incoming",
    ],
    "trace route": ['devkit trace route "/api/products/:id" --method PATCH'],
    "trace symbol": ["devkit trace symbol useProducts --json"],
    "impact model": ["devkit impact model Product"],
    "impact field": ["devkit impact field Product.baseSellingPrice --json"],
    "impact file": ["devkit impact file backend/src/core/features/products/product.service.ts"],
}

OUTPUTS = {
    "help": "Command catalog with usage, arguments, flags, examples and exit codes",
    "search": "Relative path, line, column, matched text and context",
    "find": "Relative paths and file/directory types",
    "tree": "Nested path nodes, names, types and children",
    "symbols": "Symbol name, kind, location, signature and export status",
    "module list": "Module names, aliases, applications, available layers and counts",
    "module inspect": "Owned models/tables, enums, dependencies, consumers, missing models and categorized files with reasons",
    "module impact": "Schema consumers, dependent modules, import consumers, coupled files and validation areas",
    "module check": "Checks with pass/fail/warning status and aggregate counts",
    "schema list": "Model/enum names, model ownership, physical table names and locations",
    "schema inspect": "Fields, defaults, mappings, constraints, indexes, relations and location; enum values for enums",
    "schema relations": "Directed relationship edges with cardinality, foreign keys, delete behavior and graph distance",
    "trace route": "Static route registrations, middleware/handler expressions, imported dependencies and client/model candidates",
    "trace symbol": "Definitions, imports/aliases, exports and candidate identifier references",
    "impact model": "Model references, schema relations, module context and import consumers",
    "impact field": "Field definition, candidate field-name references and dependent files",
    "impact file": "File ownership, definitions, imports and reverse import consumers",
}


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise DevkitError(message)


def bounded_int(low: int, high: int):
    def parse(value: str) -> int:
        try:
            number = int(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError("must be an integer") from exc
        if not low <= number <= high:
            raise argparse.ArgumentTypeError(f"must be between {low} and {high}")
        return number

    return parse


def build_parser() -> tuple[Parser, dict[str, Parser]]:
    common = Parser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="Emit versioned JSON")
    common.add_argument(
        "--root", default=argparse.SUPPRESS, help="Explicit project root; paths are relative to it"
    )
    common.add_argument("--config", default=argparse.SUPPRESS, help="Explicit ElectroERP YAML manifest")
    common.add_argument(
        "--app",
        choices=["erp", "hub", "manager", "all"],
        default=argparse.SUPPRESS,
        help="Application scope (default: all available roots)",
    )
    common.add_argument(
        "--max-results",
        type=bounded_int(1, 100000),
        default=argparse.SUPPRESS,
        help="Maximum rows per result collection (default: 100); truncation is reported",
    )
    common.add_argument(
        "--no-cache",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Bypass persistent source summaries and parse live files",
    )
    common.add_argument(
        "--max-output-chars",
        type=bounded_int(2048, 10000000),
        default=argparse.SUPPRESS,
        help="Total response character budget (default: 32000); JSON remains valid",
    )
    root = Parser(
        prog="devkit", description="ElectroERP code discovery for humans and coding agents.", parents=[common]
    )
    root.add_argument("--version", action="version", version=f"devkit {__version__}")
    registry = {"": root}
    subs = root.add_subparsers(dest="command")

    def add(subparsers, name: str, description: str, prefix: str = "") -> Parser:
        key = (prefix + " " + name).strip()
        examples = EXAMPLES.get(key, [])
        epilog = "Examples:\n  " + "\n  ".join(examples) if examples else ""
        if key in OUTPUTS:
            epilog += "\nOutput: " + OUTPUTS[key]
        epilog += "\nFormats: readable text (default) or --json. Scope: project root and --app; only Devkit .ignore applies."
        epilog += "\nExit codes: 0 success (including empty matches), 1 failed module checks, 2 invalid input/configuration or I/O failure."
        parser = subparsers.add_parser(
            name,
            help=description,
            description=description,
            parents=[common],
            epilog=epilog,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        registry[key] = parser
        return parser

    help_parser = add(subs, "help", "List commands or show detailed command help.")
    help_parser.add_argument("topic", nargs="*", help="Command path, such as module inspect")
    search = add(subs, "search", "Search text contents; literal smart-case matching by default.")
    search.add_argument("query", help="Literal query, or a regular expression with --regex")
    search.add_argument("path", nargs="?", default=".", help="Directory/file relative to project root")
    search.add_argument("--regex", action="store_true", help="Interpret query as a regular expression")
    search.add_argument(
        "--context", type=bounded_int(0, 100), default=0, help="Surrounding lines (default: 0)"
    )
    search.add_argument(
        "--glob", action="append", default=[], help="Restrict paths by glob; repeat for alternatives"
    )
    find = add(subs, "find", "Find paths by smart-case substring or regular expression.")
    find.add_argument("pattern", help="Path substring or regex")
    find.add_argument("path", nargs="?", default=".", help="Directory/file relative to project root")
    find.add_argument("--regex", action="store_true", help="Interpret pattern as a regular expression")
    find.add_argument(
        "--type", choices=["file", "dir", "all"], default="all", help="Result type (default: all)"
    )
    find.add_argument(
        "--extension", action="append", default=[], help="File extension, with or without dot; repeatable"
    )
    tree = add(subs, "tree", "Show a compact project tree respecting Devkit ignore rules.")
    tree.add_argument("path", nargs="?", default=".", help="Directory/file relative to project root")
    tree.add_argument(
        "--depth", type=bounded_int(0, 20), default=3, help="Levels below target (default: 3, max: 20)"
    )
    tree.add_argument("--dirs-only", action="store_true", help="Include directories only")
    symbols = add(subs, "symbols", "List parsed JavaScript/TypeScript definitions (including JSX/TSX).")
    symbols.add_argument("path", nargs="?", default=".", help="Directory/file relative to project root")
    symbols.add_argument("--name", help="Smart-case substring filter on names")
    symbols.add_argument(
        "--kind",
        choices=["function", "class", "method", "interface", "type", "enum", "variable"],
        action="append",
        default=[],
        help="Symbol kind; repeatable",
    )
    symbols.add_argument("--exported-only", action="store_true", help="Include exported definitions only")
    module = add(subs, "module", "Inspect ElectroERP modules, dependencies and consistency.")
    msubs = module.add_subparsers(dest="action")
    add(msubs, "list", "List configured and discovered modules and available layers.", "module")
    for action, desc in [
        ("inspect", "Show owned models, related models and files with reasons."),
        ("impact", "Estimate cross-module consequences of changing a module."),
        ("check", "Run read-only ownership, companion, import and documentation checks."),
    ]:
        parser = add(msubs, action, desc, "module")
        parser.add_argument(
            "module",
            help="Module name or alias" + ("; 'all' checks available modules" if action == "check" else ""),
        )
        if action == "inspect":
            parser.add_argument(
                "--scope",
                choices=["prisma", "backend", "frontend", "ai", "docs", "tests", "all"],
                default="all",
                help="Filter files by layer (default: all)",
            )
            parser.add_argument(
                "--related",
                choices=["none", "direct"],
                default="direct",
                help="Include direct relations (default: direct)",
            )
    schema = add(subs, "schema", "Read Prisma model and enum definitions without database access.")
    ssubs = schema.add_subparsers(dest="action")
    listing = add(ssubs, "list", "List Prisma models/enums, tables and ownership.", "schema")
    listing.add_argument(
        "--kind", choices=["model", "enum", "all"], default="all", help="Declaration kind (default: all)"
    )
    listing.add_argument("--module", help="Restrict to a module's models and referenced enums")
    inspect = add(
        ssubs, "inspect", "Inspect a Prisma model or enum, including fields and constraints.", "schema"
    )
    inspect.add_argument("model", help="Canonical or case-insensitive model/enum name")
    relations = add(ssubs, "relations", "Explore incoming/outgoing foreign-key relations.", "schema")
    relations.add_argument("model", help="Prisma model name")
    relations.add_argument(
        "--depth", type=bounded_int(1, 5), default=1, help="Graph depth (default: 1, max: 5)"
    )
    relations.add_argument(
        "--direction",
        choices=["incoming", "outgoing", "both"],
        default="both",
        help="Relation direction (default: both)",
    )
    relations.add_argument(
        "--include-shared",
        action="store_true",
        help="Traverse through shared hub models after their first edge",
    )
    trace = add(subs, "trace", "Trace static routes or code symbols with source evidence.")
    tsubs = trace.add_subparsers(dest="action")
    route = add(
        tsubs, "route", "Trace literal Express routes, imports, middleware and candidate consumers.", "trace"
    )
    route.add_argument("target", help="Literal full or local route, including :parameter placeholders")
    route.add_argument(
        "--full",
        action="store_true",
        help="Include all dependency-file definitions (still subject to output limits)",
    )
    route.add_argument(
        "--method",
        type=str.upper,
        choices=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD", "ALL"],
        help="HTTP method",
    )
    symbol = add(
        tsubs, "symbol", "Find definitions, imports, aliases and candidate symbol references.", "trace"
    )
    symbol.add_argument("target", help="Exact symbol name")
    impact = add(subs, "impact", "Estimate change impact for a typed target.")
    isubs = impact.add_subparsers(dest="action")
    for kind, desc in [
        ("model", "Find model references, schema relations and module context."),
        ("field", "Find Model.field usage candidates across source layers."),
        ("file", "Find a file's ownership, imports and reverse consumers."),
    ]:
        parser = add(isubs, kind, desc, "impact")
        parser.add_argument(
            "target",
            help={"model": "Prisma model name", "field": "Model.field", "file": "Project-relative file path"}[
                kind
            ],
        )
    return root, registry


def catalog(registry: dict[str, Parser], topic: str = "") -> list[dict[str, Any]]:
    if topic not in registry:
        raise DevkitError(f"Unknown help topic: {topic}")
    rows = []
    for name, parser in registry.items():
        if not name or (topic and name != topic and not name.startswith(topic + " ")):
            continue
        options = []
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                continue
            options.append(
                {
                    "name": action.dest,
                    "flags": action.option_strings,
                    "required": action.required,
                    "nargs": action.nargs,
                    "choices": action.choices,
                    "help": action.help,
                    "default": None if action.default == argparse.SUPPRESS else action.default,
                }
            )
        rows.append(
            {
                "command": "devkit " + name,
                "description": parser.description,
                "usage": parser.format_usage().strip(),
                "arguments": options,
                "examples": EXAMPLES.get(name, []),
                "output": OUTPUTS.get(name, "Subcommand group"),
                "scope": "Selected app and project-local .ignore; help reads no project files",
                "exit_codes": {
                    "0": "Success (including no matches)",
                    "1": "Module check failures",
                    "2": "Invalid input, configuration or I/O failure",
                },
            }
        )
    return rows


def matcher(query: str, regex: bool = False):
    if not query:
        raise DevkitError("Search query/pattern cannot be empty")
    flags = 0 if any(c.isupper() for c in query) else re.IGNORECASE
    try:
        return re.compile(query if regex else re.escape(query), flags)
    except re.error as exc:
        raise DevkitError(f"Invalid regular expression: {exc}") from exc


def search(project: Project, args: argparse.Namespace) -> list[dict[str, Any]]:
    pattern = matcher(args.query, args.regex)
    result = []
    # Stop after one extra result: that is sufficient to report truncation honestly.
    for path in project.entries(args.path):
        relative = project.rel(path)
        if args.glob and not any(
            fnmatch.fnmatchcase(relative, g) or fnmatch.fnmatchcase(path.name, g) for g in args.glob
        ):
            continue
        content = project.text(path)
        if content is None:
            continue
        lines = content.splitlines()
        for index, line in enumerate(lines):
            for match in pattern.finditer(line):
                result.append(
                    {
                        "path": relative,
                        "line": index + 1,
                        "column": match.start() + 1,
                        "match": match.group(),
                        "text": line,
                        "before": [
                            {"line": j + 1, "text": lines[j]}
                            for j in range(max(0, index - args.context), index)
                        ],
                        "after": [
                            {"line": j + 1, "text": lines[j]}
                            for j in range(index + 1, min(len(lines), index + 1 + args.context))
                        ],
                    }
                )
                if len(result) > args.max_results:
                    return result
    return result


def find(project: Project, args: argparse.Namespace) -> list[dict[str, Any]]:
    pattern = matcher(args.pattern, args.regex)
    paths = []
    if args.type != "dir":
        paths.extend(project.entries(args.path))
    if args.type != "file" and not args.extension:
        paths.extend(project.entries(args.path, directories=True))
    extensions = {"." + ext.lstrip(".").casefold() for ext in args.extension}
    return [
        {"path": project.rel(p), "type": "dir" if p.is_dir() else "file"}
        for p in sorted(paths)
        if pattern.search(project.rel(p)) and (not extensions or p.suffix.casefold() in extensions)
    ]


def tree(project: Project, args: argparse.Namespace) -> dict[str, Any]:
    root = project.resolve(args.path)
    selected = project.entries(args.path, directories=True)
    if not args.dirs_only:
        selected += project.entries(args.path)
    node = {
        "path": project.rel(root) or ".",
        "name": root.name,
        "type": "dir" if root.is_dir() else "file",
        "children": [],
    }
    mapping = {root: node}
    count = 0
    truncated = False
    for path in sorted(set(selected), key=lambda p: (len(p.parts), str(p))):
        depth = len(path.relative_to(root).parts)
        if depth == 0 or depth > args.depth:
            continue
        if count == args.max_results:
            truncated = True
            break
        if path.parent in mapping:
            child = {"path": project.rel(path), "name": path.name, "type": "dir" if path.is_dir() else "file"}
            if path.is_dir():
                child["children"] = []
            mapping[path.parent]["children"].append(child)
            mapping[path] = child
            count += 1
    node["truncated"] = truncated
    node["shown_nodes"] = count
    node["depth"] = args.depth
    return node


def execute(project: Project, args: argparse.Namespace) -> tuple[Any, int]:
    command, action = args.command, getattr(args, "action", None)
    if command == "search":
        return search(project, args), 0
    if command == "find":
        return find(project, args), 0
    if command == "tree":
        return tree(project, args), 0
    if command == "symbols":
        selected = project.entries(args.path)
        index = SourceIndex(project, paths=selected)
        return [
            s
            for s in index.symbols
            if (not args.name or match_name(args.name, s["name"]))
            and (not args.kind or s["kind"] in args.kind)
            and (not args.exported_only or s["exported"])
        ], 0
    if command in {"module", "schema"}:
        schema = Schema(project)
        if command == "schema" and not schema.paths:
            raise DevkitError(
                "No readable Prisma schema files found in the selected scope. Check --root, --app and .ignore."
            )
        modules = Modules(project, schema)
        if command == "module":
            if action == "list":
                return modules.catalog(), 0
            if action == "inspect":
                return modules.inspect(args.module, args.scope, args.related), 0
            if action == "impact":
                return modules.impact(args.module), 0
            result = modules.check(args.module)
            return result, 1 if result["summary"]["fail"] else 0
        if action == "list":
            wanted = None
            if args.module:
                inspection = modules.inspect(args.module)
                wanted = {m["name"] for m in inspection["models"]} | set(inspection["enums"])
            return [
                modules.model_info(d)
                for d in schema.declarations
                if args.kind in {"all", d["kind"]} and (wanted is None or d["name"] in wanted)
            ], 0
        if action == "inspect":
            model = schema.get(args.model)
            return {**model, "module": modules.owner(model["name"], model["app"])}, 0
        return schema.relations(args.model, args.depth, args.direction, args.include_shared), 0
    analysis = Analysis(project)
    if command == "trace":
        if action == "route":
            return analysis.trace_route(args.target, args.method, args.full), 0
        return analysis.source.symbol_trace(args.target), 0
    if command == "impact":
        return {"model": analysis.model_impact, "field": analysis.field_impact, "file": analysis.file_impact}[
            action
        ](args.target), 0
    raise DevkitError("Unknown command")


def limit_data(data: Any, maximum: int, location: str = "data") -> tuple[Any, list[dict[str, Any]]]:
    truncated = []
    if isinstance(data, list):
        if len(data) > maximum:
            truncated.append({"collection": location, "shown": maximum, "available_at_least": len(data)})
        rows = []
        for index, row in enumerate(data[:maximum]):
            value, child_truncation = limit_data(row, maximum, f"{location}[{index}]")
            rows.append(value)
            truncated.extend(child_truncation)
        return rows, truncated
    if isinstance(data, dict):
        result = {}
        for key, value in data.items():
            result[key], more = limit_data(value, maximum, f"{location}.{key}")
            truncated.extend(more)
        return result, truncated
    return data, []


def render_tree(node: dict[str, Any], prefix: str = "") -> list[str]:
    lines = [node["name"] + ("/" if node["type"] == "dir" else "")]
    children = node.get("children", [])
    for index, child in enumerate(children):
        last = index == len(children) - 1
        rendered = render_tree(child)
        lines.append(prefix + ("└── " if last else "├── ") + rendered[0])
        lines.extend(prefix + ("    " if last else "│   ") + line for line in rendered[1:])
    return lines


def render(data: Any, indent: int = 0) -> list[str]:
    prefix = " " * indent
    if isinstance(data, list):
        if not data:
            return [prefix + "(none)"]
        lines = []
        for row in data:
            if isinstance(row, dict) and {"path", "line", "text"} <= row.keys():
                lines.append(prefix + f"{row['path']}:{row['line']}:{row.get('column', 1)}  {row['text']}")
                for context_key in ("before", "after"):
                    lines.extend(prefix + f"  {c['line']}: {c['text']}" for c in row.get(context_key, []))
            elif isinstance(row, dict) and "path" in row and len(row) <= 8:
                scalar = "  ".join(
                    f"{k}={v}"
                    for k, v in row.items()
                    if k != "path" and not isinstance(v, (dict, list)) and v is not None
                )
                lines.append(prefix + f"{row['path']}  {scalar}".rstrip())
                for key, value in row.items():
                    if isinstance(value, (dict, list)) and value:
                        lines.append(prefix + f"  {key}:")
                        lines.extend(render(value, indent + 4))
            else:
                lines.extend(render(row, indent))
        return lines
    if isinstance(data, dict):
        lines = []
        for key, value in data.items():
            if isinstance(value, (dict, list)):
                lines.append(prefix + key + ":")
                lines.extend(render(value, indent + 2))
            else:
                lines.append(prefix + f"{key}: {value}")
        return lines
    return [prefix + str(data)]


def bound_output(result: dict[str, Any], maximum: int, output) -> None:
    """Shrink structured collections, not serialized JSON. Preserve check summaries."""
    if len(output()) + 2 <= maximum:
        return
    result["truncated"] = True
    result["truncation"] = [
        {
            "reason": "total_output_budget",
            "max_output_chars": maximum,
            "message": "Collections or strings omitted/shortened; narrow the query or raise output limits.",
        }
    ]
    while len(output()) + 2 > maximum:
        candidates = []

        def visit(value, parent=None, key=None, candidates=candidates):
            if isinstance(value, list):
                if value:
                    # Prefer trimming evidence collections over dropping an enclosing route.
                    nested = any(
                        isinstance(row, dict) and any(isinstance(v, list) and v for v in row.values())
                        for row in value
                    )
                    candidates.append((len(json.dumps(value)) / (10 if nested else 1), parent, key, value))
                for i, row in enumerate(value):
                    visit(row, value, i)
            elif isinstance(value, dict):
                for k, row in value.items():
                    if k != "summary":
                        visit(row, value, k)
            elif isinstance(value, str) and len(value) > 160:
                candidates.append((len(value), parent, key, value))

        visit(result["data"], result, "data")
        visit(result["warnings"], result, "warnings")
        if not candidates:
            # A very wide scalar mapping cannot be pruned safely field-by-field.
            summary = result["data"].get("summary") if isinstance(result["data"], dict) else None
            result["data"] = {
                "message": "Result omitted to fit output budget",
                **({"summary": summary} if summary else {}),
            }
            break
        _, parent, key, value = max(candidates, key=lambda row: row[0])
        parent[key] = (
            value[: len(value) // 2] if isinstance(value, list) else value[: max(80, len(value) // 2)] + "…"
        )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    json_mode = "--json" in argv
    command = ""
    try:
        parser, registry = build_parser()
        args = parser.parse_args(argv)
        args.json = getattr(args, "json", False)
        args.max_results = getattr(args, "max_results", 100)
        args.app = getattr(args, "app", "all")
        args.root = getattr(args, "root", None)
        args.config = getattr(args, "config", None)
        command = " ".join(p for p in [args.command, getattr(args, "action", None)] if p)
        if (
            args.command == "help"
            or not args.command
            or (args.command in {"module", "schema", "trace", "impact"} and not args.action)
        ):
            topic = " ".join(args.topic) if args.command == "help" else (args.command or "")
            data = catalog(registry, topic)
            if args.json:
                print(
                    json.dumps({"schema_version": "1.0", "command": "help", "data": data}, ensure_ascii=False)
                )
            else:
                print(registry[topic].format_help())
                if not topic:
                    for row in data:
                        print(f"{row['command']:<30} {row['description']}")
            return 0
        project = Project(args.root, args.config, args.app)
        project.use_cache = not getattr(args, "no_cache", False)
        data, status = execute(project, args)
        data, truncation = limit_data(data, args.max_results)
        if args.command == "tree" and data["truncated"]:
            truncation.append(
                {
                    "collection": "data.children",
                    "shown": data["shown_nodes"],
                    "available_at_least": data["shown_nodes"] + 1,
                }
            )
        result = {
            "schema_version": "1.0",
            "command": command,
            "root": str(project.root),
            "app": project.app,
            "data": data,
            "warnings": list(dict.fromkeys(project.warnings)),
            "truncated": bool(truncation),
            "truncation": truncation,
        }
        maximum = getattr(args, "max_output_chars", 32000)
        output = lambda: (
            json.dumps(result, ensure_ascii=False)
            if args.json
            else "\n".join(render_tree(result["data"]) if args.command == "tree" else render(result["data"]))
            + "\n"
            + "\n".join("Warning: " + w for w in result["warnings"])
            + (
                "\nResults truncated; increase --max-results / --max-output-chars or narrow the query."
                if result["truncated"]
                else ""
            )
        )
        bound_output(result, maximum, output)
        if args.command == "tree" and any(
            t.get("reason") == "total_output_budget" for t in result["truncation"]
        ):

            def count_nodes(node):
                return sum(1 + count_nodes(child) for child in node.get("children", []))

            result["data"]["shown_nodes"] = count_nodes(result["data"])
            result["data"]["truncated"] = True
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(
                "\n".join(render_tree(result["data"]) if args.command == "tree" else render(result["data"]))
            )
            if result["truncated"]:
                print(
                    "Results truncated; increase --max-results / --max-output-chars or narrow the query.",
                    file=sys.stderr,
                )
            for warning in result["warnings"]:
                print("Warning: " + warning, file=sys.stderr)
        return status
    except BrokenPipeError:
        return 0
    except (DevkitError, OSError, ValueError) as exc:
        if json_mode:
            print(
                json.dumps(
                    {"schema_version": "1.0", "command": command, "error": {"message": str(exc), "code": 2}}
                )
            )
        else:
            print(f"devkit: {exc}", file=sys.stderr)
        return 2
