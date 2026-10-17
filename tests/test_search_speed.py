"""Search optimizations must preserve the old per-line search contract."""

import argparse
import fnmatch
import json
from pathlib import PureWindowsPath

import pytest

from devkit.cli import main, matcher, search
from devkit.project import Configuration, Project


def reference(project, args):
    pattern = matcher(args.query, args.regex)
    result = []
    project.coverage.start("text")
    selected = []
    for path in project.entries(args.path):
        if args.glob and not any(
            fnmatch.fnmatchcase(project.rel(path), g) or fnmatch.fnmatchcase(path.name, g) for g in args.glob
        ):
            project.coverage.exclude("glob_filter", project.rel(path))
        else:
            selected.append(path)
    project.coverage.details["search_candidate_files"] = len(selected)
    for path in selected:
        content = project.text(path)
        if content is None:
            continue
        lines = content.splitlines()
        for index, line in enumerate(lines):
            for match in pattern.finditer(line):
                result.append(
                    {
                        "path": project.rel(path),
                        "line": index + 1,
                        "column": match.start() + 1,
                        "match": match.group(),
                        "text": line,
                        "before": [
                            {"line": j + 1, "text": lines[j]}
                            for j in range(max(0, index - args.context), index)
                        ],
                        "after": [
                            {"line": j + 1, "text": lines[j]}
                            for j in range(index + 1, min(len(lines), index + 1 + args.context))
                        ],
                    }
                )
                if len(result) > args.max_results:
                    project.coverage.gap(
                        "search_result_limit", project.rel(path), stage="text", line=index + 1
                    )
                    return result
    project.coverage.finish("text")
    return result


@pytest.mark.parametrize(
    "query,regex",
    [
        ("needle", False),
        ("NEEDLE", False),
        ("[id]", False),
        ("i", False),
        ("k", False),
        ("s", False),
        ("ſ", False),
        ("İ", False),
        ("😃", False),
        ("missing", False),
        ("needle\nend", False),
        ("^", True),
        ("$", True),
        (r"(?<=n)eedle", True),
        (r"(n)\1?", True),
        (r"\b", True),
        (".*", True),
    ],
)
@pytest.mark.parametrize("limit,context,glob", [(1, 0, []), (100, 2, ["*.txt"]), (100, 0, [])])
def test_differential_search(project, query, regex, limit, context, glob):
    folder = project / "backend/search-fixture"
    folder.mkdir()
    (folder / "a.txt").write_bytes(
        ("\ufeffneedle NEEDLE\r\n😃 [id] needle\rneedle\vend\fneedle\x85İıſK\u2028needle\u2029end\n").encode()
    )
    (folder / "z.ts").write_text("needle\nend", encoding="utf-8")
    (folder / "binary.txt").write_bytes(b"needle" + b"a" * 9000 + b"\0")
    (folder / "invalid.txt").write_bytes(b"needle" + b"a" * 9000 + b"\xff")
    args = argparse.Namespace(
        query=query,
        regex=regex,
        path="backend/search-fixture",
        glob=glob,
        max_results=limit,
        context=context,
        timings=False,
    )
    before, after = Project(project), Project(project)
    assert search(after, args) == reference(before, args)
    assert after.coverage.report() == before.coverage.report()
    assert after.warnings == before.warnings
    assert after._texts == {}


def test_timings_opt_in_and_json_clean(project, capsys):
    arguments = ["--root", str(project), "search", "not-present", "backend", "--json"]
    assert main(arguments) == 0
    ordinary = json.loads(capsys.readouterr().out)
    assert main([*arguments, "--timings"]) == 0
    output = capsys.readouterr()
    assert not output.err
    measured = json.loads(output.out)
    timing = measured["coverage"]["details"].pop("search_timings")
    assert ordinary == measured
    assert timing["bytes_read"] > 0
    assert timing["through_search_ms"] >= timing["setup_ms"] >= 0
    assert all(timing[k] >= 0 for k in ("discovery_ms", "read_ms", "match_ms"))


@pytest.mark.parametrize(
    "pattern,flags,hint",
    [
        ("*customers*", [], True),
        ("customers?", [], True),
        ("customers", [], False),
        ("[id]", [], False),
        (".*customers.*", ["--regex"], False),
    ],
)
def test_find_guidance(project, capsys, pattern, flags, hint):
    assert main(["--root", str(project), "find", pattern, *flags, "--json"]) == 0
    output = capsys.readouterr()
    assert not output.err
    result = json.loads(output.out)
    assert any("not globs" in warning for warning in result["warnings"]) == hint


def test_find_bracketed_directory_is_literal(project, capsys):
    (project / "frontend/[id]").mkdir()
    assert main(["--root", str(project), "find", "[id]", "--type", "dir", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["data"] == [{"path": "frontend/[id]", "type": "dir"}]
    assert result["warnings"] == []


def test_relative_path_cache_preserves_windows_spelling():
    config = object.__new__(Configuration)
    config.root = PureWindowsPath("C:/ERP")
    config._relative_paths = {}
    assert config.rel(PureWindowsPath("C:/ERP/Products/File.ts")) == "Products/File.ts"
    assert config.rel(PureWindowsPath("C:/ERP/products/file.ts")) == "products/file.ts"


@pytest.mark.parametrize("json_mode", [False, True])
def test_timing_output_respects_budget(project, capsys, json_mode):
    (project / "large.txt").write_text("needle " + "a" * 50000, encoding="utf-8")
    flags = ["--json"] if json_mode else []
    assert (
        main(
            [
                "--root",
                str(project),
                "search",
                "needle",
                "large.txt",
                "--timings",
                "--max-output-chars",
                "2048",
                *flags,
            ]
        )
        == 0
    )
    output = capsys.readouterr()
    assert len(output.out) + len(output.err) <= 2048
    if json_mode:
        assert json.loads(output.out)["truncated"]
    else:
        assert "Search timings:" in output.err
