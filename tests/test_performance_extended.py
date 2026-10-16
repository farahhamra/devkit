"""Offline runtime/heap/cache calculations and conservative comparisons."""

import copy
import hashlib
import json
import math
import random
import socket
import subprocess
from pathlib import Path

import pytest

from devkit.cli import main
from devkit.performance import evidence, heap
from devkit.project import DevkitError, Project

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def capture(kind):
    return json.loads((EXAMPLES / f"performance-{kind}.json").read_text())


def write(root, name, value):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return name


def invoke(root, capsys, action, data=None, *args, status=0, path="measurements/input.json", budget=200000):
    if data is not None:
        write(root, path, data)
    inputs = [] if action == "compare" else ["--input", path]
    actual = main(
        [
            "--root",
            str(root),
            "performance",
            action,
            *inputs,
            "--app",
            "erp",
            "--json",
            "--max-output-chars",
            str(budget),
            *args,
        ]
    )
    output = capsys.readouterr()
    assert actual == status, output.out + output.err
    assert not output.err
    assert len(output.out) <= budget

    def reject_constant(value):
        raise AssertionError("Nonfinite value in JSON output: " + value)

    result = json.loads(output.out, parse_constant=reject_constant)
    assert "SYNTHETIC_PRIVATE" not in output.out and "DO_NOT_PRINT" not in output.out
    return result


def compare(root, capsys, before, after, *args, status=0):
    write(root, "measurements/before.json", before)
    write(root, "measurements/after.json", after)
    return invoke(
        root,
        capsys,
        "compare",
        None,
        "--before",
        "measurements/before.json",
        "--after",
        "measurements/after.json",
        *args,
        status=status,
    )


@pytest.mark.parametrize("kind", ["runtime", "memory", "cache"])
def test_complete_captures_are_offline_and_do_not_scan_schema_or_write_reports(
    project, capsys, monkeypatch, kind
):
    def forbidden(*args, **kwargs):
        raise AssertionError("No network, subprocess or application execution")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    result = invoke(project, capsys, kind, capture(kind), "--strict")
    assert result["data"]["summary"]["complete"]
    assert result["coverage"]["stages"]["performance"] == "complete"
    assert result["coverage"]["stages"]["schema"] == "not_run"
    assert result["coverage"]["stages"]["source"] == "not_run"
    assert not (project / "devkit/.cache").exists()


def test_cpu_weighted_self_inclusive_recursive_frames_and_gc_union(project, capsys):
    data = capture("runtime")
    # Recursion: the same function at two call-tree nodes must not double-count inclusive totals.
    data["cpu_profile"]["nodes"][3]["callFrame"] = copy.deepcopy(data["cpu_profile"]["nodes"][1]["callFrame"])
    result = invoke(project, capsys, "runtime", data)["data"]
    work = next(row for row in result["cpu"]["functions"] if row["function"] == "work")
    assert work["self_ms"] == 30 and work["self_share_percent"] == 75
    tree = {n["node_id"]: n for n in result["cpu"]["call_tree"]}
    assert tree[2]["self_ms"] == 20 and tree[2]["inclusive_ms"] == 30
    assert tree[4]["self_ms"] == tree[4]["inclusive_ms"] == 10
    assert tree[4]["parent_id"] == 2 and tree[1]["inclusive_ms"] == 40
    assert result["gc"]["event_union_ms"] == 5  # overlapping events are not 4 + 3
    assert result["event_loop"]["delay"]["mean_ms"] == 5
    assert result["event_loop"]["utilization"]["ratio"] == 0.75


