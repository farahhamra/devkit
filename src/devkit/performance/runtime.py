"""V8 CPU sampling, Node event-loop histograms and GC event evidence."""

from collections import Counter, defaultdict

from .capture import choice, invalid, number, obj
from .evidence import Report, array, distribution, key, label

MAX_PROFILE_NODES = 100_000
MAX_SAMPLES = 1_000_000


def cpu(report, profile):
    obj(profile, "cpu_profile", ("nodes", "startTime", "endTime"), ("samples", "timeDeltas"))
    start = number(profile["startTime"], "cpu_profile.startTime")
    end = number(profile["endTime"], "cpu_profile.endTime", minimum=start)
    nodes = array(profile["nodes"], "cpu_profile.nodes", MAX_PROFILE_NODES)
    by_id, parent = {}, {}
    for node in nodes:
        obj(
            node,
            "cpu_profile.nodes[]",
            ("id", "callFrame"),
            ("children", "hitCount", "positionTicks", "deoptReason"),
        )
        identifier = number(node["id"], "cpu_profile.node.id", integer=True, minimum=1)
        if identifier in by_id:
            invalid("cpu_profile.nodes", "duplicate node IDs")
        frame = obj(
            node["callFrame"],
            "cpu_profile.callFrame",
            ("functionName", "scriptId", "url", "lineNumber", "columnNumber"),
        )
        for field in ("functionName", "scriptId", "url"):
            if not isinstance(frame[field], str) or len(frame[field]) > 4096:
                invalid("cpu_profile.callFrame", "invalid frame string")
        for field in ("lineNumber", "columnNumber"):
            number(frame[field], "cpu_profile.callFrame." + field, integer=True, minimum=-1, maximum=10**8)
        for child in array(node.get("children", []), "cpu_profile.children", MAX_PROFILE_NODES):
            number(child, "cpu_profile.child", integer=True, minimum=1)
            if child in parent or child == identifier:
                invalid("cpu_profile.children", "call tree has duplicate parents or a self cycle")
            parent[child] = identifier
        if "hitCount" in node:
            number(node["hitCount"], "cpu_profile.hitCount", integer=True)
        by_id[identifier] = node
    roots = set(by_id) - set(parent)
    if not nodes or len(roots) != 1 or not set(parent) <= set(by_id):
        invalid("cpu_profile.nodes", "expected one connected tree with valid child references")
    order, pending, visited = [], [next(iter(roots))], set()
    while pending:
        identifier = pending.pop()
        if identifier in visited:
            invalid("cpu_profile.nodes", "cyclic call tree")
        visited.add(identifier)
        order.append(identifier)
        pending.extend(by_id[identifier].get("children", []))
    if len(visited) != len(nodes):
        invalid("cpu_profile.nodes", "disconnected or cyclic call tree")
    samples = array(profile.get("samples", []), "cpu_profile.samples", MAX_SAMPLES)
    for identifier in samples:
        if type(identifier) is not int or identifier not in by_id:
            invalid("cpu_profile.samples", "unknown sample node ID")
    deltas = profile.get("timeDeltas")
    if deltas is not None:
        array(deltas, "cpu_profile.timeDeltas", MAX_SAMPLES)
        if len(deltas) != len(samples):
            invalid("cpu_profile.timeDeltas", "one time delta is required per sample")
        for delta in deltas:
            number(delta, "cpu_profile.timeDeltas[]")
        if sum(deltas) > end - start + 1e-6:
            invalid("cpu_profile.timeDeltas", "sample deltas exceed the profile window")
    else:
        report.gap(
            "cpu_timing_missing",
            "cpu",
            "Sample counts are available without timing; hit counts are not converted into milliseconds.",
        )
    if not samples:
        report.gap(
            "cpu_samples_missing", "cpu", "No CPU samples; a call tree alone does not measure function cost."
        )
    counts, weights = Counter(samples), defaultdict(float)
    if deltas is not None:
        for identifier, delta in zip(samples, deltas):
            weights[identifier] += delta / 1000
    inclusive, inclusive_count = dict(weights), dict(counts)
    for identifier in reversed(order):
        owner = parent.get(identifier)
        if owner is not None:
            inclusive[owner] = inclusive.get(owner, 0) + inclusive.get(identifier, 0)
            inclusive_count[owner] = inclusive_count.get(owner, 0) + inclusive_count.get(identifier, 0)
    tree, grouped = [], {}
    observed = sum(deltas) / 1000 if deltas is not None else None
    duration = (end - start) / 1000
    complete_window = bool(report.meta and abs(report.meta["workload"]["duration_ms"] - duration) <= 0.001)
    for identifier in order:
        frame = by_id[identifier]["callFrame"]
        identity = key(frame["url"], frame["functionName"], frame["lineNumber"], frame["columnNumber"])
        name = label(frame["functionName"])
        group = grouped.setdefault(
            identity,
            {"id": identity, "function": name, "samples": 0, "self_ms": 0 if deltas is not None else None},
        )
        group["samples"] += counts[identifier]
        if deltas is not None:
            group["self_ms"] += weights[identifier]
        row = {
            "node_id": identifier,
            "parent_id": parent.get(identifier),
            "frame_id": identity,
            "function": name,
            "self_samples": counts[identifier],
            "inclusive_samples": inclusive_count.get(identifier, 0),
            "self_ms": weights[identifier] if deltas is not None else None,
            "inclusive_ms": inclusive.get(identifier, 0) if deltas is not None else None,
            "source": report.location(identifier),
        }
        tree.append(row)
        report.unit("cpu:" + str(identifier))
    functions = sorted(
        grouped.values(), key=lambda r: (-(r["self_ms"] if deltas is not None else r["samples"]), r["id"])
    )
    for row in functions:
        row["self_share_percent"] = row["self_ms"] / observed * 100 if observed else None
        if row["samples"]:
            report.metric(
                "cpu.self_ms:" + row["id"],
                row["self_ms"],
                "ms",
                function=row["function"],
                direction="higher" if row["function"] == "(idle)" else "lower",
                basis="window_total",
                window_ms=duration,
                comparable=complete_window,
            )
    if report.meta and not complete_window:
        report.gap(
            "cpu_window_mismatch",
            "cpu",
            "CPU profile and declared workload windows differ; CPU metrics are not compared.",
        )
    if observed is not None:
        idle = sum(weights[n] for n, node in by_id.items() if node["callFrame"]["functionName"] == "(idle)")
        report.metric(
            "cpu.sampled_non_idle_ms",
            observed - idle,
            "ms",
            basis="window_total",
            window_ms=duration,
            comparable=complete_window,
        )
    return {
        "duration_ms": duration,
        "sample_count": len(samples),
        "timed_samples": len(samples) if deltas is not None else 0,
        "observed_ms": observed,
        "unsampled_tail_ms": max(0, duration - observed) if observed is not None else None,
        "functions": functions,
        "call_tree": tree,
        "timing_basis": "sample time deltas attributed to the sampled frame; estimates, not instrumented function durations",
        "inclusive_note": "Inclusive times overlap across ancestors; recursive frames remain distinct in the call tree. Function totals aggregate self time only.",
    }


