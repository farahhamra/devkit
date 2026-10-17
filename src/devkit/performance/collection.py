"""Explicit collector lifecycle evidence; never infer completeness from a file existing."""

from .capture import choice, invalid, number, obj, rows

ISSUES = {
    "sample_limit",
    "size_limit",
    "collector_error",
    "cpu_unavailable",
    "gc_unavailable",
    "event_loop_unavailable",
    "cache_unavailable",
    "operation_unfinished",
    "request_unfinished",
    "operation_unsupported",
    "interrupted",
    "clock_boundary_uncertain",
    "heap_not_collected",
}


def validate_collection(data):
    value = obj(data["collection"], "collection", ("status", "dropped_samples", "issues", "windows"))
    choice(value["status"], "collection.status", {"completed", "limited", "interrupted"})
    number(value["dropped_samples"], "collection.dropped_samples", integer=True)
    issues = rows(value["issues"], "collection.issues")
    for issue in issues:
        choice(issue, "collection.issues[]", ISSUES)
    if len(issues) != len(set(issues)):
        invalid("collection.issues", "duplicate issues")
    if value["status"] == "completed" and (value["dropped_samples"] or issues):
        invalid("collection", "completed collection cannot have dropped samples or issues")
    duration = data["metadata"]["workload"]["duration_ms"]
    seen = set()
    for window in rows(value["windows"], "collection.windows"):
        obj(window, "collection.windows[]", ("stream", "start_ms", "end_ms", "boundary"))
        stream = choice(
            window["stream"],
            "collection.windows.stream",
            {"cpu_profile", "event_loop", "gc", "process_samples", "caches", "queries"},
        )
        if stream in seen:
            invalid("collection.windows", "duplicate stream")
        seen.add(stream)
        choice(window["boundary"], "collection.windows.boundary", {"observed", "control_bounds"})
        start = number(window["start_ms"], "collection.windows.start_ms")
        end = number(window["end_ms"], "collection.windows.end_ms")
        if end <= start or duration is None or end > duration:
            invalid("collection.windows", "windows must fit the declared collection duration")


def incomplete(capture):
    return "collection" in capture and capture["collection"]["status"] != "completed"


def report_collection(report, capture):
    if incomplete(capture):
        report.gap(
            "collection_incomplete",
            "collection",
            "Collector reports interrupted, limited or unavailable observations; no complete baseline is inferred.",
        )