def test_native_cpu_profile_omits_urls_and_does_not_infer_missing_timings(project, capsys):
    native = capture("runtime")["cpu_profile"]
    native.pop("timeDeltas")
    native["nodes"][1]["callFrame"]["url"] = "https://DO_NOT_PRINT.example/?token=DO_NOT_PRINT"
    result = invoke(
        project, capsys, "runtime", native, "--strict", status=1, path="measurements/cpu.cpuprofile"
    )
    assert result["data"]["cpu"]["observed_ms"] is None
    assert all(row["self_ms"] is None for row in result["data"]["cpu"]["functions"])
    assert {u["code"] for u in result["data"]["unknowns"]} >= {
        "metadata_unavailable",
        "cpu_timing_missing",
        "event_loop_missing",
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "cycle",
        "unknown_child",
        "duplicate",
        "sample",
        "negative_delta",
        "excess_time",
        "length",
        "frame",
        "zero_roots",
    ],
)
def test_invalid_cpu_graphs_and_timing_are_errors(project, capsys, mutation):
    data = capture("runtime")
    p = data["cpu_profile"]
    if mutation == "cycle":
        p["nodes"][3]["children"] = [2]
    elif mutation == "unknown_child":
        p["nodes"][0]["children"].append(999)
    elif mutation == "duplicate":
        p["nodes"][1]["id"] = 1
    elif mutation == "sample":
        p["samples"][0] = 999
    elif mutation == "negative_delta":
        p["timeDeltas"][0] = -1
    elif mutation == "excess_time":
        p["timeDeltas"][0] = 100000
    elif mutation == "length":
        p["timeDeltas"].pop()
    elif mutation == "frame":
        p["nodes"][1]["callFrame"]["lineNumber"] = -2
    else:
        p["nodes"][3]["children"] = [1]
    assert "error" in invoke(project, capsys, "runtime", data, status=2)


def test_verified_source_ranges_and_stale_or_ignored_attributions(project, capsys):
    data = capture("runtime")
    target = project / "backend/src/core/features/products/product.service.ts"
    location = {
        "path": target.relative_to(project).as_posix(),
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "line": 1,
        "end_line": 2,
    }
    data["sources"] = [{"id": "2", "location": location}]
    result = invoke(project, capsys, "runtime", data, "--strict")["data"]
    row = next(n for n in result["cpu"]["call_tree"] if n["node_id"] == 2)
    assert row["source"]["line"] == 1 and row["source"]["end_line"] == 2
    location["sha256"] = "a" * 64
    result = invoke(project, capsys, "runtime", data, "--strict", status=1)["data"]
    row = next(n for n in result["cpu"]["call_tree"] if n["node_id"] == 2)
    assert row["source"] == {"location_status": "unverified"}
    location["path"] = ".env"
    invoke(project, capsys, "runtime", data, "--strict", status=1)


def test_heap_dominators_shared_cycles_weak_edges_and_private_values(project, capsys):
    result = invoke(project, capsys, "memory", capture("memory"), "--strict")["data"]
    graph = result["heap"]
    assert graph["node_count"] == 6 and graph["edge_count"] == 7
    assert graph["self_bytes"] == 150 and graph["strong_reachable_nodes"] == 5
    assert graph["unreachable_nodes"] == 1 and graph["excluded_or_unsupported_edges"] == 1
    nodes = {row["node_id"]: row for row in graph["top_retained"]}
    assert nodes[7]["retained_bytes_estimate"] == 70
    assert nodes[7]["immediate_dominator_id"] == 1  # two owners, neither dominates Shared
    assert nodes[3]["retained_bytes_estimate"] == 10 and nodes[5]["retained_bytes_estimate"] == 20
    assert nodes[9]["retained_bytes_estimate"] == 40 and nodes[9]["immediate_dominator_id"] == 7
    assert nodes[9]["retaining_path"][0]["node_id"] == 1
    assert nodes[9]["retaining_path"][-1]["node_id"] == 9
    assert result["process"]["post_gc_growth_bytes_per_second"] == pytest.approx(1250)
    assert result["findings"][0]["rule"] == "post_gc_growth"


def test_heap_layout_accepts_v8_unused_trailing_type_descriptors(project, capsys):
    data = capture("memory")
    data["heap_snapshot"]["snapshot"]["meta"]["node_types"].append("number")
    result = invoke(project, capsys, "memory", data, "--strict")["data"]
    assert result["heap"]["retained_status"] == "estimated"
    assert result["heap"]["node_count"] == 6


