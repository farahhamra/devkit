"""Run with a non-editable install: uv run --isolated --no-project --with . python tests/smoke_install.py."""

from __future__ import annotations

import json
import os
import re
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
    for template in resources.iterdir():
        content = template.read_text(encoding="utf-8")
        assert "analyze <file>" in content
        assert not re.search(r"\b(?:prefer|must use|always use)\b[^.\n]*\banalyze\b", content, re.IGNORECASE)
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
        assert invoke("config", "inspect")["data"]["ignore"]["source"] == "bundled_defaults_not_created"
        assert invoke("config", "validate", status=1)["data"]["summary"]["fail"] > 0
        doctor = invoke("doctor", status=1)["data"]
        assert doctor["runtime"]["devkit_version"] == __version__
        assert next(row for row in doctor["checks"] if row["id"] == "parser.tsx")["status"] == "pass"
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
        source = root / "backend/src/example.ts"
        source.parent.mkdir(parents=True)
        source.write_text("export const example = 1;", encoding="utf-8")
        symbols = invoke("symbols", "backend/src/example.ts")["coverage"]
        assert symbols["status"] == "complete" and symbols["counts"]["source_analyzed"] == 1
        assert symbols["stages"]["schema"] == "not_run"
        source.write_text(
            "export function outer() {\n  function inner() { return 1; }\n  return inner();\n}",
            encoding="utf-8",
        )
        for _ in range(2):
            briefing = invoke("analyze", "backend/src/example.ts", "--symbol", "outer.inner")
            parent, child = briefing["data"]["symbols"]
            assert child["parent_id"] == parent["id"]
            assert child["line"] == child["end_line"] == 2
            assert briefing["data"]["summary"]["source_complete"]
            assert briefing["coverage"]["counts"]["source_analyzed"] == 1
        source.write_text("export const example = ;", encoding="utf-8")
        for _ in range(2):
            symbols = invoke("symbols", "backend/src/example.ts")["coverage"]
            assert symbols["status"] == "partial" and symbols["counts"]["source_partial"] == 1
    print(
        "Installed-package smoke checks passed: diagnostics, analyze, coverage, bundled sync, backups and feedback."
    )


if __name__ == "__main__":
    main()
