"""Performance reports require evidence; they never collect it or infer missing measurements."""

import copy
import hashlib
import json
import os
import socket
import subprocess
from pathlib import Path

import pytest

from devkit.cli import main
from devkit.performance import capture as capture_module
from devkit.performance import database as database_module
from devkit.project import DevkitError, Project


def source(root, name="backend/src/core/features/products/product.service.ts"):
    return {
        "path": name,
        "sha256": hashlib.sha256((root / name).read_bytes()).hexdigest(),
        "line": 1,
        "end_line": 1,
    }


def capture(root):
    return {
        "capture_version": "1.0",
        "metadata": {
            "app": "erp",
            "captured_at": "2026-10-14T10:00:00Z",
            "database": {"engine": "mysql", "version": "8.0.36"},
            "workload": {"name": "synthetic-test", "duration_ms": 1000, "concurrency": 1},
            "runtime": {"node": "20.19.3", "prisma_client": "5.22.0"},
        },
        "queries": [
            {
                "id": "q1",
                "kind": "sql",
                "fingerprint": "a" * 64,
                "operation": "select",
                "tables": ["products"],
                "model": "Product",
                "timing": {
                    "source": "prisma_query_event",
                    "count": 3,
                    "total_ms": 30,
                    "min_ms": 10,
                    "max_ms": 10,
                },
                "samples": [
                    {
                        "id": f"sql-{n}",
                        "duration_ms": 10,
                        "phase": "request",
                        "correlation": "explicit",
                        "request_id": "r1",
                    }
                    for n in range(3)
                ],
                "source": source(root),
                "plan": {
                    "format": "mysql_explain_json",
                    "document": {
                        "query_block": {
                            "table": {
                                "table_name": "products",
                                "access_type": "ALL",
                                "rows_examined_per_scan": 10000,
                                "rows_produced_per_join": 100,
                                "filtered": 1,
                            }
                        }
                    },
                },
            }
        ],
        "requests": [
            {"id": "r1", "duration_ms": 25, "outcome": "finished", "route_template": "/api/products"}
        ],
        "indexes_complete": True,
        "indexes": [
            {
                "table": "products",
                "name": "PRIMARY",
                "provenance": "database",
                "unique": True,
                "primary": True,
                "parts": [{"column": "id", "order": "asc"}],
                "method": "btree",
                "visible": True,
            }
        ],
    }


def invoke(
    root, capsys, data=None, *args, status=0, input_path="diagnostics/performance/capture.json", budget=200000
):
    if data is not None:
        path = root / input_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    code = main(
        [
            "--root",
            str(root),
            "performance",
            "database",
            "--input",
            input_path,
            "--json",
            "--app",
            "erp",
            "--max-output-chars",
            str(budget),
            *args,
        ]
    )
    output = capsys.readouterr()
    assert code == status, output.out + output.err
    assert not output.err
    assert len(output.out) <= budget
    return json.loads(output.out)


