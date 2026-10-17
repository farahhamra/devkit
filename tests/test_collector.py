"""Collector lifecycle contracts and actual Node-to-Python interoperability."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from test_performance_extended import capture, compare, invoke

from devkit.cli import main

ROOT = Path(__file__).resolve().parents[1]


def quality(status="completed"):
    return {
        "status": status,
        "dropped_samples": 0,
        "issues": [] if status == "completed" else ["interrupted"],
        "windows": [],
    }


@pytest.mark.parametrize("kind", ["runtime", "memory", "cache"])
def test_collection_revisions_keep_complete_inputs_working(project, capsys, kind):
    data = capture(kind)
    data.update(capture_version="2.1", collection=quality())
    result = invoke(project, capsys, kind, data, "--strict")
    assert result["data"]["collection"]["status"] == "completed"
    assert result["coverage"]["status"] == "complete"


@pytest.mark.parametrize("kind", ["runtime", "memory", "cache"])
def test_incomplete_collection_never_compares_as_clean(project, capsys, kind):
    data = capture(kind)
    data.update(capture_version="2.1", collection=quality("interrupted"))
    result = invoke(project, capsys, kind, data, "--strict", status=1)
    assert result["coverage"]["gaps"]["performance_collection_incomplete"] == 1
    compared = compare(project, capsys, data, data, "--strict", status=1)
    assert compared["data"]["comparisons"] == []
    assert compared["data"]["comparison_status"] == "incompatible"


@pytest.mark.parametrize(
    "change",
    [
        {"dropped_samples": 1},
        {"status": "not-a-status"},
        {"dropped_samples": -1},
        {"issues": ["DO_NOT_PRINT"]},
        {"extra": "DO_NOT_PRINT"},
        {"windows": [{"stream": "gc", "start_ms": 50, "end_ms": 1, "boundary": "observed"}]},
        {"windows": [{"stream": "gc", "start_ms": 0, "end_ms": 1e12, "boundary": "observed"}]},
    ],
)
def test_invalid_collection_quality_rejected(project, capsys, change):
    data = capture("runtime")
    data.update(capture_version="2.1", collection={**quality(), **change})
    invoke(project, capsys, "runtime", data, status=2)


def test_database_collection_version_and_legacy_validation(project, capsys):
    data = json.loads((ROOT / "examples/performance-capture.json").read_text())
    data.update(capture_version="1.1", collection=quality("limited"))
    result = invoke(project, capsys, "database", data)
    assert result["data"]["capture_version"] == "1.1"
    assert result["coverage"]["gaps"]["performance_collection_incomplete"] == 1
    compare(project, capsys, data, data, "--strict", status=1)
    data["capture_version"] = "1.0"
    invoke(project, capsys, "database", data, status=2)  # no silent contract widening


def test_missing_collection_and_old_version_extra_rejected(project, capsys):
    data = capture("runtime")
    data["capture_version"] = "2.1"
    invoke(project, capsys, "runtime", data, status=2)
    data.update(capture_version="2.0", collection=quality())
    invoke(project, capsys, "runtime", data, status=2)


@pytest.mark.skipif(not shutil.which("node"), reason="Optional Node collector runtime unavailable")
def test_actual_collector_outputs_are_consumable(project, capsys):
    run = subprocess.run(
        [shutil.which("node"), str(ROOT / "tests/collector_fixture.mjs"), str(project)],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    session = json.loads(run.stdout)
    assert session["status"] == "completed", session
    assert len(session["files"]) == 4
    for file in session["files"]:
        path = Path(file)
        kind = path.stem
        assert (
            main(
                [
                    "--root",
                    str(project),
                    "performance",
                    kind,
                    "--input",
                    str(path),
                    "--json",
                    "--max-output-chars",
                    "200000",
                ]
            )
            == 0
        )
        output = capsys.readouterr()
        result = json.loads(output.out)
        assert not output.err
        assert result["data"]["performance"] == kind
        assert result["data"]["collection"]["status"] == "completed"
        # Actual sample timestamps/control windows must not be rewritten to fake alignment.
        if kind == "memory":
            assert result["coverage"]["gaps"]["performance_window_partial"] == 1
        if kind == "database":
            assert result["data"]["summary"]["prisma_groups"] == 1


@pytest.mark.skipif(not shutil.which("node"), reason="Optional Node collector runtime unavailable")
def test_node_collector_unit_suite():
    run = subprocess.run(
        [shutil.which("node"), "--test", "integrations/node/collector.test.mjs"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert run.returncode == 0, run.stdout + run.stderr


@pytest.mark.skipif(not shutil.which("node"), reason="Optional Node collector runtime unavailable")
@pytest.mark.parametrize("budget,expected", [(1024, "limited"), (16 * 1024 * 1024, "completed")])
def test_separately_authorized_synthetic_heap(project, capsys, budget, expected):
    environment = {k: v for k, v in os.environ.items() if k.upper() in {"PATH", "SYSTEMROOT", "TEMP", "TMP"}}
    run = subprocess.run(
        [shutil.which("node"), str(ROOT / "tests/collector_heap_fixture.mjs"), str(project), str(budget)],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    session = json.loads(run.stdout)
    assert session["status"] == expected
    directory = Path(session["directory"])
    if expected == "limited":
        assert session["files"] == []
        assert not (directory / "heap.heapsnapshot").exists()
        assert not (directory / "session-complete.json").exists()
    else:
        assert (directory / "heap.heapsnapshot").stat().st_size <= budget
        assert (
            main(
                [
                    "--root",
                    str(project),
                    "performance",
                    "memory",
                    "--input",
                    str(directory / "heap.heapsnapshot"),
                    "--json",
                ]
            )
            == 0
        )
        data = json.loads(capsys.readouterr().out)
        assert data["coverage"]["status"] == "partial"  # native input, no invented metadata
