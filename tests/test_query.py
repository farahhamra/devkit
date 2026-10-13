from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from devkit.cli import main
from devkit.project import Project
from devkit.query import Snapshot, engine_path


def invoke(root, capsys, *args, status=0, budget=200000):
    result = main(["--root", str(root), "query", *args, "--json", "--max-output-chars", str(budget)])
    captured = capsys.readouterr()
    assert result == status, captured.out + captured.err
    assert not captured.err
    assert len(captured.out) <= budget
    return json.loads(captured.out)


@pytest.fixture
def engine():
    assert engine_path(), "Install the query extra to run real-engine query tests"
    return engine_path()


def write(root, content, name="query.ts"):
    path = root / "backend/src" / name
    path.write_bytes(content.encode())
    return path.relative_to(root).as_posix()


def check_location(row, source, path):
    content = source.removeprefix("\ufeff").encode()
    start, end = row["start_byte"], row["end_byte"]
    assert row["path"] == path and 0 <= start < end <= len(content)
    assert row["text"] == content[start:end].decode()
    assert row["line"] == content.count(b"\n", 0, start) + 1
    assert row["column"] == start - content.rfind(b"\n", 0, start)
    occupied = end
    if content[end - 1 : end] == b"\n":
        occupied -= 1
        if content[occupied - 1 : occupied] == b"\r":
            occupied -= 1
    assert row["end_line"] == content.count(b"\n", 0, occupied) + 1
    assert row["end_column"] == occupied - content.rfind(b"\n", 0, occupied)


def test_multiline_query_and_variadic_capture_locations(project, capsys, engine):
    source = """prisma.product.findMany({where: {organizationId: 1}});
prisma.product.findMany(
  {select: { id: true }},
  options
);
prisma.invoice.findMany({});
"""
    path = write(project, source)
    result = invoke(project, capsys, "prisma.product.findMany($$$ARGS)", path)
    data = result["data"]
    assert data["engine"]["name"] == "ast-grep" and data["engine"]["version"].startswith("0.50.")
    assert data["summary"] == {
        "files_selected": 1,
        "files_analyzed": 1,
        "matching_files": 1,
        "matches_total": 2,
    }
    assert result["coverage"]["status"] == "complete"
    assert result["coverage"]["stages"]["query"] == "complete"
    assert result["coverage"]["stages"]["dependencies"] == "not_run"
    assert (data["matches"][1]["line"], data["matches"][1]["end_line"]) == (2, 5)
    assert data["matches"][1]["captures"]["multi"]["ARGS"][-1]["text"] == "options"
    for match in data["matches"]:
        check_location(match, source, path)
        for rows in match["captures"]["multi"].values():
            for row in rows:
                check_location(row, source, path)


def test_named_captures_same_line_and_empty_variadic(project, capsys, engine):
    source = "router.get('/x', handler); router.post('/y', other); save();"
    path = write(project, source)
    data = invoke(project, capsys, "router.$METHOD($PATH, $$$HANDLERS)", path)["data"]
    first, second = data["matches"]
    assert first["end_column"] < second["column"]
    assert first["captures"]["single"]["METHOD"]["text"] == "get"
    assert second["captures"]["single"]["METHOD"]["text"] == "post"
    for match in data["matches"]:
        check_location(match, source, path)
        for row in match["captures"]["single"].values():
            check_location(row, source, path)
    empty = invoke(project, capsys, "save($$$ARGS)", path)["data"]["matches"][0]
    assert empty["captures"]["multi"]["ARGS"] == []


@pytest.mark.parametrize(
    "name",
    ["query.js", "query.jsx", "query.ts", "query.tsx", "query.mts", "query.cts", "query.mjs", "query.cjs"],
)
def test_language_mapping(project, capsys, engine, name):
    path = write(project, "save(1);", name)
    result = invoke(project, capsys, "save($ARG)", path)
    assert result["data"]["summary"]["matches_total"] == 1
    assert result["coverage"]["counts"]["query_analyzed"] == 1