def test_complete_offline_report_separates_estimates_timings_and_source(project, capsys, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("No network/subprocess/source execution allowed")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    before = {p: p.read_bytes() for p in project.rglob("*.ts")}
    result = invoke(project, capsys, capture(project), "--strict")
    data = result["data"]
    assert data["summary"]["complete"] and result["coverage"]["stages"]["performance"] == "complete"
    assert result["coverage"]["stages"]["source"] == "not_run"
    assert result["coverage"]["counts"]["performance_analyzed"] == 1
    row = data["queries"][0]
    assert row["timing"]["mean_ms"] == 10
    assert row["source"]["line"] == row["source"]["end_line"] == 1
    assert row["source"]["attribution"] == "capture_supplied_not_runtime_verified"
    assert row["mappings"][0]["model"] == "Product"
    assert "products" in row["mappings"][0]["modules"]
    assert row["mappings"][0]["schema_location"]["end_line"] > row["mappings"][0]["schema_location"]["line"]
    assert row["plan"]["tables"][0]["estimated_rows_per_scan"] == 10000
    assert {f["rule"] for f in data["findings"]} == {"scan_review", "repeated_query"}
    assert data["requests"][0]["duration_ms"] == 25  # not the sum (30) of SQL durations
    assert data["requests"][0]["associated_samples"][0]["count"] == 3
    assert all(path.read_bytes() == old for path, old in before.items())
    assert not (project / "devkit/.cache/source.sqlite3").exists()


def test_logical_sql_and_detached_streams_are_not_double_counted(project, capsys):
    data = capture(project)
    logical = copy.deepcopy(data["queries"][0])
    logical.update(
        id="operation",
        kind="prisma",
        operation="create",
        timing={"source": "prisma_operation", "count": 1, "total_ms": 12},
    )
    logical.pop("plan")
    logical["samples"] = [
        {"id": "op1", "duration_ms": 12, "phase": "request", "correlation": "explicit", "request_id": "r1"}
    ]
    data["queries"].append(logical)
    for event in data["queries"][0]["samples"]:
        event.update(parent_id="op1", phase="detached")
    result = invoke(project, capsys, data)["data"]
    assert not any(f["rule"] == "repeated_query" for f in result["findings"])
    assert result["summary"]["sql_groups"] == result["summary"]["prisma_groups"] == 1
    assert [q["rank_within_stream"] for q in result["queries"]] == [1, 1]
    assert len(result["requests"][0]["associated_samples"]) == 2
    assert "total_ms" not in result["summary"]


@pytest.mark.parametrize(
    "correlation,phase,request_id",
    [
        ("unknown", "request", "r1"),
        ("explicit", "unknown", "r1"),
        ("explicit", "request", "missing"),
        ("explicit", "background", None),
    ],
)
def test_repetition_requires_explicit_request_correlation(project, capsys, correlation, phase, request_id):
    data = capture(project)
    for sample in data["queries"][0]["samples"]:
        sample.update(correlation=correlation, phase=phase, request_id=request_id)
    result = invoke(project, capsys, data)["data"]
    assert not any(f["rule"] == "repeated_query" for f in result["findings"])
    assert result["queries"][0]["correlated_samples"] == 0


def test_missing_evidence_remains_partial_and_strict_fails(project, capsys):
    data = capture(project)
    row = data["queries"][0]
    for key in ("plan", "samples", "source"):
        row.pop(key)
    row["timing"] = {"source": "unknown", "count": 10, "total_ms": None}
    data.pop("requests")
    data.pop("indexes_complete")
    data["metadata"]["database"]["version"] = None
    result = invoke(project, capsys, data)
    assert result["coverage"]["status"] == "partial"
    assert result["data"]["queries"][0]["rank_within_stream"] is None
    assert result["data"]["queries"][0]["timing"]["mean_ms"] is None
    invoke(project, capsys, data, "--strict", status=1)


@pytest.mark.parametrize("change", ["hash", "lines", "missing", "excluded", "app", "budget"])
def test_unverified_source_never_emits_claimed_line_ranges(project, capsys, monkeypatch, change):
    data = capture(project)
    row = data["queries"][0]["source"]
    if change == "hash":
        row["sha256"] = "0" * 64
    elif change == "lines":
        row["end_line"] = 9999
    elif change == "missing":
        row["path"] = "backend/src/missing.ts"
    elif change == "excluded":
        row["path"] = ".env"
    elif change == "app":
        row["path"] = "hub/index.ts"
        (project / row["path"]).write_text("export const x = 1;")
    else:
        monkeypatch.setattr(database_module, "MAX_SOURCE_TOTAL", 1)
    result = invoke(project, capsys, data)["data"]["queries"][0]["source"]
    assert result == {"location_status": "unverified"}


def test_windows_relative_source_paths_and_crlf_hash(project, capsys):
    data = capture(project)
    path = project / data["queries"][0]["source"]["path"]
    path.write_bytes(b"\xef\xbb\xbfexport function f() {\r\n return 1;\r\n}\r\n")
    data["queries"][0]["source"] = {**source(project), "end_line": 3}
    data["queries"][0]["source"]["path"] = data["queries"][0]["source"]["path"].replace("/", "\\")
    result = invoke(project, capsys, data)["data"]["queries"][0]["source"]
    assert result["location_status"] == "content_verified" and result["end_line"] == 3
    assert "\\" not in result["path"]


@pytest.mark.parametrize(
    "engine,version,format",
    [
        ("mariadb", "10.11.0", "mysql_explain_json"),
        ("mysql", None, "mysql_explain_json"),
        ("mysql", "9.1.0", "mysql_explain_json"),
        ("mysql", "8.0.36", "mysql_explain_analyze"),
        ("mysql", "8.4.0-MariaDB", "mysql_explain_json"),
    ],
)
def test_unsupported_plan_engines_and_versions(project, capsys, engine, version, format):
    data = capture(project)
    data["metadata"]["database"] = {"engine": engine, "version": version}
    data["queries"][0]["plan"]["format"] = format
    result = invoke(project, capsys, data)
    assert result["data"]["queries"][0]["plan"]["status"] == "unsupported"
    assert result["coverage"]["status"] == "partial"
    assert result["data"]["summary"]["plans_processed"] == 0


def test_plan_projects_safe_fields_and_handles_aliases(project, capsys):
    data = capture(project)
    plan = data["queries"][0]["plan"]
    table = plan["document"]["query_block"]["table"]
    table.update(
        table_name="p",
        attached_condition="password = 'DO_NOT_PRINT'",
        access_type="ref",
        key="idx_org",
        used_key_parts=["organization_id"],
    )
    plan["document"]["query_block"] = {
        "ordering_operation": {"using_filesort": True, "using_temporary_table": True, "table": table}
    }
    result = invoke(project, capsys, data)
    assert "DO_NOT_PRINT" not in json.dumps(result)
    assert result["data"]["queries"][0]["plan"]["status"] == "partial"
    plan["table_map"] = {"p": "products"}
    result = invoke(project, capsys, data)
    assert result["data"]["queries"][0]["plan"]["tables"][0]["table"] == "products"
    assert result["data"]["queries"][0]["plan"]["status"] == "processed"
    assert "DO_NOT_PRINT" not in json.dumps(result)
    assert {f["rule"] for f in result["data"]["findings"]} >= {
        "plan_using_filesort",
        "plan_using_temporary_table",
    }


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"query_block": {}},
        {"query_block": {"table": {"table_name": "products", "access_type": ["ALL"], "filtered": "secret"}}},
    ],
)
def test_unrecognized_plan_is_unknown_not_success(project, capsys, document):
    data = capture(project)
    data["queries"][0]["plan"]["document"] = document
    result = invoke(project, capsys, data)
    assert result["coverage"]["status"] == "partial"
    assert "secret" not in json.dumps(result)