def analyze(project, capture, prefix=""):
    report = Report(project, capture, "runtime", prefix)
    data = {}
    if "cpu_profile" in capture:
        data["cpu"] = cpu(report, capture["cpu_profile"])
    else:
        report.gap("cpu_profile_missing", "cpu", "No CPU profile was supplied.")
    event = capture.get("event_loop")
    if event is not None:
        obj(event, "event_loop", ("window_ms", "resolution_ms", "delay"), ("utilization",))
        number(event["window_ms"], "event_loop.window_ms", minimum=0.000001)
        number(event["resolution_ms"], "event_loop.resolution_ms", minimum=0.000001)
        delay = distribution(event["delay"], "event_loop.delay")
        data["event_loop"] = {**event, "delay": delay}
        complete_window = bool(report.meta and event["window_ms"] == report.meta["workload"]["duration_ms"])
        for field in ("mean_ms", "max_ms", "p50_ms", "p95_ms", "p99_ms"):
            report.metric(
                "event_loop.delay." + field,
                delay.get(field),
                "ms",
                signature={"resolution_ms": event["resolution_ms"]},
                comparable=complete_window,
            )
        if report.meta and event["window_ms"] != report.meta["workload"]["duration_ms"]:
            report.gap(
                "event_loop_window_mismatch",
                "event_loop",
                "Histogram window differs from the declared workload.",
            )
        if "utilization" in event:
            usage = obj(event["utilization"], "event_loop.utilization", ("active_ms", "idle_ms"))
            for value in usage.values():
                number(value, "event_loop.utilization")
            total = usage["active_ms"] + usage["idle_ms"]
            if total > event["window_ms"] + 0.001:
                invalid("event_loop.utilization", "active plus idle time exceeds the measurement window")
            ratio = usage["active_ms"] / total if total else None
            data["event_loop"]["utilization"] = {**usage, "ratio": ratio}
            report.metric("event_loop.utilization", ratio, "ratio", "neutral", comparable=complete_window)
        report.unit("event_loop")
    else:
        report.gap(
            "event_loop_missing",
            "event_loop",
            "CPU samples do not measure event-loop delay; no histogram supplied.",
        )
    if "gc" in capture:
        events = array(capture["gc"], "gc")
        intervals, totals = [], Counter()
        duration = report.meta["workload"]["duration_ms"] if report.meta else None
        for event in events:
            obj(event, "gc[]", ("start_ms", "duration_ms", "kind"))
            start = number(event["start_ms"], "gc.start_ms")
            elapsed = number(event["duration_ms"], "gc.duration_ms")
            kind = choice(event["kind"], "gc.kind", {"minor", "major", "incremental", "weakcb", "unknown"})
            if duration is not None and start + elapsed > duration + 1e-6:
                invalid("gc", "event extends outside the declared window")
            intervals.append((start, start + elapsed))
            totals[kind] += 1
        union, right = 0, 0
        for left, end in sorted(intervals):
            union += max(0, end - max(left, right))
            right = max(right, end)
        data["gc"] = {
            "event_count": len(events),
            "counts_by_kind": dict(totals),
            "event_union_ms": union,
            "note": "Union of observed GC event intervals, not a measurement of stop-the-world pauses.",
        }
        report.metric("gc.event_union_ms", union, "ms", basis="window_total", window_ms=duration)
        report.metric(
            "gc.event_count", len(events), "events", "neutral", basis="window_total", window_ms=duration
        )
        report.unit("gc")
    else:
        report.gap(
            "gc_missing", "gc", "No GC event collection was supplied; absent events are not zero observed GC."
        )
    return report.finish(**data)