def test_heap_dominators_match_independent_fixed_point_on_cyclic_graphs():
    rng = random.Random(122)
    for _ in range(40):
        count = 20
        graph = [[(node + 1, "property")] if node + 1 < count else [] for node in range(count)]
        for node in range(count):
            graph[node].extend((rng.randrange(count), "property") for _ in range(3))
        predecessors = [[] for _ in graph]
        for node, edges in enumerate(graph):
            for target, _ in edges:
                predecessors[target].append(node)
        expected = {0: {0}, **{n: set(range(count)) for n in range(1, count)}}
        changed = True
        while changed:
            changed = False
            for node in range(1, count):
                shared = set.intersection(*(expected[p] for p in predecessors[node])) | {node}
                changed |= shared != expected[node]
                expected[node] = shared
        actual = heap.dominators(graph, predecessors, list(range(count)))
        for node in range(1, count):
            strict = expected[node] - {node}
            parent = next(p for p in strict if all(q in expected[p] for q in strict))
            assert actual[node] == parent


def test_heap_dominators_handle_long_chains_without_recursion():
    count = 5000
    predecessors = [[]] + [[n - 1] for n in range(1, count)]
    actual = heap.dominators([], predecessors, list(range(count)))
    assert actual[count - 1] == count - 2


def test_native_heap_and_dominator_budget_omit_unavailable_estimates(project, capsys, monkeypatch):
    native = capture("memory")["heap_snapshot"]
    monkeypatch.setattr(heap, "DOMINATOR_BUDGET", 1)
    result = invoke(
        project, capsys, "memory", native, "--strict", status=1, path="measurements/heap.heapsnapshot"
    )["data"]
    assert result["heap"]["retained_status"] == "unavailable"
    assert all(row["retained_bytes_estimate"] is None for row in result["heap"]["top_retained"])
    assert result["heap"]["self_bytes"] == 150
    assert {u["code"] for u in result["unknowns"]} >= {
        "heap_dominators_unavailable",
        "memory_samples_missing",
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "offset",
        "node_count",
        "edge_count",
        "duplicate_id",
        "string_index",
        "name_index",
        "node_type",
        "root",
        "layout",
        "negative_size",
    ],
)
def test_invalid_heap_references_and_layout_fail(project, capsys, mutation):
    data = capture("memory")
    graph = data["heap_snapshot"]
    if mutation == "offset":
        graph["edges"][2] = 7
    elif mutation == "node_count":
        graph["snapshot"]["node_count"] = 500000
    elif mutation == "edge_count":
        graph["snapshot"]["edge_count"] = 0
    elif mutation == "duplicate_id":
        graph["nodes"][7] = 1
    elif mutation == "string_index":
        graph["nodes"][6] = 999
    elif mutation == "name_index":
        graph["edges"][1] = 999
    elif mutation == "node_type":
        graph["nodes"][5] = 999
    elif mutation == "root":
        graph["nodes"][0] = 1
    elif mutation == "layout":
        graph["snapshot"]["meta"]["node_fields"][1] = "type"
    else:
        graph["nodes"][8] = -1
    invoke(project, capsys, "memory", data, status=2)


def test_memory_counters_are_not_added_and_short_or_unlabelled_series_are_unknown(project, capsys):
    data = capture("memory")
    for row in data["process_samples"]:
        row["post_gc"] = False
    result = invoke(project, capsys, "memory", data, "--strict", status=1)["data"]
    trends = {r["metric"]: r for r in result["process"]["trends"]}
    assert trends["rss_bytes"]["last"] == 500 and trends["external_bytes"]["last"] == 50
    assert result["process"]["post_gc_growth_bytes_per_second"] is None
    assert not result["findings"]
    data["process_samples"][1]["at_ms"] = 0
    invoke(project, capsys, "memory", data, status=2)


