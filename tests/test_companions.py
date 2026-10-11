from __future__ import annotations

import json

import pytest
import yaml

from devkit.cli import main
from devkit.modules import Modules
from devkit.project import Project
from devkit.schema import Schema

TOOLS = (
    "create-ecom-note-category",
    "create-ecom-note",
    "delete-ecom-note",
    "read-ecom-note-categories",
    "read-ecom-notes",
    "update-ecom-note",
)
DOCS = "backend/ai/rag/tools/plugin-e-commerce"
IMPL = "backend/src/plugins/e-commerce/ai/notes"


def write(project, relative, content=""):
    path = project / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture
def plugin_project(project):
    # This fixture tests tools, not the unrelated Ecom models absent from its schema.
    config_path = project / "devkit/electroerp.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["modules"]["e-commerce"] = {"models": [], "aliases": ["ecom", "plugin-e-commerce"]}
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    for name in TOOLS:
        write(project, f"{DOCS}/{name}.tool.md", f"# {name}\nUses Product context.")
        write(project, f"{IMPL}/{name}.tool.ts", "export const execute = () => true;")
    return project


def check(project, capsys, module, *, status=0, flags=()):
    actual = main(
        [
            "--root",
            str(project),
            "module",
            "check",
            module,
            "--json",
            "--max-results",
            "5000",
            "--max-output-chars",
            "1000000",
            *flags,
        ]
    )
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert actual == status, result
    assert not captured.err
    assert not result["truncated"]
    return result["data"]


@pytest.mark.parametrize("module", ["ai", "e-commerce", "all"])
def test_six_plugin_tools_match_across_overlapping_owners(plugin_project, capsys, module):
    project = plugin_project
    p = Project(project)
    modules = Modules(p, Schema(p))
    assert set(modules.owners_of_file(f"{DOCS}/{TOOLS[0]}.tool.md")) == {"ai", "e-commerce"}
    assert modules.owners_of_file(f"{IMPL}/{TOOLS[0]}.tool.ts") == ["e-commerce"]
    results = [
        check(project, capsys, module, flags=flags) for flags in ((), (), ("--no-cache", "--app", "erp"))
    ]
    assert results[0] == results[1] == results[2]
    assert results[0]["summary"]["fail"] == 0
    target_module = "ai" if module == "all" else module
    for name in TOOLS:
        row = next(
            c
            for c in results[0]["checks"]
            if c["module"] == target_module
            and c["path"] == f"{DOCS}/{name}.tool.md"
            and c["rule"] == "ai_tool_implementation"
        )
        assert row["status"] == "pass"
        assert row["companions"] == [f"{IMPL}/{name}.tool.ts"]


@pytest.mark.parametrize("missing", ["implementation", "documentation"])
def test_real_missing_plugin_companions_still_fail(plugin_project, capsys, missing):
    folder, extension = (IMPL, "ts") if missing == "implementation" else (DOCS, "md")
    (plugin_project / f"{folder}/{TOOLS[0]}.tool.{extension}").unlink()
    result = check(plugin_project, capsys, "all", status=1)
    failures = [c for c in result["checks"] if c["rule"] == "ai_tool_" + missing and c["status"] == "fail"]
    assert failures
    assert all(c["companions"] == [] for c in failures)


@pytest.mark.parametrize(
    "decoy",
    [
        "backend/src/plugins/another-plugin/ai/notes",
        "backend/src/core/features/e-commerce/ai/notes",
        "backend/src/core/features/ai/tools/products",
        "hub/backend/src/plugins/e-commerce/ai/notes",
        "second/backend/src/plugins/e-commerce/ai/notes",
    ],
)
def test_same_filename_in_other_namespaces_cannot_mask_missing_tool(plugin_project, capsys, decoy):
    name = TOOLS[0]
    (plugin_project / f"{IMPL}/{name}.tool.ts").unlink()
    write(plugin_project, f"{decoy}/{name}.tool.ts", "export const unrelated = true;")
    if decoy.startswith("second/"):
        config_path = plugin_project / "devkit/electroerp.yaml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        config["apps"]["erp"]["roots"].append("second/backend")
        config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    result = check(plugin_project, capsys, "ai", status=1)
    row = next(c for c in result["checks"] if c["path"] == f"{DOCS}/{name}.tool.md")
    assert row["status"] == "fail"
    assert row["companions"] == []


