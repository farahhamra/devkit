from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from devkit.agents import AgentSync
from devkit.cli import main
from devkit.diagnostics import Diagnostics
from devkit.project import Configuration, Project
from devkit.source import SourceIndex


def invoke(root, capsys, *args, status=0):
    code = main(["--root", str(root), *args, "--json"])
    captured = capsys.readouterr()
    assert code == status, captured.out + captured.err
    assert not captured.err
    return json.loads(captured.out)


def snapshot(root):
    return {
        p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns) if p.is_file() else None
        for p in root.rglob("*")
    }


def checks(result, code):
    return [row for row in result["data"]["checks"] if row["id"] == code]


def configure(root, text):
    path = root / "devkit/electroerp.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize("command", [("config", "inspect"), ("config", "validate"), ("doctor",)])
def test_diagnostics_are_read_only_and_do_not_scan(project, capsys, monkeypatch, command):
    before = snapshot(project)

    def forbidden(*args, **kwargs):
        raise AssertionError(
            "Diagnostics must not initialize projects, scan sources, lock, write or connect to DBs"
        )

    monkeypatch.setattr(Project, "__init__", forbidden)
    monkeypatch.setattr(SourceIndex, "__init__", forbidden)
    monkeypatch.setattr(Configuration, "files", forbidden, raising=False)
    monkeypatch.setattr(AgentSync, "locked", forbidden)
    monkeypatch.setattr("sqlite3.connect", forbidden)
    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    monkeypatch.setattr(Path, "write_bytes", forbidden)
    result = invoke(project, capsys, *command)
    assert snapshot(project) == before
    if command != ("config", "inspect"):
        assert result["data"]["summary"]["fail"] == 0
        assert result["data"]["read_only"]


def test_inspect_defaults_without_initializing_an_empty_root(tmp_path, capsys):
    data = invoke(tmp_path, capsys, "config", "inspect")["data"]
    assert data["selection"]["root_selection"] == "explicit"
    assert data["ignore"]["source"] == "bundled_defaults_not_created"
    assert data["top_level_sources"]["shared_models"] == "bundled_default"
    assert len(data["effective"]["modules"]) == 34
    assert not list(tmp_path.iterdir())
    result = invoke(tmp_path, capsys, "config", "validate", status=1)
    assert result["data"]["summary"]["fail"] > 0
    assert not list(tmp_path.iterdir())


def test_inspection_matches_analysis_merge_and_entry_replacement(project, capsys):
    path = configure(
        project,
        "modules:\n  products:\n    models: [CustomProduct]\nimport_aliases:\n  '@backend/': backend/src\nshared_models: [User]\n",
    )
    expected = Configuration(project, path).config
    result = invoke(project, capsys, "config", "inspect")["data"]
    products = next(row for row in result["effective"]["modules"] if row["name"] == "products")
    assert products["source"] == "manifest_override" and products["models"] == ["CustomProduct"]
    assert products["aliases"] == [] and "aliases" not in expected["modules"]["products"]
    assert result["effective"]["shared_models"] == ["User"]
    assert result["top_level_sources"]["shared_models"] == "manifest_override"
    assert result["effective"]["import_aliases"] == [{"prefix": "@backend/", "path": "backend/src"}]
    actual = Project(project, path).config
    assert expected == actual


def test_nested_config_and_directory_override_have_distinct_meanings(project, capsys, monkeypatch):
    path = project / ".agents/devkit/electroerp.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("shared_models: [NestedModel]\n", encoding="utf-8")
    location = project / "settings with spaces"
    location.mkdir()
    (location / ".ignore").write_text("# Custom\nnode_modules/\nnotes.txt\n", encoding="utf-8")
    monkeypatch.chdir(project)
    monkeypatch.setenv("DEVKIT_CONFIG_DIR", "settings with spaces")
    data = invoke(project, capsys, "config", "inspect", "--config", ".agents/devkit/electroerp.yaml")["data"]
    assert data["effective"]["shared_models"] == ["NestedModel"]
    assert data["selection"]["manifest"]["path"] == str(path)
    assert data["selection"]["configuration_directory"] == str(location)
    assert data["ignore"]["patterns"] == ["node_modules/", "notes.txt"]
    default = invoke(project, capsys, "config", "inspect")["data"]
    assert default["effective"]["shared_models"] != ["NestedModel"]
    result = invoke(project, capsys, "config", "validate")
    assert checks(result, "configuration.unselected_manifest")[0]["details"]["path"] == str(path)