def test_functional_unique_and_unknown_constraint_indexes_are_protected(project, capsys):
    data = capture(project)
    simple = {
        "table": "products",
        "name": "idx1",
        "provenance": "database",
        "unique": False,
        "primary": False,
        "constraint": False,
        "method": "btree",
        "visible": True,
        "parts": [{"column": "organization_id", "order": "asc"}],
    }
    data["indexes"] += [simple, {**simple, "name": "idx2"}]
    for category, override in [
        ("functional", {"parts": [{"expression": True}]}),
        ("unique", {"unique": True}),
        ("constraint", {"constraint": True}),
        ("unknown", {"constraint": None}),
        ("invisible", {"visible": False}),
        ("migration", {"provenance": "migration", "source": source(project)}),
    ]:
        data["indexes"] += [{**simple, **override, "name": category + str(i)} for i in range(2)]
    result = invoke(project, capsys, data)["data"]
    duplicates = [f for f in result["findings"] if f["rule"] == "equivalent_index_definitions"]
    assert len(duplicates) == 1 and duplicates[0]["indexes"] == ["idx1", "idx2"]
    assert any(i["provenance"] == "migration" for i in result["indexes"]["captured"])
    assert any(
        i["provenance"] == "prisma" and i["columns"] == ["organization_id"]
        for i in result["indexes"]["prisma_declared"]
    )
    for index in result["indexes"]["prisma_declared"]:
        assert index["source"]["line"] <= index["source"]["end_line"]


