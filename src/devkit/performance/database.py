"""Offline evidence correlation; no database driver, subprocess or source execution."""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter, defaultdict

from ..modules import Modules
from ..project import DevkitError
from ..schema import Schema, attribute, tokens
from .capture import IDENTIFIER, eligible_path

MAX_SOURCE_BYTES = 4 * 1024 * 1024
MAX_SOURCE_TOTAL = 16 * 1024 * 1024


def location(record):
    return {key: record[key] for key in ("path", "line", "end_line")}


def identifier(value):
    return value if isinstance(value, str) and re.fullmatch(IDENTIFIER, value) else None


def finite(value):
    # Native MySQL JSON commonly represents filtered percentages as decimal strings.
    if isinstance(value, str) and re.fullmatch(r"\d{1,16}(?:\.\d{1,12})?", value):
        value = float(value)
    return value if type(value) in {int, float} and 0 <= value <= 1e15 and math.isfinite(value) else None


class DatabaseAnalysis:
    def __init__(self, project, capture):
        self.project, self.capture = project, capture
        self.coverage = project.coverage
        self.app = capture["metadata"]["app"]
        self.schema = Schema(project)
        self.modules = Modules(project, self.schema)
        self.models = [m for m in self.schema.models if m["app"] == self.app]
        self.by_table = defaultdict(list)
        for model in self.models:
            self.by_table[model["table"]].append(model)
        self.findings = []
        self.unknowns = []
        self._unknown_keys = set()
        self._sources = {}
        self._source_bytes = 0
        self.plan_count = 0
        self.indexes = []
        self.query_sources = {}
        from .collection import report_collection

        report_collection(self, capture)

    def gap(self, code, subject, message):
        if (code, subject) not in self._unknown_keys:
            self._unknown_keys.add((code, subject))
            self.unknowns.append({"code": code, "subject": subject, "message": message})
            self.coverage.gap("performance_" + code, subject, stage="performance")

    def finding(self, rule, message, **details):
        if details.get("query_id") in self.query_sources:
            details["source"] = self.query_sources[details["query_id"]]
        self.findings.append({"rule": rule, "confidence": "candidate", "message": message, **details})

    def source(self, value, subject):
        if value is None:
            self.gap(
                "source_unavailable",
                subject,
                "No source attribution supplied; table/module matches do not locate the executing call.",
            )
            return {"location_status": "unavailable"}
        path = value["path"]
        if path not in self._sources:
            try:
                target = eligible_path(self.project, path, app=self.app)
                size = target.stat().st_size
                if size > MAX_SOURCE_BYTES or size + self._source_bytes > MAX_SOURCE_TOTAL:
                    raise DevkitError("source budget")
                with target.open("rb") as handle:
                    payload = handle.read(min(MAX_SOURCE_BYTES, MAX_SOURCE_TOTAL - self._source_bytes) + 1)
                if len(payload) > MAX_SOURCE_BYTES or len(payload) + self._source_bytes > MAX_SOURCE_TOTAL:
                    raise DevkitError("source budget")
                self._source_bytes += len(payload)
                text = payload.decode("utf-8-sig")
                if "\x00" in text:
                    raise DevkitError("binary source")
                self._sources[path] = (
                    hashlib.sha256(payload).hexdigest(),
                    text.count("\n") + int(bool(text) and not text.endswith("\n")),
                )
            except (DevkitError, OSError, UnicodeError):
                self._sources[path] = None
        state = self._sources[path]
        if state is None or state[0] != value["sha256"] or value["end_line"] > state[1]:
            self.gap(
                "source_unverified",
                subject,
                "Source unavailable/excluded, over budget, stale hash, or invalid line range; supplied lines are not emitted.",
            )
            return {"location_status": "unverified"}
        return {
            **location(value),
            "sha256": value["sha256"],
            "location_status": "content_verified",
            "attribution": "capture_supplied_not_runtime_verified",
        }

    def mappings(self, query):
        result = []
        for table in query["tables"]:
            models = self.by_table.get(table, [])
            if len(models) != 1:
                self.gap(
                    "table_mapping_unresolved",
                    query["id"] + ":" + table,
                    "No unique exact-case Prisma table mapping in the capture app.",
                )
                continue
            model = models[0]
            result.append(
                {
                    "table": table,
                    "model": model["name"],
                    "modules": self.modules.owner(model["name"], self.app),
                    "schema_location": {
                        **location(model),
                        "attribution": "current_schema_not_executing_call",
                    },
                }
            )
        if not query["tables"]:
            self.gap(
                "tables_unavailable",
                query["id"],
                "No physical tables supplied; SQL text is not parsed to guess them.",
            )
        if query.get("model") and not any(m["model"] == query["model"] for m in result):
            self.gap(
                "model_mapping_unresolved",
                query["id"],
                "Reported model does not match the resolved table mappings.",
            )
        return result

    def declared_indexes(self):
        result = []
        wanted = {table for query in self.capture["queries"] for table in query["tables"]}
        wanted.update(index["table"] for index in self.capture.get("indexes", []))
        for model in self.models:
            if model["table"] not in wanted:
                continue
            columns = {f["name"]: f["column"] for f in model["fields"] if f["column"]}
            for item in [*model["indexes"], *model["unique_constraints"]]:
                definition = item["definition"]
                kind = definition.split()[0]
                args = attribute(definition, kind) or ""
                # Top-level field names only: sort/length/operator arguments are not fields.
                field_list = re.search(r"\[([^]]*)\]", args)
                fields, depth, expect = [], 0, True
                for token, _ in tokens(field_list.group(1) if field_list else ""):
                    if token == "(":
                        depth += 1
                    elif token == ")":
                        depth -= 1
                    elif depth == 0 and token == ",":
                        expect = True
                    elif depth == 0 and expect:
                        fields.append(token)
                        expect = False
                result.append(
                    {
                        "table": model["table"],
                        "model": model["name"],
                        "provenance": "prisma",
                        "fields": fields,
                        "columns": [columns.get(f) for f in fields],
                        "unique": kind in {"@@unique", "@@id"},
                        "primary": kind == "@@id",
                        "definition": definition,
                        "source": {
                            "path": model["path"],
                            "line": item["line"],
                            "end_line": item["end_line"],
                            "attribution": "current_schema_not_deployed_index",
                        },
                    }
                )
            for field in model["fields"]:
                if field["id"] or field["unique"]:
                    result.append(
                        {
                            "table": model["table"],
                            "model": model["name"],
                            "provenance": "prisma",
                            "fields": [field["name"]],
                            "columns": [field["column"]],
                            "unique": True,
                            "primary": field["id"],
                            "source": {
                                "path": model["path"],
                                "line": field["line"],
                                "end_line": field["end_line"],
                                "attribution": "current_schema_not_deployed_index",
                            },
                        }
                    )
        return result

    def inventory(self):
        self.indexes = []
        for index in self.capture.get("indexes", []):
            row = {k: v for k, v in index.items() if k != "source"}
            row["verification"] = "capture_reported_not_live_checked"
            if "source" in index or index["provenance"] == "migration":
                row["source"] = self.source(index.get("source"), "index:" + index["name"])
            row["constraint_protected"] = bool(
                index["unique"]
                or index["primary"]
                or index.get("constraint") is not False
                or any(p.get("expression") for p in index["parts"])
            )
            self.indexes.append(row)
        if not self.capture.get("indexes_complete", False):
            self.gap(
                "index_inventory_incomplete",
                "indexes",
                "Database index inventory is not declared complete; absence is not proof of a missing index.",
            )
        # Exact comparable duplicates only, not prefix guesses. Uniqueness, FK support,
        # expressions, unknown method/order/visibility/constraint semantics are protected.
        groups = defaultdict(list)
        for index in self.indexes:
            if (
                index["provenance"] != "database"
                or index["constraint_protected"]
                or index.get("method") != "btree"
                or index.get("visible") is not True
            ):
                continue
            parts = index["parts"]
            if any("column" not in part or "order" not in part for part in parts):
                continue
            signature = (
                index["table"],
                tuple((p["column"], p["order"], p.get("prefix_length")) for p in parts),
            )
            groups[signature].append(index["name"])
        for (table, _), names in groups.items():
            if len(names) > 1:
                self.finding(
                    "equivalent_index_definitions",
                    "Reported non-constraint indexes have equivalent definitions. Check workload and constraint dependencies; no drop recommendation is made.",
                    table=table,
                    indexes=names,
                )
        return {
            "database_inventory_declared_complete": self.capture.get("indexes_complete", False),
            "captured": self.indexes,
            "prisma_declared": self.declared_indexes(),
        }

    def plan(self, query):
        plan = query.get("plan")
        if plan is None:
            self.gap("plan_unavailable", query["id"], "No query plan supplied.")
            return {"status": "unavailable", "tables": []}
        database = self.capture["metadata"]["database"]
        version = database["version"] or ""
        if (
            database["engine"] != "mysql"
            or not re.match(r"^8\.(?:0|4)(?:\.|$)", version)
            or "mariadb" in version.lower()
            or plan["format"] != "mysql_explain_json"
        ):
            self.gap(
                "plan_unsupported",
                query["id"],
                "Supported plans: MySQL 8.0/8.4 EXPLAIN FORMAT=JSON. Unknown versions, MariaDB, TREE and ANALYZE formats are not interpreted.",
            )
            return {"status": "unsupported", "tables": []}
        document = plan["document"]
        if not isinstance(document.get("query_block"), dict):
            self.gap("plan_unrecognized", query["id"], "Plan lacks a query_block object.")
            return {"status": "unrecognized", "tables": []}
        result, issues, flags = [], False, set()
        query_tables = set(query["tables"])
        # Traverse optimizer structure, not arbitrary condition text or projected source strings.
        stack = [document["query_block"]]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(reversed(node))
                continue
            if not isinstance(node, dict):
                continue
            for flag in ("using_filesort", "using_temporary_table"):
                if node.get(flag) is True:
                    flags.add(flag)
            table_node = node.get("table")
            if isinstance(table_node, dict):
                alias = identifier(table_node.get("table_name"))
                mapped = plan.get("table_map", {}).get(alias, alias)
                table = mapped if mapped in query_tables else None
                access = table_node.get("access_type")
                if not isinstance(access, str) or access not in {
                    "system",
                    "const",
                    "eq_ref",
                    "ref",
                    "fulltext",
                    "ref_or_null",
                    "index_merge",
                    "unique_subquery",
                    "index_subquery",
                    "range",
                    "index",
                    "ALL",
                }:
                    access = None
                row = {
                    "table": table,
                    "alias": alias,
                    "access_type": access,
                    "key": identifier(table_node.get("key")),
                    "possible_keys": [v for v in table_node.get("possible_keys", []) if identifier(v)]
                    if isinstance(table_node.get("possible_keys"), list)
                    else [],
                    "estimated_rows_per_scan": finite(table_node.get("rows_examined_per_scan")),
                    "estimated_rows_produced": finite(table_node.get("rows_produced_per_join")),
                    "estimated_filtered_percent": finite(table_node.get("filtered")),
                    "used_key_parts": [v for v in table_node.get("used_key_parts", []) if identifier(v)]
                    if isinstance(table_node.get("used_key_parts", []), list)
                    else [],
                    "evidence": "optimizer_estimate_not_actual_execution",
                }
                if row["estimated_filtered_percent"] is not None and row["estimated_filtered_percent"] > 100:
                    row["estimated_filtered_percent"] = None
                    issues = True
                    self.gap("plan_metric_unsupported", query["id"], "Plan filtered percentage exceeds 100.")
                for key in ("possible_keys", "used_key_parts"):
                    value = table_node.get(key)
                    if value is not None and (
                        not isinstance(value, list) or any(not identifier(v) for v in value)
                    ):
                        issues = True
                        self.gap(
                            "plan_key_unsupported",
                            query["id"],
                            "A plan index list/identifier is unsupported; omitted values are not interpreted.",
                        )
                if table_node.get("key") is not None and row["key"] is None:
                    issues = True
                    self.gap(
                        "plan_key_unsupported",
                        query["id"],
                        "A chosen-key expression is unsupported (for example index-merge syntax).",
                    )
                if table is None or access is None:
                    issues = True
                    self.gap(
                        "plan_table_unresolved",
                        query["id"],
                        "Plan aliases/derived tables or access types could not be mapped; use an explicit verified table_map where applicable.",
                    )
                for key in ("rows_examined_per_scan", "rows_produced_per_join", "filtered"):
                    if key in table_node and finite(table_node[key]) is None:
                        issues = True
                        self.gap(
                            "plan_metric_unsupported",
                            query["id"],
                            "A plan numeric metric is invalid or unsupported.",
                        )
                result.append(row)
                if access in {"ALL", "index"}:
                    self.finding(
                        "scan_review",
                        "Optimizer estimates a full table/index scan. A scan may be appropriate; review selectivity, workload and actual indexes before adding one.",
                        query_id=query["id"],
                        table=table,
                        access_type=access,
                        estimated_rows_per_scan=row["estimated_rows_per_scan"],
                    )
            stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
        for flag in sorted(flags):
            self.finding(
                "plan_" + flag,
                "Optimizer reports a sort/temporary-table operation, not proof of a slow query or disk spill.",
                query_id=query["id"],
            )
        if not result:
            self.gap(
                "plan_no_table_access",
                query["id"],
                "No supported table-access nodes found; this may be a constant/no-table query or unsupported plan.",
            )
            issues = True
        if not issues:
            self.plan_count += 1
        return {
            "status": "partial" if issues else "processed",
            "format": "mysql_explain_json",
            "tables": result,
            "flags": sorted(flags),
        }

    def run(self):
        queries = self.capture["queries"]
        requests = {r["id"]: r for r in self.capture.get("requests", [])}
        events = {e["id"]: e for q in queries for e in q.get("samples", [])}
        indexes = self.inventory()
        repetitions = defaultdict(list)
        request_work = defaultdict(Counter)
        report = []
        if not queries:
            self.gap(
                "empty_capture",
                "queries",
                "No query groups were supplied; this is not evidence of a fast workload.",
            )
        if not self.capture["metadata"]["database"]["version"]:
            self.gap("database_version_unknown", "metadata", "Actual database version was not captured.")
        if self.capture["metadata"]["workload"]["duration_ms"] is None:
            self.gap(
                "workload_window_unknown",
                "metadata",
                "The observation window is unknown; rankings do not imply normalized rates.",
            )
        if not requests:
            self.gap(
                "request_timings_unavailable",
                "requests",
                "No request samples; query time is not request latency.",
            )
        for query in queries:
            qid = query["id"]
            self.coverage.record("performance_selected", qid)
            timing = query["timing"]
            samples = query.get("samples", [])
            if timing["total_ms"] is None:
                self.gap("timing_unavailable", qid, "No total duration; this query cannot be ranked by time.")
            if timing["source"] == "unknown":
                self.gap("timing_source_unknown", qid, "Measurement boundary/source is unknown.")
            if len(samples) != timing["count"]:
                self.gap(
                    "event_samples_incomplete",
                    qid,
                    "Aggregate counts exceed available events; request repetition and parentage only cover supplied samples.",
                )
            phases = Counter()
            accepted = 0
            for event in samples:
                phases[event["phase"]] += 1
                parent = events.get(event.get("parent_id"))
                if event.get("parent_id") and not parent:
                    self.gap("parent_unresolved", qid, "A supplied parent event was not captured.")
                request = requests.get(event.get("request_id"))
                if event["correlation"] != "explicit" or event["phase"] == "unknown":
                    self.gap(
                        "correlation_unknown",
                        qid,
                        "No explicit verified correlation/phase; IDs alone are not used to infer request ownership.",
                    )
                    continue
                if event["phase"] == "background" and not event.get("request_id"):
                    continue
                if request is None:
                    self.gap(
                        "request_unresolved",
                        qid,
                        "Correlated request was not captured; no request attribution is inferred.",
                    )
                    continue
                accepted += 1
                request_work[request["id"]][(query["kind"], timing["source"], event["phase"])] += 1
                if query["kind"] == "sql" and event["phase"] == "request":
                    repetitions[(request["id"], timing["source"], query["fingerprint"])].append(event["id"])
            mappings = self.mappings(query)
            self.query_sources[qid] = self.source(query.get("source"), qid)
            row = {
                "id": qid,
                "kind": query["kind"],
                "fingerprint": query["fingerprint"],
                "operation": query["operation"],
                "tables": query["tables"],
                "timing": {
                    **timing,
                    "mean_ms": timing["total_ms"] / timing["count"]
                    if timing["total_ms"] is not None
                    else None,
                },
                "sample_count": len(samples),
                "sample_phases": dict(phases),
                "correlated_samples": accepted,
                "samples": samples,
                "source": self.query_sources[qid],
                "mappings": mappings,
                "plan": self.plan(query)
                if query["kind"] == "sql"
                else {"status": "not_applicable", "tables": []},
            }
            if query["kind"] == "prisma" and "plan" in query:
                self.gap(
                    "prisma_plan_not_sql",
                    qid,
                    "A logical Prisma operation is not a single SQL statement; attach plans to SQL groups instead.",
                )
            report.append(row)
            self.coverage.record("performance_analyzed", qid)
        sql_ids = {
            (query["timing"]["source"], query["fingerprint"]): query["id"]
            for query in queries
            if query["kind"] == "sql"
        }
        for (request_id, stream, fingerprint), ids in repetitions.items():
            if len(ids) > 1:
                self.finding(
                    "repeated_query",
                    "Repeated SQL fingerprint within one explicitly correlated request. This is a batching/N+1 review candidate, not proof of N+1 or wasted work.",
                    request_id=request_id,
                    query_id=sql_ids[(stream, fingerprint)],
                    timing_source=stream,
                    fingerprint=fingerprint,
                    sample_count=len(ids),
                    event_ids=ids,
                )
        report.sort(
            key=lambda q: (
                q["kind"],
                q["timing"]["source"],
                q["timing"]["total_ms"] is None,
                -(q["timing"]["total_ms"] or 0),
                q["id"],
            )
        )
        ranks = Counter()
        for row in report:
            stream = (row["kind"], row["timing"]["source"])
            ranks[stream] += 1
            row["rank_within_stream"] = ranks[stream] if row["timing"]["total_ms"] is not None else None
        request_rows = [
            {
                **request,
                "associated_samples": [
                    {"kind": kind, "timing_source": source, "phase": phase, "count": count}
                    for (kind, source, phase), count in sorted(request_work[request["id"]].items())
                ],
            }
            for request in requests.values()
        ]
        return {
            "performance": "database",
            "capture_version": self.capture["capture_version"],
            "metadata": self.capture["metadata"],
            **({"collection": self.capture["collection"]} if "collection" in self.capture else {}),
            "queries": report,
            "requests": request_rows,
            "indexes": indexes,
            "findings": self.findings,
            "unknowns": self.unknowns,
            "summary": {
                "query_groups": len(queries),
                "sql_groups": sum(q["kind"] == "sql" for q in queries),
                "prisma_groups": sum(q["kind"] == "prisma" for q in queries),
                "request_samples": len(requests),
                "event_samples": len(events),
                "plans_processed": self.plan_count,
                "findings": len(self.findings),
                "unknowns": len(self.unknowns),
            },
            "limitations": [
                "All measurements/index inventories/correlation are supplied evidence, not independently verified runtime facts. Source hash verification only verifies file content, not execution attribution.",
                "Rankings compare total recorded duration only within kind/timing-source streams. Overlapping requests, Prisma operations, nested calls and SQL times are never summed into request latency.",
                "MySQL EXPLAIN FORMAT=JSON contains optimizer estimates, not measured rows, lock time, pool wait, actual execution time or guaranteed bottlenecks. No SQL is executed.",
                "Partial samples do not establish request-wide counts or absence of N+1. Detached work may outlive the request. No percentiles are inferred from averages.",
                "Index definitions in Prisma/migrations are not proof of deployed indexes. Functional/unique/constraint indexes are protected; no CREATE/DROP INDEX or application fixes are generated.",
                "CPU, heap and cache measurements use the separate performance runtime/memory/cache commands. Comparison consumes compatible capture streams. Browser-specific and .NET Manager formats are not supported.",
            ],
        }
