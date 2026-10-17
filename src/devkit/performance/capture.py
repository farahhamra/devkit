"""Bounded, versioned performance evidence. Never execute or echo raw SQL/parameters."""

from __future__ import annotations

import json
import math
import re
import stat
from datetime import datetime
from pathlib import PurePosixPath, PureWindowsPath

from ..project import DevkitError

MAX_BYTES = 16 * 1024 * 1024
MAX_NODES = 250000
MAX_ROWS = 20000
IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_$.-]{0,127}"
TOKEN = r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}"
HASH = r"[a-f0-9]{64}"
OPERATIONS = {
    "select",
    "insert",
    "update",
    "delete",
    "begin",
    "commit",
    "rollback",
    "other",
    "findUnique",
    "findUniqueOrThrow",
    "findFirst",
    "findFirstOrThrow",
    "findMany",
    "create",
    "createMany",
    "updateMany",
    "deleteMany",
    "upsert",
    "count",
    "aggregate",
    "groupBy",
    "executeRaw",
    "queryRaw",
    "transaction",
}


def invalid(where, message):
    # 'where' is constructed from schema keys/array offsets, never submitted keys/values.
    raise DevkitError(f"Invalid performance capture at {where}: {message}")


def obj(value, where, required=(), optional=()):
    if not isinstance(value, dict):
        invalid(where, "expected an object")
    if not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        invalid(where, "missing required or unsupported fields; see docs/guides/PERFORMANCE.md")
    return value


def string(value, where, pattern=TOKEN):
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        invalid(where, "invalid identifier/format (submitted value omitted)")
    return value


def choice(value, where, choices):
    if not isinstance(value, str) or value not in choices:
        invalid(where, "unsupported value (submitted value omitted)")
    return value


def number(value, where, *, integer=False, minimum=0, maximum=1e15):
    if (
        type(value) not in ({int} if integer else {int, float})
        or not minimum <= value <= maximum
        or not math.isfinite(value)
    ):
        invalid(where, "expected a finite, bounded nonnegative number" if not integer else "invalid integer")
    return value


def boolean(value, where):
    if type(value) is not bool:
        invalid(where, "expected a boolean")
    return value


def rows(value, where):
    if not isinstance(value, list) or len(value) > MAX_ROWS:
        invalid(where, f"expected an array with at most {MAX_ROWS} records")
    return value


def source(value, where):
    obj(value, where, ("path", "sha256", "line", "end_line"))
    raw = value["path"]
    if not isinstance(raw, str) or len(raw) > 1024 or any(ord(c) < 32 for c in raw):
        invalid(where, "invalid source path")
    path = raw.replace("\\", "/")
    if (
        PureWindowsPath(raw).drive
        or PurePosixPath(path).is_absolute()
        or ".." in path.split("/")
        or ":" in path
        or not path
        or path.endswith("/")
    ):
        invalid(where, "source paths must be project-relative, without traversal")
    value["path"] = path
    string(value["sha256"], where + ".sha256", HASH)
    number(value["line"], where + ".line", integer=True, minimum=1, maximum=10000000)
    number(value["end_line"], where + ".end_line", integer=True, minimum=value["line"], maximum=10000000)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            invalid("JSON", "duplicate object keys")
        result[key] = value
    return result


def _constant(_):
    invalid("JSON", "nonfinite numeric constants are not supported")


def preflight(payload, maximum_nodes=None):
    """Reject huge container graphs before json.loads allocates them.

    For valid JSON, structural punctuation is at most twice the value-node
    count. Quoted/escaped punctuation does not count. Exact nodes are checked
    after decoding too; this is not a replacement JSON parser.
    """
    maximum_nodes = MAX_NODES if maximum_nodes is None else maximum_nodes
    quoted, escaped, depth, structure = False, False, 0, 0
    for byte in payload:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
        elif byte in (123, 91):
            depth += 1
            structure += 1
        elif byte in (125, 93):
            depth -= 1
        elif byte in (44, 58):
            structure += 1
        if structure > maximum_nodes * 2 or depth > 65:
            invalid("JSON", "structure exceeds the node/depth limit")