def test_optional_apps_are_not_broken_unless_explicitly_selected(project, capsys):
    result = invoke(project, capsys, "doctor")
    optional = checks(result, "app.manager")[0]
    assert optional["status"] == "pass" and optional["details"]["configured"] is False
    explicit = invoke(project, capsys, "doctor", "--app", "manager", status=1)
    assert checks(explicit, "app.manager")[0]["status"] == "fail"
    assert not checks(explicit, "app.erp.root")


def test_missing_configured_app_paths_fail_only_selected_path_scope(project, capsys):
    configure(project, "apps:\n  hub:\n    roots: [missing-hub]\n    schemas: []\n")
    all_apps = invoke(project, capsys, "config", "validate", status=1)
    assert checks(all_apps, "app.hub.root")[0]["status"] == "fail"
    invoke(project, capsys, "config", "validate", "--app", "erp")


@pytest.mark.parametrize(
    "text",
    [
        "apps: [broken",
        "modules: null",
        "modules: []",
        "apps:\n  erp:\n    roots: bad\n",
        "modules:\n  products:\n    app: []\n",
        "version: true\n",
        "[]\n",
        "false\n",
        "apps:\n  manager:\n    roots: [../outside]\n",
        "modules:\n  products:\n    aliases: ['']\n",
    ],
)
def test_invalid_configuration_is_structured_and_doctor_continues(project, capsys, text):
    configure(project, text)
    before = snapshot(project)
    invoke(project, capsys, "config", "inspect", status=2)
    result = invoke(project, capsys, "config", "validate", status=1)
    assert checks(result, "configuration.load")[0]["status"] == "fail"
    doctor = invoke(project, capsys, "doctor", status=1)
    assert checks(doctor, "parser.javascript")[0]["status"] == "pass"
    assert checks(doctor, "storage.feedback")
    assert checks(doctor, "agents.template")
    assert snapshot(project) == before


def test_duplicate_yaml_keys_are_rejected_but_merge_anchors_work(project, capsys):
    configure(project, "shared_models: [User]\nshared_models: [Organization]\n")
    result = invoke(project, capsys, "config", "validate", status=1)
    assert "duplicate mapping key" in checks(result, "configuration.load")[0]["message"]
    configure(
        project,
        "example: &entry\n  roots: [backend, frontend]\n  schemas: []\napps:\n  erp:\n    <<: *entry\n    schemas: [backend/prisma/schema.prisma]\n",
    )
    invoke(project, capsys, "config", "validate")


def test_explicit_missing_manifest_is_not_silently_ignored(project, capsys):
    missing = str(project / "missing.yaml")
    invoke(project, capsys, "config", "inspect", "--config", missing, status=2)
    result = invoke(project, capsys, "doctor", "--config", missing, status=1)
    assert checks(result, "configuration.load")[0]["status"] == "fail"


def test_missing_ignored_or_out_of_scope_schema_is_explained(project, capsys):
    ignore = project / "devkit/.ignore"
    ignore.write_text("backend/prisma/\n", encoding="utf-8")
    result = invoke(project, capsys, "config", "validate", status=1)
    assert checks(result, "app.erp.schema")[0]["details"]["ignored"] is True
    ignore.write_text("", encoding="utf-8")
    configure(project, "apps:\n  erp:\n    roots: [frontend]\n    schemas: [backend/prisma/schema.prisma]\n")
    result = invoke(project, capsys, "config", "validate")
    assert checks(result, "app.erp.schema_scope")[0]["status"] == "warning"
    (project / "backend/prisma/schema.prisma").unlink()
    result = invoke(project, capsys, "config", "validate", status=1)
    assert checks(result, "app.erp.schema")[0]["details"]["state"] == "missing"


