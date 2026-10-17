"""Command catalog, argument parsing, output contract and entry point."""

from __future__ import annotations

import argparse
import fnmatch
import json
import math
import re
import sys
import time
from typing import Any

from . import __version__
from .agents import AgentsContext, AgentSync
from .coverage import Coverage, coverage_line
from .diagnostics import DiagnosticContext, Diagnostics
from .feedback import EVIDENCE, SEVERITIES, STATUSES, TYPES, FeedbackContext, FeedbackStore
from .plan import PlanArchive, PlanContext
from .project import DevkitError, Project
from .schema import Schema

EXAMPLES = {
    "plan archive": [
        "devkit plan archive <conversation-id> --dry-run",
        "devkit plan archive <conversation-id> --root /path/to/ElectroERP --json",
    ],
    "performance database": [
        "devkit performance database --input diagnostics/performance/capture.json --app erp --json",
        "devkit performance database --input diagnostics/performance/capture.json --strict --json",
    ],
    "performance runtime": [
        "devkit performance runtime --input diagnostics/performance/cpu.cpuprofile --app erp --json"
    ],
    "performance memory": [
        "devkit performance memory --input diagnostics/performance/heap.heapsnapshot --app erp --json"
    ],
    "performance cache": [
        "devkit performance cache --input diagnostics/performance/cache.json --app erp --json"
    ],
    "performance compare": [
        "devkit performance compare --before diagnostics/performance/before.json --after diagnostics/performance/after.json --app erp --json",
        "devkit performance compare --before before.json --after after.json --regression-percent 10 --fail-on-regression",
    ],
    "audit routes": ["devkit audit routes --app erp --policy devkit/audit.yaml --json"],
    "audit isolation": ["devkit audit isolation backend/src --app erp --json"],
    "audit security": ["devkit audit security backend/src --app erp --json"],
    "audit dependencies": ["devkit audit dependencies --app erp --json"],
    "audit unused": ["devkit audit unused backend/src --entry backend/src/app.ts --app erp --json"],
    "audit duplicates": ["devkit audit duplicates frontend/src --app erp --min-lines 8 --json"],
    "doctor": ["devkit doctor --app erp --json", "devkit doctor --root /path/to/ElectroERP"],
    "config inspect": [
        "devkit config inspect --json",
        "devkit config inspect --config .agents/devkit/electroerp.yaml --json",
    ],
    "config validate": ["devkit config validate --app erp --json"],
    "agents sync": [
        "devkit agents sync --dry-run --root /path/to/ElectroERP",
        "devkit agents sync --check --json",
        "devkit agents sync",
    ],
    "help": ["devkit help", "devkit help module inspect", "devkit help --json"],
    "search": [
        'devkit search "organizationId" backend/src',
        'devkit search "use[A-Z].*Products" frontend --regex --json',
        'devkit search "world-countries" frontend/src --timings --json',
    ],
    "find": [
        'devkit find "invoice"',
        "devkit find customers --type dir --app erp",
        'devkit find "product.*\\.api\\.ts$" --regex',
    ],
    "tree": ["devkit tree --depth 3", "devkit tree frontend/src --dirs-only"],
    "symbols": [
        "devkit symbols backend/src --exported-only",
        "devkit symbols --name Product --kind interface",
    ],
    "analyze": [
        "devkit analyze backend/src/core/features/products/product.service.ts --app erp --json",
        "devkit analyze backend/src/core/features/products/product.service.ts --symbol listProducts --json",
        "devkit analyze backend/src/core/features/products/product.service.ts --with-context --app erp --json",
    ],
    "query": [
        "devkit query 'prisma.product.findMany($$$ARGS)' backend/src --app erp --json",
        "devkit query 'router.$METHOD($PATH, $$$HANDLERS)' backend/src --lang ts --json",
        "devkit query 'useState($INITIAL)' frontend/src --lang tsx --json",
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
    "feedback add": [
        'devkit feedback add --type bug --title "Existing tool reported missing" --evidence observed --actual "Exit 1"'
    ],
    "feedback list": ["devkit feedback list --status open --type bug --json"],
    "feedback inspect": ["devkit feedback inspect <id> --json"],
    "feedback update": [
        'devkit feedback update <id> --status resolved --note "Verified with the reproduction command"'
    ],
}

OUTPUTS = {
    "plan archive": "Shared archive index, plan/task source and destination paths, verification and source-clearing status; no document contents",
    "performance database": "Offline query timing/count rankings by measurement stream, MySQL plan estimates, Prisma/module mappings, index candidates, verified available source ranges and explicit coverage gaps",
    "performance runtime": "V8 sampled self/inclusive CPU estimates, event-loop histograms, GC event intervals and explicit coverage gaps; no live profiling",
    "performance memory": "Process-memory trends, V8 heap constructor groups, bounded dominator retention estimates and retaining paths; no raw string/property values",
    "performance cache": "Epoch-aware counter deltas, hit ratios, observed latency distributions, storage growth and exported capacity/TTL context",
    "performance compare": "Metadata compatibility, normalized matching metrics, unknown/unmatched measurements and threshold-based regression candidates; no statistical significance claim",
    "audit routes": "Configured authentication/role/permission middleware checks, ordered mount evidence, exact registration spans and explicit unknowns",
    "audit isolation": "Prisma organization-scope contracts, exact call spans, missing constraints and unsupported/dynamic cases",
    "audit security": "Bundled Semgrep Community security candidates with source spans; no source/secret values echoed",
    "audit dependencies": "OSV-Scanner lockfile advisories, package versions/severity and available package spans; online advisory lookup by default",
    "audit unused": "Knip graph candidates with explicit entrypoints and ESLint TypeScript-aware unused local binding candidates",
    "audit duplicates": "jscpd token clone candidates with both occurrences and inclusive line ranges",
    "doctor": "Read-only installation, parser, configuration, storage and agent-template checks with remediation and summary counts",
    "config inspect": "Effective default/override settings, entry origins, ignore rules and storage locations; unknown values omitted",
    "config validate": "Read-only manifest/path/ignore/alias checks with actionable diagnostics and summary counts",
    "agents sync": "Per-file sync plan/status, template version, conflicts and local backup path; --dry-run includes managed-content diffs",
    "feedback add": "Saved local report, stable ID, occurrence count and duplicate flag; no uploads",
    "feedback list": "Local report summaries, filtered total and pagination offset",
    "feedback inspect": "Full local report, sanitized reproduction details and update history",
    "feedback update": "Updated local report and status/assessment history; no automatic code changes",
    "help": "Command catalog with usage, arguments, flags, examples and exit codes",
    "search": "Relative path, line, column, matched text and context",
    "find": "Relative paths and file/directory types",
    "tree": "Nested path nodes, names, types and children",
    "symbols": "Symbol name, kind, location, signature and export status",
    "analyze": "Numbered source, exact source spans, lexical symbol hierarchy, imports/exports, call/reference/route/Prisma candidates and coverage; optional selected-app context",
    "query": "Structural code matches and captured syntax nodes with relative paths, exact source spans, engine version, full scan counts and coverage",
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


def regression_percent(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(number) or not 0 <= number <= 1_000_000:
        raise argparse.ArgumentTypeError("must be finite and between 0 and 1000000")
    return number


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
        epilog += (
            "\nFormats: readable text or --json. Read-only: no source scan, storage initialization, network, database connection or automatic repair. --app scopes configured-path checks, not global manifest structure."
            if key == "doctor" or key.startswith("config")
            else "\nFormats: readable text or --json. Scope: fixed agent files under --root, independent of --config/--app/--no-cache. Templates are bundled; no network access or source indexing."
            if key.startswith("agents")
            else "\nFormats: readable text or --json. Scope: current user's Antigravity brain and project-local .agents/implementation/trash under --root. --app/--config/--no-cache do not select paths; no source parsing or YAML required. Apply archives both documents and clears their contents; --dry-run writes nothing."
            if key.startswith("plan")
            else "\nFormats: readable text or --json. Scope: local project feedback and --app. YAML/source are not inspected; --no-cache does not disable durable feedback storage."
            if key.startswith("feedback")
            else "\nFormats: readable text (default) or --json. Scope: project root and --app; only Devkit .ignore applies. Analysis includes measured coverage (stages, counts, gaps and exclusions); complete processing is not runtime proof."
        )
        epilog += (
            "\nExit codes: 0 report generated (findings/unknowns may remain), 1 incomplete --strict performance analysis or comparison --fail-on-regression gate, 2 invalid/unsafe capture or configuration/I/O failure. Offline evidence only: no SQL, instrumentation, profiling or load generation."
            if key.startswith("performance")
            else "\nExit codes: 0 no finding gate triggered (inspect coverage/unknowns), 1 policy violation, finding at --fail-on severity or incomplete --strict audit, 2 invalid policy, missing engine or scanner failure. Static evidence is not runtime security proof; no automatic fixes."
            if key.startswith("audit")
            else "\nExit codes: 0 successful inspection or checks without failures (warnings may remain), 1 doctor/config validate found failed checks, 2 invalid arguments/root or inspection could not complete."
            if key == "doctor" or key.startswith("config")
            else "\nExit codes: 0 success/current, 1 --check detected drift/conflicts, 2 unsafe/conflicting apply or invalid input/storage. --dry-run and --check never write. --force requires explicit approval to replace local edits; backups are always kept."
            if key.startswith("agents")
            else "\nExit codes: 0 archived/already archived or valid preview, 2 invalid input, unsafe path, copy/verification failure or incomplete clearing. Inspect per-file status and recovery record on failure."
            if key.startswith("plan")
            else "\nExit codes: 0 success (including empty matches), 1 failed module checks, 2 invalid input/configuration or I/O failure."
        )
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
    performances = add(subs, "performance", "Analyze explicitly supplied performance evidence offline.")
    psubs = performances.add_subparsers(dest="action")
    database = add(
        psubs, "database", "Analyze a versioned database capture without connecting to MySQL.", "performance"
    )
    database.add_argument(
        "--input", required=True, help="Project-relative version 1.0 JSON capture; ignore rules still apply"
    )
    database.add_argument(
        "--strict",
        action="store_true",
        help="Exit 1 on incomplete evidence/coverage; findings alone are not failures",
    )
    for action, description in (
        ("runtime", "Analyze V8 CPU profiles and exported Node event-loop/GC measurements."),
        ("memory", "Analyze process-memory samples and bounded V8 heap retention graphs."),
        ("cache", "Analyze exported cache counters, timings, capacity and growth."),
    ):
        command = add(psubs, action, description, "performance")
        command.add_argument(
            "--input",
            required=True,
            help="Project-relative capture 2.0 JSON; runtime/memory also accept native .cpuprofile/.heapsnapshot",
        )
        command.add_argument(
            "--strict", action="store_true", help="Exit 1 when supplied evidence or coverage is incomplete"
        )
    comparison = add(
        psubs,
        "compare",
        "Compare compatible captures and report measured regression candidates.",
        "performance",
    )
    comparison.add_argument("--before", required=True, help="Project-relative baseline capture")
    comparison.add_argument(
        "--after", required=True, help="Project-relative candidate capture of the same kind"
    )
    comparison.add_argument(
        "--regression-percent",
        type=regression_percent,
        default=10.0,
        help="Relative worsening threshold (default: 10); zero baselines are reported explicitly",
    )
    comparison.add_argument(
        "--fail-on-regression",
        action="store_true",
        help="Exit 1 for comparable regression candidates crossing the threshold",
    )
    comparison.add_argument(
        "--strict",
        action="store_true",
        help="Exit 1 for incomplete evidence, incompatibility or unmatched metrics",
    )
    add(subs, "doctor", "Diagnose this installation and project without writing or scanning source.")
    configuration = add(
        subs, "config", "Inspect and validate effective configuration without initializing files."
    )
    csubs = configuration.add_subparsers(dest="action")
    add(csubs, "inspect", "Show merged settings, their origins, ignore rules and storage paths.", "config")
    add(csubs, "validate", "Check manifest structure and configured paths/ignore rules read-only.", "config")
    audits = add(
        subs, "audit", "Inspect security contracts and code-quality candidates without editing source."
    )
    asubs = audits.add_subparsers(dest="action")
    for action, description in (
        ("routes", "Check configured Express authentication, roles and permissions in middleware order."),
        ("isolation", "Check configured organization scope on direct Prisma calls."),
        ("security", "Scan JS/TS security candidates with optional Semgrep Community."),
        ("dependencies", "Check JS lockfiles for known advisories with optional OSV-Scanner."),
        ("unused", "Find unused-code candidates with optional Knip and TypeScript-aware ESLint."),
        ("duplicates", "Find token clone candidates with optional jscpd."),
    ):
        audit_parser = add(asubs, action, description, "audit")
        audit_parser.add_argument(
            "path",
            nargs="?",
            default=".",
            help="Project-relative file/directory; routes resolve context across the selected app",
        )
        audit_parser.add_argument(
            "--fail-on",
            choices=["low", "medium", "high", "critical", "none"],
            default="none" if action in {"unused", "duplicates"} else "high",
            help="Finding severity gate; policy violations always exit 1. Unknown checks appear in coverage",
        )
        audit_parser.add_argument(
            "--strict",
            action="store_true",
            help="Also exit 1 when audit coverage is partial/not analyzed or checks are unknown",
        )
        if action in {"routes", "isolation", "unused"}:
            audit_parser.add_argument(
                "--policy", help="Project-relative audit YAML (default: audit.yaml beside Devkit .ignore)"
            )
        if action not in {"routes", "isolation"}:
            audit_parser.add_argument(
                "--timeout",
                type=bounded_int(1, 600),
                default=120,
                help="Timeout per external engine in seconds (default: 120)",
            )
        if action == "dependencies":
            audit_parser.add_argument(
                "--offline",
                action="store_true",
                help="Use a pre-populated OSV local advisory database without network",
            )
            audit_parser.add_argument(
                "--offline-db", help="OSV local database directory (requires --offline)"
            )
        if action == "unused":
            audit_parser.add_argument(
                "--entry",
                action="append",
                default=[],
                help="Explicit project-relative source entry file, repeatable; overrides unused.entry",
            )
        if action == "duplicates":
            audit_parser.add_argument(
                "--min-lines",
                type=bounded_int(1, 10000),
                default=5,
                help="Minimum clone line count (default: 5)",
            )
            audit_parser.add_argument(
                "--min-tokens",
                type=bounded_int(5, 100000),
                default=50,
                help="Minimum token count (default: 50)",
            )
    search = add(subs, "search", "Search text contents; literal smart-case matching by default.")
    search.add_argument("query", help="Literal query, or a regular expression with --regex")
    search.add_argument("path", nargs="?", default=".", help="Directory/file relative to project root")
    search.add_argument("--regex", action="store_true", help="Interpret query as a regular expression")
    search.add_argument(
        "--timings",
        action="store_true",
        help="Report measured setup/discovery/read/match timings and bytes read; no query contents",
    )
    search.add_argument(
        "--context", type=bounded_int(0, 100), default=0, help="Surrounding lines (default: 0)"
    )
    search.add_argument(
        "--glob", action="append", default=[], help="Restrict paths by glob; repeat for alternatives"
    )
    find = add(subs, "find", "Find paths by smart-case substring or regular expression.")
    find.add_argument(
        "pattern", help="Literal path substring (files and directories), not a glob; or --regex"
    )
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
    query = add(subs, "query", "Find structural code patterns using optional ast-grep 0.50.x.")
    query.add_argument(
        "pattern", help="Code pattern; $NAME captures one node, $$$NAME captures multiple nodes"
    )
    query.add_argument("path", nargs="?", default=".", help="Directory/file relative to project root")
    query.add_argument(
        "--lang",
        choices=["auto", "js", "jsx", "ts", "tsx"],
        default="auto",
        help="Grammar/file scope (default: auto by extension)",
    )
    query.add_argument(
        "--timeout", type=bounded_int(1, 300), default=30, help="Seconds per ast-grep batch (default: 30)"
    )
    analyze = add(subs, "analyze", "Brief one JS/TS/JSX/TSX file with numbered source and lexical structure.")
    analyze.add_argument("path", help="Single source file relative to project root")
    analyze.add_argument(
        "--symbol",
        help="Exact case-sensitive name, qualified name or file-local symbol ID; ambiguous matches fail",
    )
    analyze.add_argument(
        "--with-context",
        action="store_true",
        help="Opt in to selected-app-wide import/consumer, module and schema context (file-level)",
    )
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
    feedback = add(subs, "feedback", "Save and review local Devkit feedback; never upload reports.")
    fsubs = feedback.add_subparsers(dest="action")
    report = add(
        fsubs,
        "add",
        "Save a sanitized report; exact repeats reuse an ID and increment occurrences.",
        "feedback",
    )
    report.add_argument("--type", choices=TYPES, required=True, help="Feedback category")
    report.add_argument("--title", required=True, help="Short summary (maximum 200 characters)")
    report.add_argument("--severity", choices=SEVERITIES, default="medium", help="Impact (default: medium)")
    report.add_argument(
        "--evidence",
        choices=EVIDENCE,
        default="suspected",
        help="Agent assessment, not independent verification (default: suspected)",
    )
    report.add_argument("--command", dest="reported_command", help="Affected command as text; never executed")
    report.add_argument("--description", help="Context or requested capability")
    report.add_argument("--expected", help="Expected behavior")
    report.add_argument("--actual", help="Observed behavior or error")
    report.add_argument(
        "--step", action="append", default=[], help="Reproduction step as text; repeatable, maximum 20"
    )
    report.add_argument(
        "--diagnostics", help="Small, manually sanitized excerpt; never paste secrets or customer data"
    )
    listing = add(fsubs, "list", "List local reports, newest update first.", "feedback")
    for name, choices in (
        ("type", TYPES),
        ("severity", SEVERITIES),
        ("evidence", EVIDENCE),
        ("status", STATUSES),
    ):
        listing.add_argument("--" + name, choices=choices, help="Filter by " + name)
    listing.add_argument(
        "--offset", type=bounded_int(0, 10000000), default=0, help="Pagination offset (default: 0)"
    )
    inspection = add(fsubs, "inspect", "Read one complete local report by its stable ID.", "feedback")
    inspection.add_argument("report_id", help="ID returned by feedback add/list")
    update = add(
        fsubs,
        "update",
        "Record triage changes or a note; resolving requires a verification note.",
        "feedback",
    )
    update.add_argument("report_id", help="ID returned by feedback add/list")
    for name, choices in (("status", STATUSES), ("severity", SEVERITIES), ("evidence", EVIDENCE)):
        update.add_argument("--" + name, choices=choices, help="New " + name)
    update.add_argument("--note", help="Explanation or verification evidence; required for resolved status")
    plans = add(subs, "plan", "Archive Antigravity implementation plans and tasks locally.")
    plan_subs = plans.add_subparsers(dest="action")
    archive = add(
        plan_subs,
        "archive",
        "Save numbered plan/task copies, verify both, then clear their original contents.",
        "plan",
    )
    archive.add_argument("conversation_id", help="Antigravity brain conversation folder ID, not a path")
    archive.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview paths, validation and index without writing or clearing files",
    )
    agents = add(subs, "agents", "Synchronize bundled Devkit guidance for coding agents.")
    asubs = agents.add_subparsers(dest="action")
    sync = add(
        asubs, "sync", "Sync the dedicated rule/skill and only the Devkit block in AGENTS.md.", "agents"
    )
    mode = sync.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview file actions and managed-content diffs without writing",
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="Read-only check: exit 0 when current, 1 when sync/conflict needs attention",
    )
    sync.add_argument(
        "--force",
        action="store_true",
        help="Explicitly replace unmanaged/local edits with backups; cannot override unsafe paths or ambiguous AGENTS.md boundaries",
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
                "scope": "Read-only configuration/install inspection; selected app paths, global manifest structure"
                if name == "doctor" or name.startswith("config")
                else "Project-wide fixed agent files under --root; ignores app/config/cache selection"
                if name.startswith("agents")
                else "Current user's .gemini/antigravity-ide/brain/<conversation-id>/implementation_plan.md and task.md; local numbered archives under --root/.agents/implementation/trash; apply clears both originals"
                if name.startswith("plan")
                else "Local feedback only; YAML/source are not inspected"
                if name.startswith("feedback")
                else "Selected app and project-local .ignore; analysis reports measured coverage, not runtime completeness. Help reads no project files",
                "exit_codes": {
                    "0": "No finding gate triggered; inspect coverage and unknown checks"
                    if name.startswith("audit")
                    else "Report generated; inspect findings/unknowns and coverage"
                    if name.startswith("performance")
                    else "Archived/already archived or valid preview"
                    if name.startswith("plan")
                    else "Success (including no matches)",
                    "1": "Policy violation, finding at the selected severity threshold, or incomplete --strict audit"
                    if name.startswith("audit")
                    else "Incomplete --strict performance analysis or comparison --fail-on-regression gate"
                    if name.startswith("performance")
                    else "Diagnostic checks failed (doctor/config validate)"
                    if name == "doctor" or name.startswith("config")
                    else "--check detected drift/conflicts"
                    if name.startswith("agents")
                    else "Unused"
                    if name.startswith("plan")
                    else "Module check failures",
                    "2": "Unsafe/conflicting sync or invalid input/storage"
                    if name.startswith("agents")
                    else "Invalid/unsafe input, copy/verification failure or incomplete clearing; inspect per-file status"
                    if name.startswith("plan")
                    else "Invalid input, configuration or I/O failure",
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
    project.coverage.start("text")
    selected = []
    timings = {"discovery_ms": 0.0, "read_ms": 0.0, "match_ms": 0.0, "bytes_read": 0}
    measured = getattr(args, "timings", False)
    started = time.perf_counter() if measured else 0
    for path in project.entries(args.path):
        if args.glob and not any(
            fnmatch.fnmatchcase(project.rel(path), g) or fnmatch.fnmatchcase(path.name, g) for g in args.glob
        ):
            project.coverage.exclude("glob_filter", project.rel(path))
        else:
            selected.append(path)
    project.coverage.details["search_candidate_files"] = len(selected)
    if measured:
        timings["discovery_ms"] = (time.perf_counter() - started) * 1000
        project.coverage.details.setdefault("search_timings", {}).update(timings)
        timings = project.coverage.details["search_timings"]
    # Stop after one extra result: that is sufficient to report truncation honestly.
    for path in selected:
        relative = project.rel(path)
        started = time.perf_counter() if measured else 0
        content = project.text(path, cache=False, stats=timings if measured else None)
        if measured:
            timings["read_ms"] += (time.perf_counter() - started) * 1000
        if content is None:
            continue
        started = time.perf_counter() if measured else 0
        # Literal matches cannot depend on anchors/lookarounds. A whole-file
        # rejection avoids splitting and one Python regex call per line, while
        # preserving Python's exact Unicode IGNORECASE semantics. Regex queries
        # retain their original per-line semantics, including empty matches.
        if not args.regex and (
            (
                args.query.lower() not in content.lower()
                if pattern.flags & re.IGNORECASE
                else args.query not in content
            )
            if args.query.isascii() and content.isascii()
            else pattern.search(content) is None
        ):
            if measured:
                timings["match_ms"] += (time.perf_counter() - started) * 1000
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
                    if measured:
                        timings["match_ms"] += (time.perf_counter() - started) * 1000
                    project.coverage.gap("search_result_limit", relative, stage="text", line=index + 1)
                    return result
        if measured:
            timings["match_ms"] += (time.perf_counter() - started) * 1000
    project.coverage.finish("text")
    return result


def find(project: Project, args: argparse.Namespace) -> list[dict[str, Any]]:
    pattern = matcher(args.pattern, args.regex)
    paths = []
    if args.type != "dir":
        paths.extend(project.entries(args.path))
    if args.type != "file" and not args.extension:
        paths.extend(project.entries(args.path, directories=True))
    extensions = {"." + ext.lstrip(".").casefold() for ext in args.extension}
    result = [
        {"path": project.rel(p), "type": "dir" if p.is_dir() else "file"}
        for p in sorted(paths)
        if pattern.search(project.rel(p)) and (not extensions or p.suffix.casefold() in extensions)
    ]
    if not result and not args.regex and any(char in args.pattern for char in "*?"):
        project.warnings.append(
            "find matches literal path substrings, not globs; wildcard characters are literal. "
            "For paths containing customers, use 'devkit find customers'; "
            "add --type dir for directories, or use --regex for regular expressions."
        )
    return result


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


def execute(
    project: Project | FeedbackContext | AgentsContext | DiagnosticContext, args: argparse.Namespace
) -> tuple[Any, int]:
    command, action = args.command, getattr(args, "action", None)
    if command == "performance":
        from .performance import performance

        return performance(project, args)
    if command == "audit":
        from .audit import audit

        return audit(project, args)
    if command in {"doctor", "config"}:
        diagnostics = Diagnostics(project)
        return (
            diagnostics.inspect() if action == "inspect" else diagnostics.validate(doctor=command == "doctor")
        )
    if command == "agents":
        return AgentSync(project).run(args)
    if command == "plan":
        return PlanArchive(project, args.conversation_id).run(dry_run=args.dry_run)
    if command == "feedback":
        store = FeedbackStore(project)
        if action == "inspect":
            return store.inspect(args.report_id), 0
        return {"add": store.add, "list": store.list, "update": store.update}[action](args), 0
    if command == "search":
        return search(project, args), 0
    if command == "find":
        return find(project, args), 0
    if command == "tree":
        return tree(project, args), 0
    if command == "analyze":
        from .analyze import analyze

        return analyze(project, args), 0
    if command == "query":
        from .query import query

        return query(project, args), 0
    if command == "symbols":
        from .source import SourceIndex, match_name

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
        from .modules import Modules

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
    from .analysis import Analysis

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
            if (
                isinstance(row, dict)
                and {"path", "line", "text"} <= row.keys()
                and row.get("evidence") != "structural_match"
            ):
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
    coverage = result.get("coverage")
    if coverage:
        coverage["output_truncated"] = True
        if coverage.pop("details", None) is not None:
            coverage["details_truncated"] = True
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

        if "data" in result:
            visit(result["data"], result, "data")
        if "error" in result:
            visit(result["error"], result, "error")
        visit(result.get("warnings", []), result, "warnings")
        if not candidates:
            # A very wide scalar mapping cannot be pruned safely field-by-field.
            summary = result["data"].get("summary") if isinstance(result.get("data"), dict) else None
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
    entered = time.perf_counter()
    argv = list(sys.argv[1:] if argv is None else argv)
    json_mode = "--json" in argv
    # JSON escapes preserve source text on legacy Windows/ASCII output streams.
    encoding = (getattr(sys.stdout, "encoding", None) or "utf-8").lower().replace("-", "")
    json_ascii = encoding not in {"utf8", "utf8sig"}
    command = ""
    coverage = None
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
            or (
                args.command
                in {
                    "module",
                    "schema",
                    "trace",
                    "impact",
                    "feedback",
                    "agents",
                    "config",
                    "audit",
                    "performance",
                    "plan",
                }
                and not args.action
            )
        ):
            topic = " ".join(args.topic) if args.command == "help" else (args.command or "")
            data = catalog(registry, topic)
            if args.json:
                print(
                    json.dumps(
                        {"schema_version": "1.0", "command": "help", "data": data}, ensure_ascii=json_ascii
                    )
                )
            else:
                print(registry[topic].format_help())
                if not topic:
                    for row in data:
                        print(f"{row['command']:<30} {row['description']}")
            return 0
        context = {
            "feedback": FeedbackContext,
            "agents": AgentsContext,
            "plan": PlanContext,
            "doctor": DiagnosticContext,
            "config": DiagnosticContext,
        }.get(args.command, Project)
        if context is Project:
            coverage = Coverage()
            project = Project(args.root, args.config, args.app, coverage=coverage)
            coverage.configure(project, args)
        else:
            project = context(args.root, args.config, args.app)
        project.use_cache = not getattr(args, "no_cache", False)
        if args.command == "search" and args.timings:
            coverage.details["search_timings"] = {"setup_ms": (time.perf_counter() - entered) * 1000}
        data, status = execute(project, args)
        if args.command == "search" and args.timings:
            coverage.details["search_timings"]["through_search_ms"] = (time.perf_counter() - entered) * 1000
        original_analysis = data if args.command == "analyze" else None
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
        if coverage:
            measured = coverage.report()
            measured["details"], omitted = limit_data(
                measured["details"], args.max_results, "coverage.details"
            )
            measured["details_truncated"] = bool(omitted)
            result["truncation"].extend(omitted)
            result["truncated"] = bool(result["truncation"])
            measured["output_truncated"] = result["truncated"]
            result["coverage"] = measured
        maximum = getattr(args, "max_output_chars", 32000)

        def output():
            if original_analysis is not None:
                from .analyze import refresh_display_summary

                refresh_display_summary(result["data"], original_analysis)
            return (
                json.dumps(result, ensure_ascii=json_ascii)
                if args.json
                else "\n".join(
                    render_tree(result["data"]) if args.command == "tree" else render(result["data"])
                )
                + "\n"
                + "\n".join("Warning: " + w for w in result["warnings"])
                + ("\n" + coverage_line(result["coverage"]) if coverage else "")
                + (
                    "\nSearch timings: " + json.dumps(coverage.details["search_timings"])
                    if args.command == "search" and args.timings
                    else ""
                )
                + (
                    "\nResults truncated; increase --max-results / --max-output-chars or narrow the query."
                    if result["truncated"]
                    else ""
                )
            )

        bound_output(result, maximum, output)
        if original_analysis is not None:
            from .analyze import refresh_display_summary

            refresh_display_summary(result["data"], original_analysis)
        if args.command == "tree" and any(
            t.get("reason") == "total_output_budget" for t in result["truncation"]
        ):

            def count_nodes(node):
                return sum(1 + count_nodes(child) for child in node.get("children", []))

            result["data"]["shown_nodes"] = count_nodes(result["data"])
            result["data"]["truncated"] = True
        if args.json:
            print(json.dumps(result, ensure_ascii=json_ascii))
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
            if coverage:
                print(coverage_line(result["coverage"]), file=sys.stderr)
            if args.command == "search" and args.timings:
                print("Search timings: " + json.dumps(coverage.details["search_timings"]), file=sys.stderr)
        return status
    except BrokenPipeError:
        return 0
    except (DevkitError, OSError, ValueError, ImportError) as exc:
        measured = None
        if coverage:
            coverage.aborted = True
            coverage.gap("analysis_aborted", stage="discovery")
            measured = coverage.report()
        if json_mode:
            result = {"schema_version": "1.0", "command": command, "error": {"message": str(exc), "code": 2}}
            if measured:
                measured["details"], omitted = limit_data(
                    measured["details"], args.max_results, "coverage.details"
                )
                measured["details_truncated"] = measured["output_truncated"] = bool(omitted)
                result.update(coverage=measured, truncated=bool(omitted), truncation=omitted)
                bound_output(result, getattr(args, "max_output_chars", 32000), lambda: json.dumps(result))
            print(json.dumps(result))
        else:
            print(f"devkit: {exc}", file=sys.stderr)
            if measured:
                print(coverage_line(measured), file=sys.stderr)
        return 2
