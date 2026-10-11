"""Run with a non-editable install: uv run --isolated --no-project --with . python tests/smoke_install.py."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from importlib.resources import files
from pathlib import Path

from devkit import __version__
from devkit.agents import TARGETS


def main():
    resources = files("devkit").joinpath("agent_templates")
    assert resources.is_dir(), "Installed packages must include templates, not depend on checkout fallback"
    assert {p.name for p in resources.iterdir()} == set(TARGETS.values())
    with tempfile.TemporaryDirectory(prefix="ElectroERP project with spaces ") as directory:
        root = Path(directory)

        def invoke(*args, status=0):
            result = subprocess.run(
                [sys.executable, "-m", "devkit", "--root", directory, *args, "--json"],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
                env={**os.environ, "PYTHONPATH": "", "DEVKIT_CONFIG_DIR": str(root / "devkit")},
            )
            assert result.returncode == status, result.stdout + result.stderr
            assert not result.stderr, result.stderr
            return json.loads(result.stdout)

        assert any(row["command"] == "devkit agents sync" for row in invoke("help")["data"])
        invoke("agents", "sync", "--dry-run")
        invoke("agents", "sync", "--check", status=1)
        assert not list(root.iterdir())
        synced = invoke("agents", "sync")["data"]
        assert synced["applied"] and synced["template_version"] == __version__
        invoke("agents", "sync", "--check")
        rule = root / ".agents/rules/devkit-rules.md"
        edited = rule.read_bytes() + b"\nLocal customization\n"
        rule.write_bytes(edited)
        invoke("agents", "sync", status=2)
        invoke("agents", "sync", "--force", "--dry-run")
        assert rule.read_bytes() == edited
        forced = invoke("agents", "sync", "--force")["data"]
        assert (root / forced["backup"] / ".agents/rules/devkit-rules.md").read_bytes() == edited
        invoke("agents", "sync", "--check")
        report = invoke("feedback", "add", "--type", "feature", "--title", "Package smoke test")["data"][
            "report"
        ]
        assert invoke("feedback", "inspect", report["id"])["data"]["title"] == "Package smoke test"
    print("Installed-package smoke checks passed: bundled sync, backups, idempotence and feedback.")


if __name__ == "__main__":
    main()
