"""Process-memory time series and bounded native V8 heap analysis."""

from . import heap
from .capture import boolean, invalid, number, obj
from .evidence import Report, array, sample_time, window

FIELDS = ("heap_used_bytes", "heap_total_bytes", "rss_bytes", "external_bytes", "array_buffers_bytes")


def slope(samples, field):
    if len(samples) < 2:
        return None
    times = [(row["at_ms"] - samples[0]["at_ms"]) / 1000 for row in samples]
    mean_time = sum(times) / len(times)
    mean_value = sum(row[field] for row in samples) / len(samples)
    denominator = sum((t - mean_time) ** 2 for t in times)
    return (
        sum((t - mean_time) * (row[field] - mean_value) for t, row in zip(times, samples)) / denominator
        if denominator
        else None
    )


def analyze(project, capture, prefix=""):
    report = Report(project, capture, "memory", prefix)
    output = {}
    samples = array(capture.get("process_samples", []), "process_samples")
    previous = -1
    duration = report.meta["workload"]["duration_ms"] if report.meta else None
    for row in samples:
        obj(
            row,
            "process_samples[]",
            ("at_ms", "heap_used_bytes", "heap_total_bytes", "rss_bytes", "external_bytes", "post_gc"),
            ("array_buffers_bytes",),
        )
        previous = sample_time(row, "process_samples[]", previous, duration)
        boolean(row["post_gc"], "process_samples.post_gc")
        for field in FIELDS:
            if field in row:
                number(row[field], "process_samples." + field, integer=True)
        if row["heap_used_bytes"] > row["heap_total_bytes"]:
            invalid("process_samples", "heap used exceeds heap total")
        if row.get("array_buffers_bytes", 0) > row["external_bytes"]:
            invalid("process_samples", "array buffers are a subset of external memory")
        report.unit("process:" + str(row["at_ms"]))
    if samples:
        window(report, samples, "process_samples")
        complete_window = samples[0]["at_ms"] == 0 and samples[-1]["at_ms"] == duration
        trends = []
        for field in FIELDS:
            available = [row for row in samples if field in row]
            if not available:
                continue
            row = {
                "metric": field,
                "samples": len(available),
                "first": available[0][field],
                "last": available[-1][field],
                "peak": max(s[field] for s in available),
                "delta_bytes": available[-1][field] - available[0][field],
                "slope_bytes_per_second": slope(available, field),
            }
            trends.append(row)
            comparable = complete_window and len(available) == len(samples)
            report.metric(
                "memory." + field + ".last", row["last"], "bytes", basis="endpoint", comparable=comparable
            )
            report.metric(
                "memory." + field + ".peak", row["peak"], "bytes", basis="endpoint", comparable=comparable
            )
        post_gc = [row for row in samples if row["post_gc"]]
        growth = slope(post_gc, "heap_used_bytes") if len(post_gc) >= 3 else None
        if growth is not None:
            report.metric(
                "memory.post_gc_heap_growth",
                growth,
                "bytes/second",
                basis="endpoint",
                comparable=complete_window,
            )
            if growth > 0 and post_gc[-1]["heap_used_bytes"] > post_gc[0]["heap_used_bytes"]:
                report.finding(
                    "post_gc_growth",
                    "Observed post-GC heap growth warrants retention review; cache warmup and legitimate live data can also grow.",
                    slope_bytes_per_second=growth,
                    samples=len(post_gc),
                )
        else:
            report.gap(
                "post_gc_samples_insufficient",
                "process_samples",
                "At least three exporter-labelled post-GC samples are needed for a growth trend; leaks cannot be inferred from RSS alone.",
            )
        output["process"] = {
            "sample_count": len(samples),
            "duration_ms": samples[-1]["at_ms"] - samples[0]["at_ms"],
            "trends": trends,
            "post_gc_samples": len(post_gc),
            "post_gc_growth_bytes_per_second": growth,
            "samples": samples,
            "note": "RSS, V8 heap, external memory and array buffers overlap; they are not additive. Post-GC labels are exporter claims.",
        }
    else:
        report.gap(
            "memory_samples_missing",
            "process_samples",
            "A snapshot alone cannot establish memory growth over time.",
        )
    if "heap_snapshot" in capture:
        output["heap"] = heap.analyze(report, capture["heap_snapshot"])
    else:
        report.gap(
            "heap_snapshot_missing",
            "heap",
            "Process counters do not identify retained objects; no heap snapshot supplied.",
        )
    return report.finish(**output)