@pytest.mark.parametrize("ending", ["\n", "\r\n"])
@pytest.mark.parametrize("bom", ["", "\ufeff"])
def test_unicode_byte_columns_bom_and_crlf(project, capsys, engine, ending, bom):
    source = bom + f"const café = '界'; save({ending}'你好'{ending});{ending}"
    path = write(project, source)
    row = invoke(project, capsys, "save($ARG)", path)["data"]["matches"][0]
    check_location(row, source, path)
    check_location(row["captures"]["single"]["ARG"], source, path)
    assert row["column"] == len("const café = '界'; ".encode()) + 1
    assert row["source_sha256"] == hashlib.sha256(source.removeprefix("\ufeff").encode()).hexdigest()


def test_scoping_ignore_hidden_files_and_no_project_config_loading(project, capsys, engine):
    path = write(project, "save(1);", ".hidden.ts")
    (project / ".gitignore").write_text(path + "\n", encoding="utf-8")
    ignored = project / "backend/src/excluded.ts"
    ignored.write_text("save('ignored');", encoding="utf-8")
    # Devkit's sole ignore file controls selection, not Git or ast-grep settings.
    configuration = Project(project)
    configuration.ignore_file.write_text(
        configuration.ignore_file.read_text(encoding="utf-8") + "\nbackend/src/excluded.ts\n",
        encoding="utf-8",
    )
    (project / "sgconfig.yml").write_text("invalid: [", encoding="utf-8")
    (project / "hub/src/index.ts").write_text("save('hub');", encoding="utf-8")
    result = invoke(project, capsys, "save($ARG)", "--app", "erp")
    assert [row["path"] for row in result["data"]["matches"]] == [path]
    assert result["coverage"]["exclusions"]["ignore_rule"] >= 1
    excluded = invoke(project, capsys, "save($ARG)", "backend/src/excluded.ts")
    assert excluded["data"]["summary"]["files_analyzed"] == 0
    assert excluded["coverage"]["status"] == "partial"


def test_language_filter_and_jsx_pattern(project, capsys, engine):
    js = write(project, "save(1);", "a.js")
    ts = write(project, "save(2);", "b.ts")
    jsx = write(project, "const view = <Card title='hi'/>;", "view.tsx")
    assert (
        invoke(project, capsys, "save($ARG)", "backend/src", "--lang", "js")["data"]["matches"][0]["path"]
        == js
    )
    assert (
        invoke(project, capsys, "save($ARG)", "backend/src", "--lang", "ts")["data"]["matches"][0]["path"]
        == ts
    )
    result = invoke(project, capsys, "<Card $$$PROPS/>", jsx, "--lang", "tsx")
    assert result["data"]["summary"]["matches_total"] == 1
    no_inputs = invoke(project, capsys, "save($ARG)", js, "--lang", "ts")
    assert no_inputs["coverage"]["status"] == "not_analyzed"


def test_no_matches_empty_file_and_unsupported_inputs(project, capsys, engine):
    path = write(project, "export const value=1;")
    result = invoke(project, capsys, "notPresent($ARG)", path)
    assert not result["truncated"] and result["data"]["matches"] == []
    assert result["coverage"]["status"] == "complete"
    write(project, "")
    assert invoke(project, capsys, "save($ARG)", path)["coverage"]["status"] == "complete"
    result = invoke(project, capsys, "save($ARG)", "notes.txt")
    assert result["coverage"]["status"] == "not_analyzed"
    assert result["data"]["summary"]["files_selected"] == 0


@pytest.mark.parametrize("pattern", ["", "  ", "function {", "const a=1; const b=2;"])
def test_invalid_patterns_are_not_reported_as_no_matches(project, capsys, engine, pattern):
    invoke(project, capsys, pattern, "backend/src/app.ts", status=2)


