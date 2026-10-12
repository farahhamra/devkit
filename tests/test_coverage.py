from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from devkit.cli import main
from devkit.project import Project


def invoke(root, capsys, *args, status=0):
    actual = main(["--root", str(root), *args, "--json"])
    captured = capsys.readouterr()
    assert actual == status, captured.out + captured.err
    assert not captured.err
    return json.loads(captured.out)


@pytest.mark.parametrize(
    "command",
    [
        ("search", "missing-needle", "notes.txt"),
        ("find", "missing-needle"),
        ("tree", "frontend"),
        ("symbols", "backend/src/app.ts"),
        ("module", "list"),
        ("module", "inspect", "products"),
        ("module", "impact", "products"),
        ("module", "check", "products"),
        ("module", "check", "all"),
        ("schema", "list"),
        ("schema", "inspect", "Product"),
        ("schema", "relations", "Product"),
        ("trace", "route", "/api/products"),
        ("trace", "symbol", "ProductService"),
        ("impact", "model", "Product"),
        ("impact", "field", "Product.name"),
        ("impact", "file", "backend/src/app.ts"),
    ],
)
def test_every_analysis_command_exposes_the_same_contract(project, capsys, command):
    result = invoke(project, capsys, *command)
    coverage = result["coverage"]
    assert result["schema_version"] == coverage["version"] == "1.0"
    assert coverage["status"] in {"complete", "partial", "not_analyzed"}
    assert set(coverage["stages"]) == {"discovery", "text", "source", "schema", "dependencies"}
    assert coverage["counts"]["files"] >= 1
    assert coverage["output_truncated"] == result["truncated"]
    assert "percentage" not in coverage


@pytest.mark.parametrize(
    "command",
    [
        ("help",),
        ("config", "inspect"),
        ("config", "validate"),
        ("doctor",),
        ("feedback", "list"),
        ("agents", "sync", "--dry-run"),
    ],
)
def test_non_analysis_commands_do_not_claim_source_coverage(project, capsys, command):
    assert "coverage" not in invoke(project, capsys, *command)


def test_path_discovery_is_not_content_or_ast_analysis(project, capsys):
    coverage = invoke(project, capsys, "find", "not-found")["coverage"]
    assert coverage["status"] == "complete"
    assert coverage["stages"] == {
        "discovery": "complete",
        "text": "not_run",
        "source": "not_run",
        "schema": "not_run",
        "dependencies": "not_run",
    }
    assert coverage["counts"]["text_read"] == coverage["counts"]["source_analyzed"] == 0
    assert coverage["exclusions"]["ignore_rule"] >= 2
    assert next(a for a in coverage["details"]["apps"] if a["app"] == "manager")["configured"] is False