def test_cache_deltas_hit_denominator_latency_and_capacity(project, capsys):
    result = invoke(project, capsys, "cache", capture("cache"), "--strict")["data"]
    row = result["caches"][0]
    assert row["counters"]["hits"] == 90 and row["counters"]["misses"] == 30
    assert row["lookup_count"] == 120 and row["hit_rate"] == 0.75
    assert row["lookups_per_second"] == 3000 and row["latency"]["mean_ms"] == 2
    assert row["storage"]["byte_growth"] == 200
    assert len(result["findings"]) == 2


@pytest.mark.parametrize("reset", ["epoch", "decrease"])
def test_cache_resets_skip_intervals_and_are_not_zero_activity(project, capsys, reset):
    data = capture("cache")
    if reset == "epoch":
        data["caches"][0]["samples"][-1]["epoch"] = "new-run"
    else:
        data["caches"][0]["samples"][-1]["hits"] = 1
    result = invoke(project, capsys, "cache", data, "--strict", status=1)["data"]
    row = result["caches"][0]
    assert row["excluded_intervals"] == 1 and row["observed_ms"] == 20
    assert row["lookup_count"] == 80 and row["hit_rate"] == 0.75
    assert all(not metric["comparable"] for metric in result["metrics"])


def test_cache_unknown_secondary_counters_and_bytes_remain_null(project, capsys):
    data = capture("cache")
    for row in data["caches"][0]["samples"]:
        row.pop("sets")
        row["errors"] = None
        row["bytes"] = None
    result = invoke(project, capsys, "cache", data, "--strict", status=1)["data"]
    row = result["caches"][0]
    assert row["counters"]["sets"] is None and row["storage"]["byte_growth"] is None
    assert row["hit_rate"] == 0.75


def test_zero_cache_lookups_have_unknown_hit_rate(project, capsys):
    data = capture("cache")
    data["caches"][0].pop("latency")
    for row in data["caches"][0]["samples"]:
        row["hits"] = row["misses"] = 0
    result = invoke(project, capsys, "cache", data, "--strict", status=1)["data"]
    assert result["caches"][0]["hit_rate"] is None
    assert not any(m["id"].startswith("cache.hit_rate") for m in result["metrics"])


@pytest.mark.parametrize("samples", [0, 1])
def test_cache_without_observed_interval_does_not_invent_zero_counts(project, capsys, samples):
    data = capture("cache")
    data["caches"][0]["samples"] = data["caches"][0]["samples"][:samples]
    result = invoke(project, capsys, "cache", data, "--strict", status=1)["data"]
    assert result["caches"][0]["lookup_count"] is None
    assert all(value is None for value in result["caches"][0]["counters"].values())


def test_empty_comparison_never_reports_comparable(project, capsys):
    data = capture("cache")
    data["caches"] = []
    result = compare(project, capsys, data, data, "--strict", status=1)["data"]
    assert result["comparison_status"] == "partial" and not result["comparisons"]


@pytest.mark.parametrize("mutation", ["latency", "order", "negative", "secret", "duplicates"])
def test_invalid_cache_contract_is_rejected(project, capsys, mutation):
    data = capture("cache")
    row = data["caches"][0]
    if mutation == "latency":
        row["latency"]["p95_ms"] = 999
    elif mutation == "order":
        row["samples"][-1]["at_ms"] = 1
    elif mutation == "negative":
        row["samples"][0]["hits"] = -1
    elif mutation == "secret":
        row["DO_NOT_PRINT"] = "DO_NOT_PRINT"
    else:
        data["caches"].append(copy.deepcopy(row))
    invoke(project, capsys, "cache", data, status=2)


@pytest.mark.parametrize("kind", ["runtime", "memory", "cache"])
def test_comparison_of_identical_compatible_captures_has_no_regressions(project, capsys, kind):
    data = capture(kind)
    result = compare(project, capsys, data, data, "--strict", "--fail-on-regression")["data"]
    assert result["comparison_status"] == "comparable" and result["summary"]["complete"]
    assert result["summary"]["regression_candidates"] == 0
    assert result["comparisons"] and all(row["delta"] == 0 for row in result["comparisons"])