def test_prisma_index_field_options_are_not_column_names(project, capsys):
    path = project / "backend/prisma/schema.prisma"
    path.write_text(
        path.read_text().replace(
            "@@index([organizationId])", "@@index([organizationId(sort: Desc), name(length: 20)])"
        )
    )
    result = invoke(project, capsys, capture(project))["data"]
    index = next(
        i for i in result["indexes"]["prisma_declared"] if i.get("fields") == ["organizationId", "name"]
    )
    assert index["columns"] == ["organization_id", "name"]


@pytest.mark.parametrize(
    "change",
    [
        "version",
        "unknown_field",
        "parameters",
        "negative",
        "nan",
        "bool",
        "missing",
        "duplicate_query",
        "duplicate_event",
        "cycle",
        "bad_total",
        "too_many_samples",
        "parent_request",
        "path_escape",
        "drive",
        "metadata",
        "too_large_integer",
    ],
)
def test_invalid_capture_fails_without_echoing_payload(project, capsys, change):
    data = capture(project)
    row = data["queries"][0]
    if change == "version":
        data["capture_version"] = "2.0"
    elif change == "unknown_field":
        data["DO_NOT_PRINT"] = "DO_NOT_PRINT"
    elif change == "parameters":
        row["params"] = "DO_NOT_PRINT"
    elif change == "negative":
        row["timing"]["total_ms"] = -1
    elif change == "nan":
        row["timing"]["total_ms"] = float("nan")
    elif change == "bool":
        row["timing"]["count"] = True
    elif change == "missing":
        row.pop("timing")
    elif change == "duplicate_query":
        data["queries"].append(copy.deepcopy(row))
    elif change == "duplicate_event":
        row["samples"][1]["id"] = row["samples"][0]["id"]
    elif change == "cycle":
        row["samples"][0]["parent_id"] = row["samples"][1]["id"]
        row["samples"][1]["parent_id"] = row["samples"][0]["id"]
    elif change == "bad_total":
        row["timing"]["total_ms"] = 50
    elif change == "too_many_samples":
        row["timing"]["count"] = 1
    elif change == "parent_request":
        row["samples"][0].update(parent_id=row["samples"][1]["id"], request_id="r2")
    elif change in {"path_escape", "drive"}:
        row["source"]["path"] = "../../DO_NOT_PRINT" if change == "path_escape" else "C:\\DO_NOT_PRINT"
    elif change == "metadata":
        data["metadata"] = []
    else:
        row["timing"]["count"] = 10**500
    result = invoke(project, capsys, data, status=2)
    assert result["error"]["code"] == 2 and "DO_NOT_PRINT" not in json.dumps(result)


@pytest.mark.parametrize(
    "payload",
    [
        b"not JSON DO_NOT_PRINT",
        b'{"capture_version":"1.0","capture_version":"2.0"}',
        b"\xffDO_NOT_PRINT",
        b"[" * 1500 + b"]" * 1500,
    ],
)
def test_invalid_json_encoding_duplicates_and_depth(project, capsys, payload):
    path = project / "capture.json"
    path.write_bytes(payload)
    result = invoke(project, capsys, status=2, input_path="capture.json")
    assert "DO_NOT_PRINT" not in json.dumps(result)