def test_symbols_stays_targeted_and_does_not_claim_dependency_resolution(project, capsys, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Targeted coverage must not enumerate the project")

    monkeypatch.setattr(Project, "files", forbidden)
    coverage = invoke(project, capsys, "symbols", "backend/src/app.ts")["coverage"]
    assert coverage["status"] == "complete"
    assert (
        coverage["counts"]["files"]
        == coverage["counts"]["source_selected"]
        == coverage["counts"]["source_analyzed"]
        == 1
    )
    assert coverage["stages"]["schema"] == coverage["stages"]["dependencies"] == "not_run"
    assert coverage["details"]["scope"]["path"] == "backend/src/app.ts"


def test_empty_results_do_not_imply_that_inputs_were_analyzed(project, capsys):
    searched = invoke(project, capsys, "search", "not-found", "notes.txt")
    assert searched["data"] == [] and searched["coverage"]["status"] == "complete"
    symbols = invoke(project, capsys, "symbols", "notes.txt")
    assert symbols["data"] == [] and symbols["coverage"]["status"] == "not_analyzed"
    assert symbols["coverage"]["exclusions"]["not_source_language"] == 1
    ignored = invoke(project, capsys, "search", "secret", ".env")
    assert ignored["data"] == [] and ignored["coverage"]["status"] == "partial"
    assert ignored["coverage"]["gaps"]["requested_path_excluded"] == 1


def test_unsupported_manager_language_is_not_reported_as_analyzed(project, capsys):
    (project / "hub/Main.cs").write_text("public class Main {}", encoding="utf-8")
    coverage = invoke(project, capsys, "symbols", "hub/Main.cs", "--app", "hub")["coverage"]
    assert coverage["status"] == "not_analyzed"
    assert coverage["counts"]["source_analyzed"] == 0
    impact = invoke(project, capsys, "impact", "file", "hub/Main.cs")["coverage"]
    assert impact["status"] == "partial" and impact["gaps"]["unsupported_impact_target"] == 1


@pytest.mark.parametrize(
    "content,reason", [(b"binary\x00secret", "binary_content"), (b"\xffsecret", "non_utf8")]
)
def test_skipped_text_and_source_are_counted_without_dumping_content(project, capsys, content, reason):
    path = project / "backend/src/unreadable.ts"
    path.write_bytes(content)
    for command in [
        ("search", "secret", "backend/src/unreadable.ts"),
        ("symbols", "backend/src/unreadable.ts"),
    ]:
        result = invoke(project, capsys, *command)
        coverage = result["coverage"]
        assert result["data"] == [] and coverage["status"] == "partial"
        assert coverage["counts"]["text_skipped"] == coverage["gaps"][reason] == 1
        assert "secret" not in json.dumps(coverage)


def test_read_and_traversal_failures_are_measured(project, capsys, monkeypatch):
    original = Path.open

    def denied(path, *args, **kwargs):
        if path.name == "notes.txt":
            raise PermissionError(13, "Permission denied", str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    coverage = invoke(project, capsys, "search", "needle", "notes.txt")["coverage"]
    assert coverage["gaps"]["read_error"] == 1

    def failed_walk(target, *, followlinks, onerror):
        onerror(PermissionError(13, "Permission denied", str(target)))
        return iter(())

    monkeypatch.setattr(os, "walk", failed_walk)
    coverage = invoke(project, capsys, "find", "anything", "frontend")["coverage"]
    assert coverage["status"] == "partial" and coverage["gaps"]["traversal_error"] == 1


def test_search_early_stop_differs_from_display_only_truncation(project, capsys):
    early = invoke(project, capsys, "search", "token", "notes.txt", "--max-results", "1")["coverage"]
    assert early["status"] == "partial" and early["gaps"]["search_result_limit"] == 1
    assert early["output_truncated"]
    listing = invoke(project, capsys, "find", "products", "--max-results", "1")["coverage"]
    assert listing["status"] == "complete" and listing["output_truncated"]
    assert not listing["gaps"]


def test_glob_filters_and_repeated_tree_walks_do_not_double_count(project, capsys):
    coverage = invoke(project, capsys, "search", "not-found", "--glob", "*.txt")["coverage"]
    assert coverage["counts"]["text_read"] == 1
    assert coverage["exclusions"]["glob_filter"] > 0
    found = invoke(project, capsys, "find", "anything", "--type", "all")["coverage"]
    tree = invoke(project, capsys, "tree", "--depth", "2")["coverage"]
    assert found["counts"]["files"] == tree["counts"]["files"]
    assert found["counts"]["directories"] == tree["counts"]["directories"]
    assert found["exclusions"] == tree["exclusions"]


def test_warm_cache_replays_parser_and_dependency_gaps(project, capsys):
    (project / "backend/src/broken.ts").write_text("export const broken = ;", encoding="utf-8")
    (project / "backend/src/dynamic.ts").write_text("import(pathFromRuntime);", encoding="utf-8")
    (project / "backend/src/missing.ts").write_text("import x from './gone';", encoding="utf-8")
    cold = invoke(project, capsys, "trace", "symbol", "not-found")["coverage"]
    warm = invoke(project, capsys, "trace", "symbol", "not-found")["coverage"]
    uncached = invoke(project, capsys, "trace", "symbol", "not-found", "--no-cache")["coverage"]
    for measured in (cold, warm, uncached):
        assert measured["status"] == "partial"
        assert (
            measured["gaps"]["source_parse_error"]
            == measured["gaps"]["dynamic_import"]
            == measured["gaps"]["unresolved_local_import"]
            == 1
        )
        assert measured["counts"] == cold["counts"]
        assert measured["stages"] == cold["stages"]
    assert cold["details"]["cache"]["source_cached"] == 0
    assert warm["details"]["cache"]["source_parsed"] == 0
    assert warm["details"]["cache"]["source_cached"] == warm["counts"]["source_analyzed"]


def test_configured_alias_failures_are_local_not_external_packages(project, capsys):
    manifest = project / "devkit/electroerp.yaml"
    manifest.write_text(
        manifest.read_text() + "\nimport_aliases: {'~shared/': backend/src}\n", encoding="utf-8"
    )
    (project / "backend/src/alias.ts").write_text(
        "import x from '~shared/missing'; import y from 'normal-package';", encoding="utf-8"
    )
    coverage = invoke(project, capsys, "trace", "symbol", "x")["coverage"]
    assert coverage["gaps"]["unresolved_local_import"] == 1


def test_route_specific_static_gaps_are_not_inferred_from_warning_text(project, capsys):
    (project / "backend/src/computed.ts").write_text(
        "import express from 'express'; const app = express(); app.get(computedPath, handler);",
        encoding="utf-8",
    )
    coverage = invoke(project, capsys, "trace", "route", "/not-found")["coverage"]
    assert coverage["status"] == "partial"
    assert coverage["gaps"]["nonliteral_route_or_mount"] == 1


def test_missing_configured_roots_are_gaps_but_unselected_apps_are_not(project, capsys):
    manifest = project / "devkit/electroerp.yaml"
    manifest.write_text(
        manifest.read_text().replace("roots: [hub]", "roots: [missing-hub]"), encoding="utf-8"
    )
    coverage = invoke(project, capsys, "find", "not-found")["coverage"]
    assert coverage["status"] == "partial"
    assert coverage["gaps"]["configured_root_missing_or_not_directory"] == 1
    erp = invoke(project, capsys, "find", "not-found", "--app", "erp")["coverage"]
    assert erp["status"] == "complete"
    targeted = invoke(project, capsys, "symbols", "backend/src/app.ts")["coverage"]
    assert targeted["status"] == "complete"
    error = invoke(project, capsys, "symbols", "--app", "manager", status=2)
    assert error["coverage"]["status"] == "not_analyzed"


def test_scope_metadata_uses_the_same_normalized_path_as_discovery(project, capsys):
    manifest = project / "devkit/electroerp.yaml"
    manifest.write_text(
        manifest.read_text().replace("roots: [hub]", "roots: [missing-hub]"), encoding="utf-8"
    )
    coverage = invoke(project, capsys, "symbols", "missing-hub/../backend/src/app.ts")["coverage"]
    assert coverage["status"] == "complete"
    assert not coverage["gaps"]


@pytest.mark.parametrize("failure", ["missing", "ignored", "binary"])
def test_unavailable_schemas_cannot_be_silently_treated_as_empty(project, capsys, failure):
    path = project / "backend/prisma/schema.prisma"
    if failure == "missing":
        path.unlink()
    elif failure == "ignored":
        (project / "devkit/.ignore").write_text("backend/prisma/\n", encoding="utf-8")
    else:
        path.write_bytes(b"\x00private")
    result = invoke(project, capsys, "schema", "list", status=2)
    coverage = result["coverage"]
    assert coverage["status"] == "partial"
    assert coverage["counts"]["schema_selected"] == 1 and coverage["counts"]["schema_analyzed"] == 0
    assert coverage["stages"]["schema"] == "partial"
    assert "private" not in json.dumps(coverage)


def test_missing_one_of_multiple_schemas_keeps_results_but_reports_gap(project, capsys):
    manifest = project / "devkit/electroerp.yaml"
    manifest.write_text(
        manifest.read_text().replace(
            "schemas: [backend/prisma/schema.prisma]",
            "schemas: [backend/prisma/schema.prisma, backend/prisma/absent.prisma]",
        ),
        encoding="utf-8",
    )
    result = invoke(project, capsys, "schema", "list")
    assert result["data"] and result["coverage"]["status"] == "partial"
    assert result["coverage"]["counts"]["schema_selected"] == 2
    assert result["coverage"]["counts"]["schema_analyzed"] == 1


def test_schema_parse_error_preserves_coverage_on_error_envelope(project, capsys):
    (project / "backend/prisma/schema.prisma").write_text("model Broken {", encoding="utf-8")
    coverage = invoke(project, capsys, "schema", "list", status=2)["coverage"]
    assert coverage["status"] == "partial"
    assert coverage["gaps"]["schema_parse_error"] == 1


def test_module_check_reports_which_modules_were_not_checked(project, capsys):
    coverage = invoke(project, capsys, "module", "check", "all")["coverage"]
    selection = coverage["details"]["module_check_selection"]
    assert coverage["counts"]["modules_checked"] == len(selection["checked_modules"])
    assert coverage["counts"]["modules_unavailable"] == len(selection["unavailable_modules_not_checked"])
    assert "restaurant" in selection["unavailable_modules_not_checked"]


@pytest.mark.parametrize("json_mode", [True, False])
def test_coverage_survives_output_budget_with_partial_counts(project, capsys, json_mode):
    for i in range(25):
        (project / f"backend/src/broken{i}.ts").write_text("export const broken = ;", encoding="utf-8")
    args = ["--root", str(project), "symbols", "backend/src", "--max-output-chars", "2048"]
    assert main(args + (["--json"] if json_mode else [])) == 0
    captured = capsys.readouterr()
    assert len(captured.out + captured.err) <= 2048
    if json_mode:
        coverage = json.loads(captured.out)["coverage"]
        assert coverage["status"] == "partial" and coverage["gaps"]["source_parse_error"] == 25
        assert coverage["counts"]["source_partial"] == 25
        assert coverage["output_truncated"] and coverage["details_truncated"]
    else:
        assert "Coverage: partial" in captured.err and "source_parse_error=25" in captured.err


def test_config_failure_reports_not_started_rather_than_zero_match_success(project, capsys):
    (project / "devkit/electroerp.yaml").write_text("apps: [invalid", encoding="utf-8")
    result = invoke(project, capsys, "symbols", status=2)
    assert result["coverage"]["status"] == "not_analyzed"
    assert set(result["coverage"]["stages"].values()) == {"not_run"}


def test_cache_fallback_does_not_itself_make_analysis_partial(project, capsys, monkeypatch):
    import sqlite3

    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("Simulated cache unavailable")

    monkeypatch.setattr(sqlite3, "connect", unavailable)
    result = invoke(project, capsys, "symbols", "backend/src/app.ts")
    assert result["coverage"]["status"] == "complete"
    assert not result["coverage"]["gaps"]
    assert any("cache" in warning.lower() for warning in result["warnings"])


@pytest.mark.parametrize("warm", [False, True])
def test_changed_source_is_a_gap_even_on_cache_hits(project, capsys, monkeypatch, warm):
    from devkit.cache import ParseCache

    if warm:
        invoke(project, capsys, "symbols", "backend/src/app.ts")
    original = ParseCache.stamp
    calls = 0

    def changing(path):
        nonlocal calls
        calls += 1
        return original(path) + ("-changed" if calls > 1 else "")

    monkeypatch.setattr(ParseCache, "stamp", staticmethod(changing))
    coverage = invoke(project, capsys, "symbols", "backend/src/app.ts")["coverage"]
    assert coverage["status"] == "partial"
    assert coverage["gaps"]["source_changed_during_read"] == 1
    assert coverage["details"]["cache"]["source_cached"] == int(warm)


def test_symlink_exclusion_counts_do_not_follow_targets(project, capsys):
    try:
        (project / "backend/src/alias.ts").symlink_to(project / "backend/src/app.ts")
        (project / "backend/src/linked").symlink_to(project / "frontend", target_is_directory=True)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows account lacks symlink privilege")
        raise
    coverage = invoke(project, capsys, "symbols", "backend/src")["coverage"]
    assert coverage["exclusions"]["symlink_or_junction"] == 2


def test_coverage_does_not_scan_excluded_descendants(project, capsys, monkeypatch):
    original = os.walk
    visited = []

    def observed(*args, **kwargs):
        for row in original(*args, **kwargs):
            visited.append(row[0])
            yield row

    monkeypatch.setattr(os, "walk", observed)
    invoke(project, capsys, "find", "no-match")
    assert not any("node_modules" in str(path) for path in visited)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO unavailable")
def test_special_files_are_excluded_without_reading(project, capsys):
    os.mkfifo(project / "backend/src/pipe.ts")
    coverage = invoke(project, capsys, "symbols", "backend/src")["coverage"]
    assert coverage["exclusions"]["non_regular"] == 1
    assert coverage["counts"]["text_skipped"] == 0
