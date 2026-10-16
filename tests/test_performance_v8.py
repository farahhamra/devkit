"""Opt-in format integration against real, isolated synthetic Node/V8 exports."""

import json
import os
import shutil
import subprocess

import pytest

from devkit.cli import main


@pytest.mark.skipif(
    os.environ.get("DEVKIT_TEST_V8") != "1", reason="Set DEVKIT_TEST_V8=1 with Node installed"
)
def test_native_v8_exports(project, capsys):
    node = shutil.which("node")
    assert node, "Node is required when DEVKIT_TEST_V8=1"
    directory = project / "measurements"
    directory.mkdir()
    # Snapshots include environment values: do not pass tokens or inherited
    # NODE_OPTIONS/Python hooks into this synthetic profiling process.
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"PATH", "SYSTEMROOT", "TEMP", "TMP"}
    }
    result = subprocess.run(
        [
            node,
            "--cpu-prof",
            f"--cpu-prof-dir={directory}",
            "--cpu-prof-name=test.cpuprofile",
            "-e",
            'function work(){let n=0;for(let i=0;i<1000000;i++)n+=Math.sqrt(i);return n;} work(); require("node:v8").writeHeapSnapshot(process.argv[1]);',
            str(directory / "test.heapsnapshot"),
        ],
        cwd=directory,
        env=environment,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, "Synthetic Node exporter failed"
    for kind, suffix in (("runtime", "cpuprofile"), ("memory", "heapsnapshot")):
        code = main(
            [
                "--root",
                str(project),
                "performance",
                kind,
                "--input",
                f"measurements/test.{suffix}",
                "--app",
                "erp",
                "--json",
            ]
        )
        output = capsys.readouterr()
        assert code == 0, output.out + output.err
        report = json.loads(output.out)["data"]
        assert report["summary"]["measurement_units"] > 0
        if kind == "runtime":
            assert report["cpu"]["sample_count"] > 0
        else:
            assert report["heap"]["node_count"] > 1000
            assert report["heap"]["retained_status"] == "estimated"