def test_required_paths_and_alias_collisions(project, capsys):
    configure(
        project,
        "modules:\n  example:\n    aliases: [product]\n    paths: [missing-optional-layer]\n    required: [missing-required-file.ts]\nimport_aliases:\n  '@missing/': not-here\n",
    )
    result = invoke(project, capsys, "config", "validate", status=1)
    assert any(row["status"] == "fail" for row in checks(result, "module.required"))
    assert checks(result, "module.alias_collision")[0]["status"] == "warning"
    assert checks(result, "module.path")[0]["status"] == "warning"
    assert checks(result, "import_alias.path")[0]["status"] == "warning"


def test_unknown_values_environment_and_raw_yaml_are_not_disclosed(project, capsys, monkeypatch):
    monkeypatch.setenv("DOCTOR_PRIVATE_SECRET", "environment-secret-should-not-appear")
    configure(
        project,
        "database_url: 'mysql://user:top-secret@localhost/db'\napps:\n  erp:\n    roots: [backend, frontend]\n    schemas: [backend/prisma/schema.prisma]\n    extra_private: private-app-value\nmodules:\n  products:\n    models: [Product]\n    extra_private: private-module-value\n",
    )
    result = invoke(project, capsys, "config", "inspect")
    serialized = json.dumps(result)
    for secret in (
        "top-secret",
        "private-app-value",
        "private-module-value",
        "environment-secret-should-not-appear",
    ):
        assert secret not in serialized
    assert "apps.erp.extra_private" in result["data"]["unrecognized_fields"]
    configure(project, "private: 'a-secret-value\n")
    result = invoke(project, capsys, "doctor", status=1)
    assert "a-secret-value" not in json.dumps(result)
    assert "line" in checks(result, "configuration.load")[0]["message"]


def test_missing_parser_can_be_diagnosed_in_a_fresh_process(project):
    code = """
import importlib.abc, json, sys
class Deny(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith('tree_sitter'):
            raise ImportError('Simulated missing parser')
sys.meta_path.insert(0, Deny())
from devkit.cli import main
sys.exit(main(['--root', sys.argv[1], 'doctor', '--json']))
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(project)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 1, result.stdout + result.stderr
    data = json.loads(result.stdout)
    assert not result.stderr
    assert checks(data, "dependency.tree-sitter")[0]["status"] == "fail"
    assert checks(data, "configuration.load")[0]["status"] == "pass"
    assert checks(data, "storage.feedback")


@pytest.mark.parametrize("module", ["yaml", "pathspec"])
def test_missing_configuration_dependency_does_not_prevent_doctor(project, module):
    code = """
import importlib.abc, sys
class Deny(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == sys.argv[2]:
            raise ImportError('Simulated missing configuration dependency')
sys.meta_path.insert(0, Deny())
from devkit.cli import main
sys.exit(main(['--root', sys.argv[1], 'doctor', '--json']))
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(project), module], capture_output=True, text=True, check=False
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert not result.stderr
    assert checks(json.loads(result.stdout), "storage.feedback")


def test_parser_api_failure_is_a_failed_check_not_a_traceback(project, capsys, monkeypatch):
    def fail(*args, **kwargs):
        raise TypeError("Simulated ABI mismatch")

    monkeypatch.setattr("tree_sitter.Parser", fail)
    result = invoke(project, capsys, "doctor", status=1)
    assert checks(result, "parser.tsx")[0]["status"] == "fail"


def test_agent_template_states_are_read_only(project, capsys):
    result = invoke(project, capsys, "doctor")
    assert {r["details"]["state"] for r in checks(result, "agents.template")} == {"missing"}
    invoke(project, capsys, "agents", "sync")
    before = snapshot(project)
    current = invoke(project, capsys, "doctor")
    assert {r["details"]["state"] for r in checks(current, "agents.template")} == {"current"}
    assert snapshot(project) == before
    path = project / ".agents/rules/devkit-rules.md"
    path.write_text(path.read_text(encoding="utf-8") + "\nPrivate custom instruction\n", encoding="utf-8")
    modified = invoke(project, capsys, "doctor")
    assert checks(modified, "agents.template")[0]["details"]["state"] == "locally_edited_or_unmanaged"
    assert "Private custom instruction" not in json.dumps(modified)


