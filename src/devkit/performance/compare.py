"""Comparison of compatible measured streams; no absent-as-zero inference."""

import math

from ..project import DevkitError
from .evidence import Report, load


def database_metrics(project, capture, prefix):
    report = Report(project, capture, "database", prefix)
    duration = capture["metadata"]["workload"]["duration_ms"]
    for query in capture["queries"]:
        timing = query["timing"]
        identity = query["kind"] + ":" + timing["source"] + ":" + query["fingerprint"]
        signature = {"kind": query["kind"], "collector": timing["source"]}
        report.unit(identity)
        if timing["source"] == "unknown" or timing["total_ms"] is None:
            report.gap(
                "timing_unavailable",
                identity,
                "Unknown collector or missing total; no timing comparison is inferred.",
            )
            continue
        report.metric(
            "database.mean_ms:" + identity, timing["total_ms"] / timing["count"], "ms", signature=signature
        )
        report.metric(
            "database.total_ms:" + identity,
            timing["total_ms"],
            "ms",
            basis="window_total",
            window_ms=duration,
            signature=signature,
        )
        report.metric(
            "database.count:" + identity,
            timing["count"],
            "queries",
            "neutral",
            basis="window_total",
            window_ms=duration,
            signature=signature,
        )
    return report.finish(summary={"query_groups": len(capture["queries"])})


def inspect(project, kind, capture, prefix):
    if kind == "database":
        return database_metrics(project, capture, prefix)
    from . import cache, memory, runtime

    return {"runtime": runtime, "memory": memory, "cache": cache}[kind].analyze(project, capture, prefix)


def compatibility(before, after, kind):
    reasons = []
    if before is None or after is None:
        return [{"field": "metadata", "reason": "missing"}]
    pairs = [
        (field, before.get(field), after.get(field))
        for field in ("app", "environment", "process_scope", "collector", "runtime")
    ]
    pairs += [
        ("workload." + field, before["workload"].get(field), after["workload"].get(field))
        for field in ("name", "concurrency", "warmup_ms")
    ]
    if kind == "database":
        pairs.append(("database", before.get("database"), after.get("database")))
        for meta in (before, after):
            if meta["database"]["engine"] == "unknown" or meta["database"]["version"] is None:
                reasons.append({"field": "database", "reason": "unknown_engine_or_version"})
            if not meta.get("runtime", {}).get("node"):
                reasons.append({"field": "runtime.node", "reason": "missing"})
    for field, left, right in pairs:
        if left is None or right is None or left == {} or right == {}:
            reasons.append({"field": field, "reason": "missing"})
        elif left != right:
            reasons.append({"field": field, "reason": "different"})
    return reasons


def normalized(metric):
    if not metric.get("comparable", True):
        return None
    if metric.get("basis") == "window_total":
        window = metric.get("window_ms")
        if window is None or window <= 0:
            return None
        value = metric["value"] * 1000 / window
        return (value, metric["unit"] + "/second") if math.isfinite(value) else None
    return metric["value"], metric["unit"]