def eligible_path(project, raw, *, app=None):
    """Explicit evidence does not bypass ignored parents, symlinks or app source scope."""
    path = project.resolve(raw)
    for entry in (path, *path.parents):
        if entry == project.root:
            break
        if not project.allowed(entry, entry.is_dir()):
            raise DevkitError("Performance input/source is excluded by Devkit's ignore/storage policy")
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise DevkitError("Performance input/source must be a regular non-symlink file")
    if app and project.app_for(path) != app:
        raise DevkitError("Performance source is outside the capture's app scope")
    return path


def load_json(project, raw, *, suffixes=(".json",), maximum_bytes=None, maximum_nodes=None):
    maximum_bytes = MAX_BYTES if maximum_bytes is None else maximum_bytes
    maximum_nodes = MAX_NODES if maximum_nodes is None else maximum_nodes
    path = eligible_path(project, raw)
    if path.suffix.lower() not in suffixes:
        raise DevkitError("Unsupported performance input extension; see devkit help performance")
    if path.stat().st_size > maximum_bytes:
        raise DevkitError(f"Performance capture exceeds the {maximum_bytes // (1024 * 1024)} MiB input limit")
    with path.open("rb") as handle:
        payload = handle.read(maximum_bytes + 1)
    if len(payload) > maximum_bytes:
        raise DevkitError(f"Performance capture exceeds the {maximum_bytes // (1024 * 1024)} MiB input limit")
    preflight(payload, maximum_nodes)
    try:
        data = json.loads(payload.decode("utf-8-sig"), object_pairs_hook=_unique, parse_constant=_constant)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise DevkitError("Performance capture must contain valid UTF-8 JSON (contents omitted)") from exc
    stack, visited = [iter((data,))], 0
    while stack:
        value = next(stack[-1], StopIteration)
        if value is StopIteration:
            stack.pop()
            continue
        visited += 1
        if visited > maximum_nodes or len(stack) > 65:
            invalid("JSON", "structure exceeds the node/depth limit")
        if isinstance(value, dict):
            stack.append(iter(value.values()))
        elif isinstance(value, list):
            stack.append(iter(value))
        elif isinstance(value, float) and not math.isfinite(value):
            invalid("JSON", "nonfinite numeric values are not supported")
    return data, project.rel(path)


def load(project, raw):
    data, path = load_json(project, raw)
    validate(data)
    if project.app not in {"all", data["metadata"]["app"]}:
        raise DevkitError("Capture app does not match --app; captures are analyzed one app at a time")
    return data, path