def test_same_filename_docs_in_other_plugin_cannot_mask_missing_doc(plugin_project, capsys):
    name = TOOLS[0]
    (plugin_project / f"{DOCS}/{name}.tool.md").unlink()
    write(plugin_project, f"backend/ai/rag/tools/plugin-another-plugin/{name}.tool.md")
    result = check(plugin_project, capsys, "e-commerce", status=1)
    row = next(
        c
        for c in result["checks"]
        if c["path"] == f"{IMPL}/{name}.tool.ts" and c["rule"] == "ai_tool_documentation"
    )
    assert row["status"] == "fail"


def test_other_app_cannot_satisfy_companion_even_in_same_backend_tree(plugin_project, capsys):
    config_path = plugin_project / "devkit/electroerp.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["apps"]["hub"]["roots"] = [IMPL]
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    for scope in ("all", "erp"):
        result = check(plugin_project, capsys, "ai", status=1, flags=("--app", scope))
        assert result["summary"]["fail"] == 6


@pytest.mark.parametrize("suffix", ["ts", "md"])
def test_ignored_companion_does_not_count(plugin_project, capsys, suffix):
    p = Project(plugin_project)
    relative = f"{IMPL if suffix == 'ts' else DOCS}/{TOOLS[0]}.tool.{suffix}"
    p.ignore_file.write_text(p.ignore_file.read_text(encoding="utf-8") + relative + "\n", encoding="utf-8")
    module = "ai" if suffix == "ts" else "e-commerce"
    result = check(plugin_project, capsys, module, status=1)
    assert any(c["status"] == "fail" and "excluded by .ignore" in c["message"] for c in result["checks"])


def test_duplicate_same_namespace_companions_report_ambiguity(plugin_project, capsys):
    duplicate = f"backend/src/plugins/e-commerce/ai/alternate/{TOOLS[0]}.tool.ts"
    write(plugin_project, duplicate, "export const duplicate = true;")
    result = check(plugin_project, capsys, "ai")
    row = next(c for c in result["checks"] if c["path"] == f"{DOCS}/{TOOLS[0]}.tool.md")
    assert row["status"] == "warning"
    assert "Ambiguous" in row["message"]
    assert row["companions"] == sorted([duplicate, f"{IMPL}/{TOOLS[0]}.tool.ts"])


def test_feature_aliases_match_without_crossing_other_features(project):
    name = "read-products"
    old = project / f"backend/ai/rag/tools/products/{name}.tool.md"
    new = write(project, f"backend/ai/rag/tools/product/{name}.tool.md", old.read_text(encoding="utf-8"))
    old.unlink()
    p = Project(project)
    modules = Modules(p, Schema(p))
    result = modules.check("products")
    row = next(c for c in result["checks"] if c["path"] == p.rel(new))
    assert row["status"] == "pass"
    assert row["companions"] == [f"backend/src/core/features/ai/tools/products/{name}.tool.ts"]


def test_unrecognized_layout_only_matches_same_directory_sibling(project, capsys):
    doc = "backend/ai/custom-a/generic.tool.md"
    wrong_impl = write(project, "backend/ai/custom-b/generic.tool.ts", "export const wrong = true;")
    write(project, doc)
    result = check(project, capsys, "ai", status=1)
    assert any(c["path"] == doc and c["status"] == "fail" for c in result["checks"])
    wrong_impl.unlink()
    write(project, "backend/ai/custom-a/generic.tool.ts", "export const correct = true;")
    assert check(project, capsys, "ai")["summary"]["fail"] == 0