def test_runtime_comparison_threshold_and_opt_in_gate(project, capsys):
    before, after = capture("runtime"), capture("runtime")
    after["cpu_profile"]["timeDeltas"] = [15000, 10000, 5000, 10000]
    result = compare(project, capsys, before, after)["data"]
    row = next(r for r in result["comparisons"] if r["id"] == "cpu.sampled_non_idle_ms")
    assert row["unit"] == "ms/second" and row["before"] == 750 and row["after"] == 875
    assert row["regression_candidate"]
    compare(project, capsys, before, after, "--fail-on-regression", status=1)
    result = compare(project, capsys, before, after, "--regression-percent", "99", "--fail-on-regression")[
        "data"
    ]
    assert result["summary"]["regression_candidates"] == 0


@pytest.mark.parametrize("kind", ["runtime", "memory"])
def test_partial_windows_are_reported_but_not_compared(project, capsys, kind):
    before, after = capture(kind), capture(kind)
    if kind == "runtime":
        after["cpu_profile"]["endTime"] += 1000
        after["event_loop"]["window_ms"] += 1
        prefixes = ("cpu.", "event_loop.")
    else:
        after["process_samples"].pop(0)
        prefixes = ("memory.",)
    result = compare(project, capsys, before, after, "--strict", status=1)["data"]
    assert not any(row["id"].startswith(prefixes) for row in result["comparisons"])
    assert any(row["status"] == "incomplete_measurement_window" for row in result["unmatched"])


def test_rate_normalization_and_endpoint_workload_compatibility(project, capsys):
    before, after = capture("runtime"), capture("runtime")
    after["metadata"]["workload"]["duration_ms"] *= 2
    after["cpu_profile"]["endTime"] *= 2
    after["cpu_profile"]["timeDeltas"] = [n * 2 for n in after["cpu_profile"]["timeDeltas"]]
    after["event_loop"]["window_ms"] *= 2
    result = compare(project, capsys, before, after)["data"]
    assert all(row["delta"] == 0 for row in result["comparisons"] if row["id"].startswith("cpu."))
    before, after = capture("memory"), capture("memory")
    after["metadata"]["workload"].update(duration_ms=80, operations=8)
    for sample in after["process_samples"]:
        sample["at_ms"] *= 2
    result = compare(project, capsys, before, after, "--strict", status=1)["data"]
    assert not result["comparisons"]
    assert all(row["status"] == "different_duration_and_operation_count" for row in result["unmatched"])


@pytest.mark.parametrize("threshold", ["nan", "inf", "-1", "1000001"])
def test_invalid_comparison_threshold_is_rejected(project, capsys, threshold):
    data = capture("runtime")
    compare(project, capsys, data, data, "--regression-percent", threshold, status=2)


@pytest.mark.parametrize(
    "field", ["environment", "process_scope", "collector", "runtime", "concurrency", "warmup_ms", "name"]
)
def test_incompatible_metadata_never_emits_numeric_comparisons(project, capsys, field):
    before, after = capture("runtime"), capture("runtime")
    if field in {"concurrency", "warmup_ms", "name"}:
        after["metadata"]["workload"][field] = "other" if field == "name" else 2
    elif field == "runtime":
        after["metadata"]["runtime"]["version"] = "23.0.0"
    else:
        after["metadata"][field] = "other"
    result = compare(project, capsys, before, after, "--strict", status=1)["data"]
    assert result["comparison_status"] == "incompatible" and not result["comparisons"]
    assert result["summary"]["regression_candidates"] == 0


def test_unknown_metadata_unmatched_frames_and_different_metric_boundaries(project, capsys):
    before, after = capture("runtime"), capture("runtime")
    after["metadata"].pop("environment")
    assert compare(project, capsys, before, after)["data"]["comparison_status"] == "incompatible"
    after = capture("runtime")
    after["cpu_profile"]["nodes"][3]["callFrame"]["functionName"] = "newFunction"
    after["event_loop"]["resolution_ms"] = 2
    result = compare(project, capsys, before, after, "--strict", status=1)["data"]
    assert {r["status"] for r in result["unmatched"]} >= {
        "only_before",
        "only_after",
        "different_measurement_boundary",
    }


