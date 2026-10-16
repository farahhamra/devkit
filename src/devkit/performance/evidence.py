"""Shared capture metadata and bounded, projected performance evidence."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime

from ..project import DevkitError
from .capture import choice, eligible_path, invalid, load_json, number, obj, rows, source, string

MAX_BYTES = 64 * 1024 * 1024
MAX_VALUES = 4_000_000
VERSION = r"[0-9][A-Za-z0-9_.+-]{0,63}"


def label(value):
    """Only symbolic names; never echo heap strings, arbitrary labels or script URLs."""
    if value in {"(root)", "(program)", "(idle)", "(garbage collector)"}:
        return value
    if value == "":
        return "(anonymous)"
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$.[\]<>-]{0,127}", value)
        else "(redacted)"
    )


def key(*parts):
    return hashlib.sha256(json.dumps(parts, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def array(value, where, maximum=20000):
    if not isinstance(value, list) or len(value) > maximum:
        invalid(where, f"expected an array with at most {maximum} entries")
    return value


def metadata(meta):
    obj(
        meta,
        "metadata",
        ("app", "captured_at", "runtime", "workload"),
        ("environment", "process_scope", "collector", "source_revision"),
    )
    choice(meta["app"], "metadata.app", {"erp", "hub"})
    try:
        if datetime.fromisoformat(meta["captured_at"]).utcoffset() is None:
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        invalid("metadata.captured_at", "expected an ISO timestamp with timezone")
    runtime = obj(meta["runtime"], "metadata.runtime", ("engine", "version"))
    choice(runtime["engine"], "metadata.runtime.engine", {"node"})
    string(runtime["version"], "metadata.runtime.version", VERSION)
    workload = obj(
        meta["workload"],
        "metadata.workload",
        ("name", "duration_ms"),
        ("concurrency", "warmup_ms", "operations"),
    )
    string(workload["name"], "metadata.workload.name")
    number(workload["duration_ms"], "metadata.workload.duration_ms", minimum=0.000001)
    for name in ("concurrency", "warmup_ms", "operations"):
        if name in workload:
            number(
                workload[name],
                "metadata.workload." + name,
                integer=name != "warmup_ms",
                minimum=1 if name == "concurrency" else 0,
            )
    for name in ("environment", "process_scope", "collector"):
        if name in meta:
            string(meta[name], "metadata." + name)
    if "source_revision" in meta:
        string(meta["source_revision"], "metadata.source_revision", r"[a-f0-9]{40}|[a-f0-9]{64}")


def load(project, raw, expected=None):
    data, path = load_json(
        project,
        raw,
        suffixes=(".json", ".cpuprofile", ".heapsnapshot"),
        maximum_bytes=MAX_BYTES,
        maximum_nodes=MAX_VALUES,
    )
    if not isinstance(data, dict):
        invalid("capture", "expected an object")
    if data.get("capture_version") == "1.0":
        from .capture import load as load_database

        data, path = load_database(project, raw)
        kind = "database"
    elif "capture_version" not in data:
        if {"snapshot", "nodes", "edges", "strings"} <= data.keys():
            kind, field = "memory", "heap_snapshot"
        elif {"nodes", "startTime", "endTime"} <= data.keys():
            kind, field = "runtime", "cpu_profile"
        else:
            invalid("capture", "expected capture 2.0, a V8 CPU profile or a V8 heap snapshot")
        data = {"capture_version": "native", "kind": kind, field: data, "metadata": None}
    else:
        choice(data["capture_version"], "capture_version", {"2.0"})
        kind = choice(data.get("kind"), "kind", {"runtime", "memory", "cache"})
        fields = {
            "runtime": ("cpu_profile", "event_loop", "gc"),
            "memory": ("heap_snapshot", "process_samples"),
            "cache": ("caches",),
        }[kind]
        obj(data, "capture", ("capture_version", "kind", "metadata"), (*fields, "sources"))
        metadata(data["metadata"])
        seen = set()
        for row in rows(data.get("sources", []), "sources"):
            obj(row, "sources[]", ("id", "location"))
            string(row["id"], "sources[].id")
            if row["id"] in seen:
                invalid("sources", "duplicate attribution IDs")
            seen.add(row["id"])
            source(row["location"], "sources[].location")
    if expected and kind != expected:
        raise DevkitError("Performance command and capture kind differ; select the matching subcommand")
    meta = data.get("metadata")
    if meta and project.app not in {"all", meta["app"]}:
        raise DevkitError("Capture app does not match --app")
    return kind, data, path


def distribution(value, where):
    obj(value, where, ("count", "total_ms", "min_ms", "max_ms"), ("p50_ms", "p95_ms", "p99_ms"))
    number(value["count"], where + ".count", integer=True, minimum=1)
    for field, item in value.items():
        number(item, where + "." + field)
    low, high = value["min_ms"], value["max_ms"]
    if high < low or not low * value["count"] - 1e-6 <= value["total_ms"] <= high * value["count"] + 1e-6:
        invalid(where, "inconsistent count, total or bounds")
    previous = low
    for field in ("p50_ms", "p95_ms", "p99_ms"):
        if field in value:
            if not previous <= value[field] <= high:
                invalid(where, "percentiles are outside ordered bounds")
            previous = value[field]
    return {**value, "mean_ms": value["total_ms"] / value["count"]}


class Report:
    def __init__(self, project, capture, kind, prefix=""):
        self.project, self.capture, self.kind, self.prefix = project, capture, kind, prefix
        self.meta = capture.get("metadata")
        self.unknowns, self.findings, self.metrics = [], [], []
        self.units, self._gaps = set(), set()
        self.locations = {r["id"]: r["location"] for r in capture.get("sources", [])}
        self.source_cache, self.source_bytes = {}, 0
        if not self.meta:
            self.gap(
                "metadata_unavailable",
                "capture",
                "Native input has no workload, app or environment metadata; use a versioned capture for comparison.",
            )

    def gap(self, code, subject, message):
        if (code, subject) not in self._gaps:
            self._gaps.add((code, subject))
            self.unknowns.append({"code": code, "subject": subject, "message": message})
            self.project.coverage.gap("performance_" + code, self.prefix + subject, stage="performance")

    def unit(self, value):
        self.units.add(str(value))
        for state in ("selected", "analyzed"):
            self.project.coverage.record("performance_" + state, self.prefix + self.kind + ":" + str(value))

    def metric(self, identifier, value, unit, direction="lower", **extra):
        if value is not None:
            self.metrics.append(
                {"id": identifier, "value": value, "unit": unit, "direction": direction, **extra}
            )

    def finding(self, rule, message, **extra):
        self.findings.append({"rule": rule, "confidence": "candidate", "message": message, **extra})

    def location(self, identifier):
        value = self.locations.get(str(identifier))
        if not value:
            return {"location_status": "not_supplied"}
        path = value["path"]
        if path not in self.source_cache:
            try:
                if not self.meta:
                    raise DevkitError("app unknown")
                target = eligible_path(self.project, path, app=self.meta["app"])
                if target.stat().st_size > 4 * 1024 * 1024:
                    raise DevkitError("source budget")
                with target.open("rb") as handle:
                    payload = handle.read(min(4 * 1024 * 1024, 16 * 1024 * 1024 - self.source_bytes) + 1)
                if self.source_bytes + len(payload) > 16 * 1024 * 1024 or len(payload) > 4 * 1024 * 1024:
                    raise DevkitError("source budget")
                self.source_bytes += len(payload)
                text = payload.decode("utf-8-sig")
                if "\x00" in text:
                    raise DevkitError("binary source")
                self.source_cache[path] = hashlib.sha256(payload).hexdigest(), len(text.splitlines())
            except (DevkitError, OSError, UnicodeError):
                self.source_cache[path] = None
        known = self.source_cache[path]
        if known is None or known[0] != value["sha256"] or value["end_line"] > known[1]:
            self.gap(
                "source_unverified",
                str(identifier),
                "Supplied source is stale, excluded, inaccessible or over budget; no range emitted.",
            )
            return {"location_status": "unverified"}
        return {
            **value,
            "location_status": "content_verified",
            "attribution": "capture_supplied_not_runtime_verified",
        }

    def finish(self, **data):
        if not self.units:
            self.gap("empty_capture", self.kind, "No usable measurements were supplied.")
        # Also validate explicitly supplied attributions not selected for display.
        for identifier in self.locations:
            self.location(identifier)
        summary = data.pop("summary", {})
        return {
            "performance": self.kind,
            "capture_version": self.capture["capture_version"],
            "metadata": self.meta,
            **data,
            "metrics": self.metrics,
            "findings": self.findings,
            "unknowns": self.unknowns,
            "summary": {
                **summary,
                "measurement_units": len(self.units),
                "findings": len(self.findings),
                "unknowns": len(self.unknowns),
            },
            "limitations": [
                "All inputs and metadata are exporter-supplied observations, not independently verified measurements.",
                "Sampling, workload differences and observer overhead affect results. Candidates are not proof of a bottleneck or leak.",
                "Source hashes verify content and inclusive 1-based ranges, not execution attribution. Missing attribution is not inferred.",
                "No application execution, profiling, network access, database connection or automatic fixes occur.",
            ],
        }


def sample_time(row, where, previous, duration=None):
    stamp = number(row["at_ms"], where + ".at_ms")
    if stamp - previous < 0.000001 or (duration is not None and stamp > duration):
        invalid(where, "sample timestamps must increase within the declared window")
    return stamp


def window(report, samples, subject):
    if report.meta and samples:
        duration = report.meta["workload"]["duration_ms"]
        if samples[0]["at_ms"] != 0 or samples[-1]["at_ms"] != duration:
            report.gap(
                "window_partial",
                subject,
                "Samples do not span the declared workload window; rates use only observed intervals.",
            )