def compare(project, args):
    threshold = args.regression_percent
    if not math.isfinite(threshold) or not 0 <= threshold <= 1_000_000:
        raise DevkitError("--regression-percent must be finite and between 0 and 1000000")
    kind, before, before_path = load(project, args.before)
    after_kind, after, after_path = load(project, args.after)
    if kind != after_kind:
        raise DevkitError("Performance comparison requires captures of the same kind")
    project.coverage.details["performance_captures"] = {
        "kind": kind,
        "before": before_path,
        "after": after_path,
    }
    baseline = inspect(project, kind, before, "before:")
    candidate = inspect(project, kind, after, "after:")
    report = Report(
        project, {"capture_version": "comparison", "metadata": after.get("metadata")}, "compare", "compare:"
    )
    reasons = compatibility(before.get("metadata"), after.get("metadata"), kind)
    from .collection import incomplete

    if incomplete(before) or incomplete(after):
        reasons.append({"field": "collection", "reason": "incomplete"})
    for issue in reasons:
        report.gap(
            "comparison_incompatible",
            issue["field"],
            "Capture metadata is missing or different; no numeric comparisons are emitted.",
        )
    rows, unmatched = [], []
    if not reasons:
        before_metrics = {m["id"]: m for m in baseline["metrics"]}
        after_metrics = {m["id"]: m for m in candidate["metrics"]}
        bw, aw = before["metadata"]["workload"], after["metadata"]["workload"]
        endpoint_equal = (
            bw.get("duration_ms") is not None and bw["duration_ms"] == aw.get("duration_ms")
        ) or (bw.get("operations", 0) > 0 and bw["operations"] == aw.get("operations"))
        for identifier in sorted(before_metrics.keys() | after_metrics.keys()):
            left, right = before_metrics.get(identifier), after_metrics.get(identifier)
            if left is None or right is None:
                unmatched.append(
                    {"id": identifier, "status": "only_after" if left is None else "only_before"}
                )
                report.gap(
                    "comparison_unmatched",
                    identifier,
                    "An absent measurement is unknown, not zero; samples/frames/fingerprints may differ.",
                )
                continue
            x, y = normalized(left), normalized(right)
            issue = None
            if x is None or y is None:
                issue = "incomplete_measurement_window"
            elif (
                x[1] != y[1]
                or left["direction"] != right["direction"]
                or left.get("signature") != right.get("signature")
            ):
                issue = "different_measurement_boundary"
            elif left.get("basis") == "endpoint" and not endpoint_equal:
                issue = "different_duration_and_operation_count"
            if issue:
                unmatched.append({"id": identifier, "status": issue})
                report.gap(
                    "comparison_metric_incompatible",
                    identifier,
                    "Measurement boundaries or endpoint workloads are not comparable.",
                )
                continue
            delta = y[0] - x[0]
            percent = delta / abs(x[0]) * 100 if x[0] else (0.0 if delta == 0 else None)
            percent_status = "undefined_zero_baseline" if percent is None else "computed"
            if percent is not None and not math.isfinite(percent):
                percent, percent_status = None, "numeric_overflow"
            direction = left["direction"]
            worse = (direction == "lower" and delta > 0) or (direction == "higher" and delta < 0)
            regression = worse and (percent is None or abs(percent) >= threshold)
            row = {
                "id": identifier,
                "unit": x[1],
                "before": x[0],
                "after": y[0],
                "delta": delta,
                "percent_change": percent,
                "percent_status": percent_status,
                "direction": direction,
                "regression_candidate": regression,
                "zero_baseline": x[0] == 0,
                "change": "unchanged" if delta == 0 else "increased" if delta > 0 else "decreased",
            }
            rows.append(row)
            report.unit(identifier)
            if regression:
                report.finding(
                    "regression_candidate",
                    "Measured change crosses the selected relative threshold; a single pair does not establish statistical significance or causation.",
                    metric_id=identifier,
                )
    if not rows:
        report.gap("comparison_no_metrics", "compare", "No compatible measured metrics could be compared.")
    return report.finish(
        kind=kind,
        comparison_status="incompatible" if reasons else "partial" if unmatched or not rows else "comparable",
        compatibility={"basis": "exporter_declared_not_independently_verified", "issues": reasons},
        before={
            "input": before_path,
            "metadata": before.get("metadata"),
            "summary": baseline["summary"],
            "unknowns": baseline["unknowns"],
        },
        after={
            "input": after_path,
            "metadata": after.get("metadata"),
            "summary": candidate["summary"],
            "unknowns": candidate["unknowns"],
        },
        comparisons=rows,
        unmatched=unmatched,
        summary={
            "compared_metrics": len(rows),
            "unmatched_metrics": len(unmatched),
            "regression_candidates": sum(row["regression_candidate"] for row in rows),
            "regression_percent": threshold,
            "fail_on_regression": args.fail_on_regression,
        },
    )