def test_byte_and_structural_limits(project, capsys, monkeypatch):
    data = capture(project)
    monkeypatch.setattr(capture_module, "MAX_BYTES", 20)
    invoke(project, capsys, data, status=2)
    monkeypatch.setattr(capture_module, "MAX_BYTES", 16 * 1024 * 1024)
    monkeypatch.setattr(capture_module, "MAX_NODES", 20)
    invoke(project, capsys, data, status=2)


def test_input_cannot_escape_ignore_or_internal_storage(project, capsys):
    invoke(project, capsys, status=2, input_path="../outside.json")
    invoke(project, capsys, capture(project), status=2, input_path="node_modules/capture.json")
    invoke(project, capsys, capture(project), status=2, input_path="devkit/.feedback/capture.json")


def test_input_symlinks_rejected(project, capsys):
    original = project / "original.json"
    original.write_text(json.dumps(capture(project)))
    linked = project / "linked.json"
    try:
        linked.symlink_to(original)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink privileges unavailable")
    invoke(project, capsys, status=2, input_path="linked.json")


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO")
def test_fifo_does_not_block(project, capsys):
    os.mkfifo(project / "capture.json")
    invoke(project, capsys, status=2, input_path="capture.json")


def test_app_mismatch_and_empty_capture(project, capsys):
    data = capture(project)
    data["metadata"]["app"] = "hub"
    invoke(project, capsys, data, status=2)
    data["metadata"]["app"] = "erp"
    data["queries"] = []
    result = invoke(project, capsys, data, "--strict", status=1)
    assert not result["data"]["summary"]["complete"]
    assert result["coverage"]["counts"]["performance_analyzed"] == 0


def test_full_summary_survives_collection_and_character_limits(project, capsys):
    data = capture(project)
    for i in range(1, 20):
        row = copy.deepcopy(data["queries"][0])
        row.update(id=f"q{i + 1}", fingerprint=hashlib.sha256(str(i).encode()).hexdigest())
        for e in row["samples"]:
            e["id"] += f"-{i}"
        data["queries"].append(row)
    expected = invoke(project, capsys, data)["data"]["summary"]
    result = invoke(project, capsys, data, "--max-results", "1", budget=3200)
    assert result["truncated"] and result["coverage"]["output_truncated"]
    assert result["data"]["summary"] == expected
    assert result["coverage"]["counts"]["performance_analyzed"] == 20


