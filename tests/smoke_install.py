"""Run with a non-editable install: uv run --isolated --no-project --with . python tests/smoke_install.py."""

from __future__ import annotations

import argparse
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
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--query", action="store_true", help="Exercise the installed optional ast-grep engine"
    )
    args = parser.parse_args()
    resources = files("devkit").joinpath("agent_templates")
    assert resources.is_dir(), "Installed packages must include templates, not depend on checkout fallback"
    assert {p.name for p in resources.iterdir()} == set(TARGETS.values())
    audit_assets = files("devkit").joinpath("audit")
    assert audit_assets.joinpath("security.yaml").is_file()
    assert audit_assets.joinpath("unused.mjs").is_file()
    assert audit_assets.joinpath("knip-plugins.mjs").is_file()
    collector = files("devkit").joinpath("node_collector")
    for name in ("collector.mjs", "adapters.mjs", "storage.mjs", "package.json"):
        assert collector.joinpath(name).is_file(), "Optional Node collector must ship in installed wheels"
    assert json.loads(collector.joinpath("package.json").read_text())["version"] == __version__
    for template in resources.iterdir():
        content = template.read_text(encoding="utf-8")
        assert "analyze <file>" in content
        assert "query <pattern>" in content
        assert "audit routes [path]" in content
        assert "audit duplicates [path]" in content
        assert "performance database --input <capture.json>" in content
        for command in ("runtime", "memory", "cache"):
            assert f"performance {command} --input <capture" in content
        assert "performance compare --before <capture> --after <capture>" in content
        assert "plan archive <conversation-id>" in content
        for command in ("open", "navigate", "inspect", "screenshot", "close", "status", "console", "network"):
            assert f"browser {command}" in content
        assert ".gemini/antigravity-ide/brain/<conversation-id>/" in content
        assert not re.search(
            r"\b(?:prefer|must use|always use)\b[^.\n]*\b(?:analyze|query)\b", content, re.IGNORECASE
        )
    with tempfile.TemporaryDirectory(prefix="ElectroERP project with spaces ") as directory:
        root = Path(directory)
        profile = root / "Synthetic user profile"

        def invoke(*args, status=0):
            result = subprocess.run(
                [sys.executable, "-m", "devkit", "--root", directory, *args, "--json"],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
                env={
                    **os.environ,
                    "PYTHONPATH": "",
                    "DEVKIT_CONFIG_DIR": str(root / "devkit"),
                    "HOME": str(profile),
                    "USERPROFILE": str(profile),
                },
            )
            assert result.returncode == status, result.stdout + result.stderr
            assert not result.stderr, result.stderr
            return json.loads(result.stdout)

        assert any(row["command"] == "devkit agents sync" for row in invoke("help")["data"])
        assert len(invoke("help", "browser")["data"]) == 9
        invoke("browser", "inspect", "--session", "missing", status=2)
        for command in ("status", "console", "network"):
            invoke("browser", command, "--session", "missing", status=2)
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
        conversation = profile / ".gemini/antigravity-ide/brain/package-smoke"
        conversation.mkdir(parents=True)
        documents = {"implementation_plan.md": b"# Plan\r\nPackage check\r\n", "task.md": b"- [x] Verify\n"}
        for name, content in documents.items():
            (conversation / name).write_bytes(content)
        assert invoke("plan", "archive", "package-smoke", "--dry-run")["data"]["status"] == "would_archive"
        assert not (root / ".agents/implementation").exists()
        saved = invoke("plan", "archive", "package-smoke")["data"]
        assert saved["index"] == "001" and saved["status"] == "archived"
        for row in saved["files"].values():
            name = Path(row["source"]).name
            assert (root / row["archive"]).read_bytes() == documents[name]
            assert (conversation / name).read_bytes() == b""
            assert row["verified"]
        assert invoke("plan", "archive", "package-smoke")["data"]["status"] == "already_archived"
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
        schema = root / "backend/prisma/schema.prisma"
        schema.parent.mkdir(parents=True)
        schema.write_text('model Product { id Int @id @@map("products") }', encoding="utf-8")
        capture_path = root / "capture.json"
        capture_path.write_text(
            json.dumps(
                {
                    "capture_version": "1.0",
                    "metadata": {
                        "app": "erp",
                        "captured_at": "2026-10-14T00:00:00Z",
                        "database": {"engine": "mysql", "version": "8.0.36"},
                        "workload": {"name": "installed-wheel-test", "duration_ms": 100},
                    },
                    "queries": [
                        {
                            "id": "q1",
                            "kind": "sql",
                            "fingerprint": "a" * 64,
                            "operation": "select",
                            "tables": ["products"],
                            "timing": {"source": "mysql_digest", "count": 2, "total_ms": 10},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        performance = invoke("performance", "database", "--input", "capture.json", "--app", "erp")
        assert performance["data"]["queries"][0]["timing"]["mean_ms"] == 5
        assert performance["data"]["queries"][0]["mappings"][0]["model"] == "Product"
        assert performance["coverage"]["stages"]["performance"] == "partial"
        invoke("performance", "database", "--input", "capture.json", "--app", "erp", "--strict", status=1)
        for kind in ("runtime", "memory", "cache"):
            # Only fixture data comes from the checkout; all executable code and
            # templates under test must be supplied by the installed package.
            fixture = Path(__file__).resolve().parents[1] / "examples" / f"performance-{kind}.json"
            name = f"{kind}.json"
            (root / name).write_bytes(fixture.read_bytes())
            report = invoke("performance", kind, "--input", name, "--app", "erp")
            assert report["data"]["summary"]["measurement_units"] > 0
            assert report["coverage"]["stages"]["schema"] == "not_run"
            comparison = invoke(
                "performance",
                "compare",
                "--before",
                name,
                "--after",
                name,
                "--app",
                "erp",
                "--fail-on-regression",
            )
            assert comparison["data"]["summary"]["compared_metrics"] > 0
            assert comparison["data"]["summary"]["regression_candidates"] == 0
        if args.query:
            source.write_bytes("const café = '界'; save(\r\n'你好'\r\n);".encode())
            data = invoke("query", "save($ARG)", "backend/src/example.ts")
            row = data["data"]["matches"][0]
            assert (row["line"], row["end_line"]) == (1, 3)
            assert row["column"] == len("const café = '界'; ".encode()) + 1
            assert row["captures"]["single"]["ARG"]["text"] == "'你好'"
            assert data["coverage"]["stages"]["query"] == "complete"
            assert invoke("query", "missing($ARG)", "backend/src/example.ts")["data"]["matches"] == []
    print(
        "Installed-package smoke checks passed: diagnostics, analyze, coverage, bundled sync, backups, feedback, performance, plan archives"
        + (" and structural query." if args.query else ".")
    )


if __name__ == "__main__":
    main()