def test_storage_failures_are_reported_without_db_connections(project, capsys, monkeypatch):
    folder = project / "devkit/.feedback"
    folder.mkdir()
    (folder / "reports.sqlite3").write_bytes(b"private opaque database bytes")
    (folder / ".gitignore").write_text("# customized", encoding="utf-8")
    before = snapshot(project)

    def forbidden(*args, **kwargs):
        raise AssertionError("Doctor must not open SQLite")

    monkeypatch.setattr("sqlite3.connect", forbidden)
    result = invoke(project, capsys, "doctor", status=1)
    assert checks(result, "storage.feedback.ignore")[0]["status"] == "fail"
    assert "private opaque database bytes" not in json.dumps(result)
    assert snapshot(project) == before


def test_permission_estimates_and_no_cache_switch(project, capsys, monkeypatch):
    original = __import__("devkit.diagnostics", fromlist=["probe"]).probe

    def inaccessible(path):
        row = original(path)
        if path.name == ".cache":
            row["creatable_estimate"] = False
        return row

    monkeypatch.setattr("devkit.diagnostics.probe", inaccessible)
    result = invoke(project, capsys, "doctor")
    assert checks(result, "storage.cache")[0]["status"] == "warning"
    result = invoke(project, capsys, "doctor", "--no-cache")
    assert checks(result, "storage.cache")[0]["status"] == "pass"
    assert "disabled" in checks(result, "storage.cache")[0]["message"]


@pytest.mark.parametrize("relative", ["devkit/.feedback", "devkit/.ignore", ".agents/.devkit-sync"])
def test_unsafe_symlink_paths_are_not_followed(project, capsys, tmp_path, relative):
    destination = tmp_path / "outside"
    destination.mkdir()
    target = project / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.symlink_to(destination, target_is_directory=True)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows account lacks symlink privilege")
        raise
    invoke(project, capsys, "doctor", status=1)
    assert not list(destination.iterdir())


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO unavailable")
def test_configuration_fifo_is_rejected_without_opening(tmp_path, capsys):
    (tmp_path / "devkit").mkdir()
    os.mkfifo(tmp_path / "devkit/electroerp.yaml")
    invoke(tmp_path, capsys, "config", "validate", status=1)
    invoke(tmp_path, capsys, "config", "inspect", status=2)


def test_global_structure_safety_is_checked_even_for_other_app(project, capsys):
    configure(project, "apps:\n  hub:\n    roots: [../outside]\n")
    result = invoke(project, capsys, "config", "validate", "--app", "erp", status=1)
    assert checks(result, "configuration.load")[0]["status"] == "fail"


def test_summary_and_exit_status_survive_truncation(project, capsys):
    configure(project, "apps:\n  hub:\n    roots: [missing-hub]\n")
    result = invoke(project, capsys, "doctor", "--max-results", "1", "--max-output-chars", "2048", status=1)
    assert result["truncated"]
    assert result["data"]["summary"]["fail"] > 0


def test_config_inspection_has_no_schema_or_installation_health_claim(project, capsys, monkeypatch):
    (project / "backend/prisma/schema.prisma").write_text("model NotParsed {", encoding="utf-8")

    def forbidden(*args, **kwargs):
        raise AssertionError("Config inspect should not probe parsers")

    monkeypatch.setattr(Diagnostics, "runtime_checks", forbidden)
    invoke(project, capsys, "config", "inspect")
    invoke(project, capsys, "config", "validate")


def test_config_help_is_side_effect_free(tmp_path, capsys):
    data = invoke(tmp_path, capsys, "config")["data"]
    assert len(data) == 3
    assert not list(tmp_path.iterdir())