def validate(data):
    obj(
        data,
        "capture",
        ("capture_version", "metadata", "queries"),
        (
            "requests",
            "indexes",
            "indexes_complete",
            *(("collection",) if isinstance(data, dict) and data.get("capture_version") == "1.1" else ()),
        ),
    )
    choice(data["capture_version"], "capture_version", {"1.0", "1.1"})
    meta = obj(
        data["metadata"],
        "metadata",
        ("app", "captured_at", "database", "workload"),
        ("source_revision", "runtime", "environment", "process_scope", "collector"),
    )
    choice(meta["app"], "metadata.app", {"erp", "hub"})
    for name in ("environment", "process_scope", "collector"):
        if name in meta:
            string(meta[name], "metadata." + name)
    try:
        timestamp = datetime.fromisoformat(meta["captured_at"])
        if timestamp.utcoffset() is None:
            raise ValueError()
    except (AttributeError, TypeError, ValueError):
        invalid("metadata.captured_at", "expected an ISO timestamp with timezone")
    db = obj(meta["database"], "metadata.database", ("engine", "version"))
    choice(db["engine"], "metadata.database.engine", {"mysql", "mariadb", "unknown"})
    if db["version"] is not None:
        string(db["version"], "metadata.database.version", r"[0-9][A-Za-z0-9_.+-]{0,63}")
    workload = obj(
        meta["workload"], "metadata.workload", ("name", "duration_ms"), ("concurrency", "warmup_ms")
    )
    string(workload["name"], "metadata.workload.name")
    for name in ("duration_ms", "concurrency", "warmup_ms"):
        if workload.get(name) is not None:
            number(
                workload[name],
                "metadata.workload." + name,
                integer=name == "concurrency",
                minimum=1 if name == "concurrency" else 0,
            )
    if meta.get("source_revision") is not None:
        string(meta["source_revision"], "metadata.source_revision", r"[a-f0-9]{40}|[a-f0-9]{64}")
    runtime = obj(meta.get("runtime", {}), "metadata.runtime", (), ("node", "prisma_client"))
    for name, version in runtime.items():
        string(version, "metadata.runtime." + name, r"[0-9][A-Za-z0-9_.+-]{0,63}")
    if data["capture_version"] == "1.1":
        if "collection" not in data:
            invalid("capture", "collection is required for version 1.1")
        from .collection import validate_collection

        validate_collection(data)
    requests = {}
    for i, request in enumerate(rows(data.get("requests", []), "requests")):
        where = f"requests[{i}]"
        obj(request, where, ("id", "duration_ms", "outcome"), ("route_template",))
        string(request["id"], where + ".id")
        number(request["duration_ms"], where + ".duration_ms")
        choice(request["outcome"], where + ".outcome", {"finished", "aborted"})
        if "route_template" in request:
            string(request["route_template"], where + ".route_template", r"/[A-Za-z0-9_/:.*{}-]{0,255}")
        if request["id"] in requests:
            invalid(where, "duplicate request ID")
        requests[request["id"]] = request
    ids, fingerprints, events = set(), set(), {}
    for i, query in enumerate(rows(data["queries"], "queries")):
        where = f"queries[{i}]"
        obj(
            query,
            where,
            ("id", "kind", "fingerprint", "operation", "tables", "timing"),
            ("model", "samples", "plan", "source"),
        )
        string(query["id"], where + ".id")
        if query["id"] in ids:
            invalid(where, "duplicate query ID")
        ids.add(query["id"])
        choice(query["kind"], where + ".kind", {"sql", "prisma"})
        string(query["fingerprint"], where + ".fingerprint", HASH)
        choice(query["operation"], where + ".operation", OPERATIONS)
        for table in rows(query["tables"], where + ".tables"):
            string(table, where + ".tables[]", IDENTIFIER)
        query_tables = set(query["tables"])
        if len(query["tables"]) != len(query_tables):
            invalid(where, "duplicate tables")
        if "model" in query:
            string(query["model"], where + ".model", IDENTIFIER)
        timing = obj(
            query["timing"],
            where + ".timing",
            ("source", "count", "total_ms"),
            ("min_ms", "max_ms", "rows_examined", "rows_sent"),
        )
        choice(
            timing["source"],
            where + ".timing.source",
            {"prisma_operation", "unknown"}
            if query["kind"] == "prisma"
            else {"mysql_digest", "mysql_slow_log", "prisma_query_event", "unknown"},
        )
        identity = (query["kind"], timing["source"], query["fingerprint"])
        if identity in fingerprints:
            invalid(
                where, "duplicate fingerprint within one measurement stream; merge disjoint records first"
            )
        fingerprints.add(identity)
        count = number(timing["count"], where + ".timing.count", integer=True, minimum=1, maximum=1000000000)
        for key in ("total_ms", "min_ms", "max_ms", "rows_examined", "rows_sent"):
            if timing.get(key) is not None:
                number(timing[key], where + ".timing." + key, integer=key.startswith("rows_"))
        total, low, high = (timing.get(k) for k in ("total_ms", "min_ms", "max_ms"))
        if low is not None and high is not None and low > high:
            invalid(where, "min_ms exceeds max_ms")
        if total is not None and (
            (low is not None and low * count > total + 1e-6)
            or (high is not None and high * count < total - 1e-6)
            or (high is not None and high > total + 1e-6)
        ):
            invalid(where, "timing bounds disagree with count/total_ms")
        samples = rows(query.get("samples", []), where + ".samples")
        if len(samples) > count:
            invalid(where, "sample count exceeds timing.count")
        sample_total = 0
        for j, event in enumerate(samples):
            ew = where + f".samples[{j}]"
            obj(event, ew, ("id", "duration_ms", "phase", "correlation"), ("request_id", "parent_id"))
            string(event["id"], ew + ".id")
            if event["id"] in events:
                invalid(ew, "duplicate event ID")
            events[event["id"]] = event
            sample_total += number(event["duration_ms"], ew + ".duration_ms")
            choice(event["phase"], ew + ".phase", {"request", "detached", "background", "unknown"})
            choice(event["correlation"], ew + ".correlation", {"explicit", "unknown"})
            for key in ("request_id", "parent_id"):
                if event.get(key) is not None:
                    string(event[key], ew + "." + key)
            if (low is not None and event["duration_ms"] < low) or (
                high is not None and event["duration_ms"] > high
            ):
                invalid(ew, "sample falls outside reported timing bounds")
        if total is not None and (
            sample_total > total + 1e-6
            or (len(samples) == count and not math.isclose(sample_total, total, abs_tol=1e-6))
        ):
            invalid(where, "sample durations disagree with total_ms")
        if "source" in query:
            source(query["source"], where + ".source")
        if "plan" in query:
            plan = obj(query["plan"], where + ".plan", ("format", "document"), ("table_map",))
            string(plan["format"], where + ".plan.format")
            # Native EXPLAIN documents contain free text. Only selected numeric/identifier fields
            # are projected by the analyzer; raw conditions, SQL, costs and literals never echo.
            if not isinstance(plan["document"], dict):
                invalid(where + ".plan.document", "expected an object")
            mapping = plan.get("table_map", {})
            if not isinstance(mapping, dict):
                invalid(where + ".plan.table_map", "expected a mapping")
            for alias, table in mapping.items():
                string(alias, where + ".plan.table_map alias", IDENTIFIER)
                if not isinstance(table, str) or table not in query_tables:
                    invalid(where + ".plan.table_map", "mapped table must occur in query.tables")
    # Parent links may span query groups. Validate the graph in O(events), not by
    # walking every ancestor chain independently. Missing links remain coverage gaps.
    done = set()
    for start in events:
        trail, node = set(), start
        while node in events and node not in done:
            if node in trail:
                invalid("queries.samples", "cyclic parent links")
            trail.add(node)
            event = events[node]
            parent = events.get(event.get("parent_id"))
            if (
                parent
                and event.get("request_id")
                and parent.get("request_id")
                and event["request_id"] != parent["request_id"]
            ):
                invalid("queries.samples", "parent and child request IDs disagree")
            node = event.get("parent_id")
        done.update(trail)
    boolean(data.get("indexes_complete", False), "indexes_complete")
    index_ids = set()
    for i, index in enumerate(rows(data.get("indexes", []), "indexes")):
        where = f"indexes[{i}]"
        obj(
            index,
            where,
            ("table", "name", "provenance", "unique", "primary", "parts"),
            ("constraint", "visible", "method", "source"),
        )
        for key in ("table", "name"):
            string(index[key], where + "." + key, IDENTIFIER)
        choice(index["provenance"], where + ".provenance", {"database", "migration"})
        identity = (index["table"], index["name"], index["provenance"])
        if identity in index_ids:
            invalid(where, "duplicate index identity")
        index_ids.add(identity)
        for key in ("unique", "primary", "constraint", "visible"):
            if key in {"unique", "primary"} or index.get(key) is not None:
                boolean(index[key], where + "." + key)
        if index["primary"] and not index["unique"]:
            invalid(where, "primary indexes must be unique")
        choice(
            index.get("method", "unknown"),
            where + ".method",
            {"btree", "hash", "fulltext", "spatial", "unknown"},
        )
        parts = rows(index["parts"], where + ".parts")
        if not parts:
            invalid(where, "index parts cannot be empty")
        for part in parts:
            obj(part, where + ".parts[]", (), ("column", "expression", "order", "prefix_length"))
            if ("column" in part) == ("expression" in part):
                invalid(where, "each index part must contain column OR expression")
            if "column" in part:
                string(part["column"], where + ".parts[].column", IDENTIFIER)
            elif part["expression"] is not True:
                invalid(where, "expression must be true; do not include expression text")
            if "order" in part:
                choice(part["order"], where + ".parts[].order", {"asc", "desc"})
            if part.get("prefix_length") is not None:
                number(
                    part["prefix_length"],
                    where + ".parts[].prefix_length",
                    integer=True,
                    minimum=1,
                    maximum=65535,
                )
        if "source" in index:
            source(index["source"], where + ".source")