def test_missing_engine_has_actionable_error_and_doctor_does_not_execute(project, capsys, monkeypatch):
    monkeypatch.setattr("devkit.query.engine_path", lambda: None)
    result = invoke(project, capsys, "save($ARG)", status=2)
    assert "[query]" in result["error"]["message"]
    assert "query_engine_unavailable" in result["coverage"]["gaps"]
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("doctor must not execute ast-grep"))
    assert main(["--root", str(project), "doctor", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert next(row for row in data["checks"] if row["id"] == "engine.ast_grep")["status"] == "warning"


@pytest.mark.parametrize(
    "behavior",
    [
        "wrong_version",
        "timeout",
        "wrong_span",
        "wrong_text",
        "wrong_file",
        "skipped_scan",
        "non_json",
        "nonzero",
    ],
)
def test_engine_failures_and_unverifiable_results_are_errors(project, capsys, engine, monkeypatch, behavior):
    write(project, "save(1);")
    real_run = subprocess.run

    def altered_run(argv, **kwargs):
        assert isinstance(argv, list) and not kwargs.get("shell")
        if "--version" in argv:
            if behavior == "wrong_version":
                return subprocess.CompletedProcess(argv, 0, b"ast-grep 0.1.0\n", b"")
            return real_run(argv, **kwargs)
        if "--stdin" in argv:
            return real_run(argv, **kwargs)
        if behavior == "timeout":
            raise subprocess.TimeoutExpired(argv, 1)
        if behavior == "nonzero":
            kwargs["stderr"].write(b"Engine failed")
            return subprocess.CompletedProcess(argv, 2)
        if behavior == "skipped_scan":
            kwargs["stderr"].write(b"sg: summary|file: scannedFileCount=0,skippedFileCount=1\n")
            return subprocess.CompletedProcess(argv, 1)
        record = {
            "file": str(Path(argv[-1]) / "input00000000.ts"),
            "text": "save(1)",
            "range": {"byteOffset": {"start": 0, "end": 7}},
        }
        if behavior == "wrong_span":
            record["range"]["byteOffset"]["end"] = 1000
        elif behavior == "wrong_text":
            record["text"] = "fabricated"
        elif behavior == "wrong_file":
            record["file"] = str(project / ".env")
        kwargs["stdout"].write(b"not json" if behavior == "non_json" else json.dumps(record).encode() + b"\n")
        kwargs["stderr"].write(b"sg: summary|file: scannedFileCount=1,skippedFileCount=0\n")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", altered_run)
    result = invoke(project, capsys, "save($ARG)", "backend/src/query.ts", status=2)
    assert result["coverage"]["status"] == "partial"
    assert "query_incomplete" in result["coverage"]["gaps"]


def test_full_counts_survive_limits_and_order_is_stable(project, capsys, engine):
    for i in range(4):
        write(project, "\n".join(f"save({j});" for j in range(30)), f"order{i}.ts")
    first = invoke(project, capsys, "save($ARG)", "backend/src", "--max-results", "3")
    second = invoke(project, capsys, "save($ARG)", "backend/src", "--max-results", "3", "--no-cache")
    assert first["data"] == second["data"]
    assert first["truncated"] and first["data"]["summary"]["matches_total"] == 120
    assert len(first["data"]["matches"]) == 3
    assert all(row["path"] == "backend/src/order0.ts" for row in first["data"]["matches"])
    small = invoke(project, capsys, "save($ARG)", "backend/src", budget=2048)
    assert small["data"]["summary"]["matches_total"] == 120
    assert small["coverage"]["output_truncated"]


def test_binary_parse_errors_and_warm_cache_coverage(project, capsys, engine):
    path = write(project, "save(1); const broken = ;")
    (project / "backend/src/binary.ts").write_bytes(b"\0secret")
    for _ in range(2):
        result = invoke(project, capsys, "save($ARG)", "backend/src", "--lang", "ts")
        assert result["coverage"]["status"] == "partial"
        assert result["coverage"]["gaps"]["source_parse_error"] >= 1
        assert result["coverage"]["gaps"]["query_input_unreadable"] == 1
        assert any(row["path"] == path for row in result["data"]["matches"])


def test_engine_invocation_is_batched_and_cleanup_preserves_source(project, capsys, engine, monkeypatch):
    for i in range(40):
        write(project, "save(1);", f"batch{i}.ts")
    before = {path: path.read_bytes() for path in (project / "backend/src").rglob("*.ts")}
    real_run = subprocess.run
    batches = []

    def recorded(argv, **kwargs):
        if "--inspect=summary" in argv:
            batches.append(Path(argv[-1]))
            assert not Path(kwargs["cwd"]).is_relative_to(project)
            assert "--update-all" not in argv and "--rewrite" not in argv
            assert "--custom-languages=ignore" in argv
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", recorded)
    result = invoke(project, capsys, "save($ARG)", "backend/src", "--lang", "ts")
    assert len(batches) == 1 and not batches[0].exists()
    assert result["data"]["summary"]["matches_total"] == 40
    assert before == {path: path.read_bytes() for path in before}


def test_snapshot_rejects_capture_outside_match():
    snapshot = Snapshot("file.ts", b"save(1); other(2);", "ts", 0)
    record = {
        "text": "save(1)",
        "range": {"byteOffset": {"start": 0, "end": 7}},
        "metaVariables": {
            "single": {"X": {"text": "other(2)", "range": {"byteOffset": {"start": 9, "end": 17}}}}
        },
    }
    with pytest.raises(ValueError, match="outside"):
        snapshot.normalize(record)


def test_human_output_includes_ranges_and_captures(project, capsys, engine):
    path = write(project, "save(1);")
    assert main(["--root", str(project), "query", "save($ARG)", path]) == 0
    output = capsys.readouterr()
    assert "end_line:" in output.out and "captures:" in output.out
    assert "Coverage: complete" in output.err


def test_comments_strings_and_nested_calls(project, capsys, engine):
    source = "// save(3);\nconst text = 'save(4)';\nsave(save(1));"
    path = write(project, source)
    result = invoke(project, capsys, "save($ARG)", path)
    outer, inner = result["data"]["matches"]
    assert result["data"]["summary"]["matches_total"] == 2
    assert outer["line"] == inner["line"] == 3
    assert outer["start_byte"] < inner["start_byte"] < inner["end_byte"] < outer["end_byte"]
    for row in (outer, inner):
        check_location(row, source, path)


@pytest.mark.parametrize("content", [" ", "\n\n", "// Comment only\n", "\ufeff"])
def test_trivial_files_have_complete_processing(project, capsys, engine, content):
    path = write(project, content)
    result = invoke(project, capsys, "save($ARG)", path)
    assert result["coverage"]["status"] == "complete"
    assert result["data"]["summary"]["files_analyzed"] == 1
    assert result["data"]["summary"]["matches_total"] == 0


def test_active_environment_engine_is_found_without_path(project, capsys, monkeypatch):
    executable = engine_path()
    assert executable
    monkeypatch.setenv("PATH", "")
    assert engine_path() == executable


@pytest.mark.parametrize("path", ["../outside", ".env", "not-found.ts", "hub/src/index.ts"])
def test_paths_cannot_escape_scope(project, capsys, engine, path):
    result = invoke(project, capsys, "save($ARG)", path, "--app", "erp", status=0 if path == ".env" else 2)
    assert "supersecretneedle" not in json.dumps(result)
    if path == ".env":
        assert result["data"]["summary"]["files_analyzed"] == 0
        assert result["coverage"]["status"] == "partial"


def test_unicode_json_is_preserved_on_legacy_output_encoding(project, engine):
    source = "const café='界'; save('你好');"
    path = write(project, source)
    result = subprocess.run(
        [sys.executable, "-m", "devkit", "--root", str(project), "query", "save($ARG)", path, "--json"],
        capture_output=True,
        check=False,
        env={**os.environ, "PYTHONIOENCODING": "ascii"},
    )
    assert result.returncode == 0 and result.stderr == b"", result.stdout + result.stderr
    row = json.loads(result.stdout.decode("ascii"))["data"]["matches"][0]
    assert row["text"] == "save('你好')"
    check_location(row, source, path)