def test_help_catalog_and_text_output(project, capsys):
    assert main(["help", "performance", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert any(r["command"] == "devkit performance database" for r in result["data"])
    assert main(["performance", "--json"]) == 0
    capsys.readouterr()
    invoke(project, capsys, capture(project))
    assert (
        main(
            [
                "--root",
                str(project),
                "performance",
                "database",
                "--input",
                "diagnostics/performance/capture.json",
            ]
        )
        == 0
    )
    output = capsys.readouterr()
    assert "performance: database" in output.out and "Coverage:" in output.err


def test_example_capture_contract():
    example = Path(__file__).resolve().parents[1] / "examples/performance-capture.json"
    capture_module.validate(json.loads(example.read_text()))


def test_native_mysql_decimal_strings_and_nested_plans(project, capsys):
    data = capture(project)
    data["metadata"]["database"]["version"] = "8.4.0"
    node = data["queries"][0]["plan"]["document"]["query_block"]["table"]
    node.update(filtered="12.50", possible_keys=["PRIMARY"], cost_info={"read_cost": "999.00"})
    data["queries"][0]["plan"]["document"] = {"query_block": {"nested_loop": [{"table": node}]}}
    result = invoke(project, capsys, data, "--strict")["data"]
    assert result["queries"][0]["plan"]["tables"][0]["estimated_filtered_percent"] == 12.5
    assert result["queries"][0]["plan"]["tables"][0]["possible_keys"] == ["PRIMARY"]
    assert "999.00" not in json.dumps(result)


@pytest.mark.parametrize(
    "field,value",
    [
        ("filtered", 101),
        ("key", "intersect(idx1,idx2)"),
        ("used_key_parts", "DO_NOT_PRINT"),
        ("possible_keys", ["DO NOT PRINT"]),
    ],
)
def test_unsupported_plan_fields_are_partial(project, capsys, field, value):
    data = capture(project)
    data["queries"][0]["plan"]["document"]["query_block"]["table"][field] = value
    result = invoke(project, capsys, data, "--strict", status=1)
    assert result["data"]["queries"][0]["plan"]["status"] == "partial"
    assert "DO_NOT_PRINT" not in json.dumps(result)


def test_scope_is_capture_app_even_when_flag_is_all(project, capsys):
    path = project / "hub/schema.prisma"
    path.write_text('model Other { id Int @id @@map("products") }')
    result = invoke(project, capsys, capture(project), "--app", "all")["data"]
    assert result["queries"][0]["mappings"][0]["model"] == "Product"
    assert all(index.get("model") != "Other" for index in result["indexes"]["prisma_declared"])


def test_hub_mapping_and_ambiguous_schema(project, capsys):
    path = project / "hub/schema.prisma"
    path.write_text('model HubProduct { id Int @id @@map("products") }')
    service = project / "hub/service.ts"
    service.write_text("export const hub = 1;")
    data = capture(project)
    data["metadata"]["app"] = "hub"
    data["queries"][0]["source"] = source(project, "hub/service.ts")
    data["queries"][0].pop("model")
    result = invoke(project, capsys, data, "--app", "hub")["data"]
    assert result["queries"][0]["mappings"][0]["model"] == "HubProduct"
    path.write_text(path.read_text() + '\nmodel Duplicate { id Int @id @@map("products") }')
    result = invoke(project, capsys, data, "--app", "hub", "--strict", status=1)["data"]
    assert result["queries"][0]["mappings"] == []


def test_minimum_output_budget_preserves_valid_report_and_summary(project, capsys):
    data = capture(project)
    result = invoke(project, capsys, data, budget=2048)
    assert result["truncated"] and result["data"]["summary"]["query_groups"] == 1


def test_nested_checkout_uses_explicit_project_root(project, capsys, monkeypatch):
    target = project / ".agents/devkit"
    target.mkdir(parents=True)
    monkeypatch.chdir(target)
    result = invoke(project, capsys, capture(project))
    assert result["root"] == str(project)
    assert result["data"]["queries"][0]["source"]["location_status"] == "content_verified"


def test_preflight_bounds_graph_before_json_allocation(project, monkeypatch):
    path = project / "capture.json"
    path.write_bytes(b"[" + b"0," * 100 + b"0]")
    context = Project(project)
    monkeypatch.setattr(capture_module, "MAX_NODES", 10)

    def forbidden(*args, **kwargs):
        raise AssertionError("Oversized graphs must fail before allocating decoded objects")

    monkeypatch.setattr(capture_module.json, "loads", forbidden)
    with pytest.raises(DevkitError, match="structure exceeds"):
        capture_module.load(context, "capture.json")


def test_preflight_does_not_count_escaped_string_punctuation():
    capture_module.preflight(json.dumps({"ignored": '[,::]"\\' * 50000}).encode())


def test_query_findings_carry_verified_source_ranges(project, capsys):
    result = invoke(project, capsys, capture(project))["data"]
    for finding in result["findings"]:
        assert finding["query_id"] == "q1"
        assert finding["source"]["line"] == finding["source"]["end_line"] == 1


def test_unknown_parent_and_source_bom_capture(project, capsys):
    data = capture(project)
    data["queries"][0]["samples"][0]["parent_id"] = "not-captured"
    path = project / "capture.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(data).encode())
    result = invoke(project, capsys, input_path="capture.json")
    assert result["coverage"]["gaps"]["performance_parent_unresolved"] == 1