def test_zero_baseline_and_relative_overflow_stay_finite(project, capsys):
    before, after = capture("runtime"), capture("runtime")
    for field in before["event_loop"]["delay"]:
        if field != "count":
            before["event_loop"]["delay"][field] = 0
    result = compare(project, capsys, before, after, "--fail-on-regression", status=1)["data"]
    row = next(r for r in result["comparisons"] if r["id"] == "event_loop.delay.mean_ms")
    assert row["percent_change"] is None and row["zero_baseline"] and row["regression_candidate"]
    for field in before["event_loop"]["delay"]:
        if field != "count":
            before["event_loop"]["delay"][field] = 1e-310
    before["event_loop"]["delay"]["total_ms"] = 4e-310
    result = compare(project, capsys, before, after)["data"]
    assert any(r["percent_status"] == "numeric_overflow" for r in result["comparisons"])
    assert all(
        r["percent_change"] is None or math.isfinite(r["percent_change"]) for r in result["comparisons"]
    )


def test_database_compare_uses_query_fingerprints_and_keeps_streams_separate(project, capsys):
    from test_performance import capture as database_capture

    before = database_capture(project)
    before["metadata"].update(environment="test", process_scope="backend", collector="prisma-v1")
    before["metadata"]["workload"]["warmup_ms"] = 0
    after = copy.deepcopy(before)
    after["queries"][0]["timing"].update(total_ms=60, min_ms=20, max_ms=20)
    for event in after["queries"][0]["samples"]:
        event["duration_ms"] = 20
    result = compare(project, capsys, before, after, "--fail-on-regression", status=1)["data"]
    assert result["kind"] == "database" and result["summary"]["compared_metrics"] == 3
    assert next(r for r in result["comparisons"] if "mean_ms" in r["id"])["percent_change"] == 100


@pytest.mark.parametrize("kind", ["runtime", "memory", "cache"])
def test_summaries_and_counts_survive_output_truncation(project, capsys, kind):
    data = capture(kind)
    full = invoke(project, capsys, kind, data)
    bounded = invoke(project, capsys, kind, data, "--max-results", "1", budget=2048)
    assert bounded["truncated"] and bounded["data"]["summary"] == full["data"]["summary"]
    assert bounded["coverage"]["counts"] == full["coverage"]["counts"]


@pytest.mark.parametrize(
    "payload",
    [b'{"capture_version":"2.0","capture_version":"2.0"}', b'{"x":NaN}', b"\xff", b"[" * 70 + b"]" * 70],
)
def test_json_duplicates_nonfinite_encoding_and_depth_are_errors(project, capsys, payload):
    (project / "invalid.json").write_bytes(payload)
    invoke(project, capsys, "runtime", status=2, path="invalid.json")


def test_limits_fail_before_decode_and_outside_or_ignored_inputs_are_refused(project, capsys, monkeypatch):
    path = project / "large.json"
    path.write_bytes(b"[" + b"0," * 100 + b"0]")
    monkeypatch.setattr(evidence, "MAX_VALUES", 10)
    with pytest.raises(DevkitError, match="structure exceeds"):
        evidence.load(Project(project), "large.json")
    monkeypatch.setattr(evidence, "MAX_VALUES", 4_000_000)
    invoke(project, capsys, "runtime", capture("runtime"), status=2, path="node_modules/capture.json")
    invoke(project, capsys, "runtime", status=2, path="../outside.json")


def test_mismatched_command_kind_and_apps_are_rejected(project, capsys):
    invoke(project, capsys, "runtime", capture("cache"), status=2)
    data = capture("runtime")
    data["metadata"]["app"] = "hub"
    invoke(project, capsys, "runtime", data, status=2)
    compare(project, capsys, capture("runtime"), capture("memory"), status=2)
