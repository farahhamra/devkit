from __future__ import annotations

import json
import subprocess
import sys

import pytest

from devkit.cli import main


def invoke(capsys, project, *args, status=0):
    actual = main(["--root", str(project), *args, "--json"])
    captured = capsys.readouterr()
    assert actual == status, captured.out + captured.err
    assert captured.err == ""
    return json.loads(captured.out)


def test_help_catalog_is_complete_and_side_effect_free(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = invoke(capsys, tmp_path, "help")
    assert len(result["data"]) == 45
    commands = {r["command"] for r in result["data"]}
    assert "devkit impact field" in commands
    assert "devkit trace route" in commands
    assert not list(tmp_path.iterdir())
    topic = invoke(capsys, tmp_path, "help", "module", "inspect")
    assert len(topic["data"]) == 1
    assert "--scope" in topic["data"][0]["usage"]


def test_search_literal_smart_case_and_ignore(project, capsys):
    data = invoke(capsys, project, "search", "token", "notes.txt")["data"]
    assert len(data) == 3
    data = invoke(capsys, project, "search", "Token", "notes.txt")["data"]
    assert len(data) == 1
    data = invoke(capsys, project, "search", "literal[brackets]", "notes.txt", "--context", "1")["data"]
    assert data[0]["line"] == 2 and data[0]["column"] == 1
    assert data[0]["before"][0]["line"] == 1
    assert data[0]["after"][0]["line"] == 3
    data = invoke(capsys, project, "search", "supersecretneedle")["data"]
    assert data == []
    data = invoke(capsys, project, "search", "specialneedle")["data"]
    assert {r["path"] for r in data} == {"notes.txt", "frontend/src/.visible.ts"}
    assert (project / "devkit/.ignore").exists()


def test_regex_glob_limits_and_json_errors(project, capsys):
    result = invoke(capsys, project, "search", "token", "--glob", "*.txt", "--max-results", "2")
    assert len(result["data"]) == 2 and result["truncated"]
    assert result["truncation"][0]["available_at_least"] == 3
    result = invoke(capsys, project, "search", "^Token", "--regex", "--glob", "*.txt")
    assert len(result["data"]) == 1
    error = invoke(capsys, project, "search", "[", "--regex", status=2)
    assert "regular expression" in error["error"]["message"]
    invoke(capsys, project, "search", "anything", "--max-results", "0", status=2)
    invoke(capsys, project, "no-such-command", status=2)
    invoke(capsys, project, "search", "", status=2)


def test_project_root_relative_paths_from_nested_cwd(project, capsys, monkeypatch):
    monkeypatch.chdir(project / "frontend/src/app/core/products")
    assert main(["find", "notes.txt", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["root"] == str(project)
    assert result["data"][0]["path"] == "notes.txt"


def test_find_extension_and_tree_budget(project, capsys):
    result = invoke(capsys, project, "find", "products", "--type", "file", "--extension", "tsx")
    assert len(result["data"]) == 1
    result = invoke(capsys, project, "find", "products", "--type", "dir")
    assert all(r["type"] == "dir" for r in result["data"])
    result = invoke(capsys, project, "tree", "frontend", "--depth", "4", "--max-results", "2")
    assert result["data"]["shown_nodes"] == 2 and result["truncated"]


def test_symbols_js_ts_tsx_and_filters(project, capsys):
    result = invoke(capsys, project, "symbols", "backend/src/core/features/products", "--exported-only")
    names = {r["name"] for r in result["data"]}
    assert {"ProductService", "ProductDto", "ProductId", "Status", "listProducts"} <= names
    assert "localOnly" not in names
    result = invoke(capsys, project, "symbols", "frontend", "--kind", "function", "--name", "Product")
    assert "ProductForm" in {r["name"] for r in result["data"]}


def test_module_inspection_relations_and_reasons(project, capsys):
    result = invoke(capsys, project, "module", "inspect", "product")["data"]
    assert result["module"] == "products"
    assert {m["name"] for m in result["models"]} == {"Product", "ProductVariant"}
    assert {r["target"] for r in result["dependencies"]} == {"Organization"}
    assert {r["source"] for r in result["consumers"]} == {"Invoice"}
    assert any(f["layer"] == "migrations" for f in result["files"])
    assert any(f["path"].endswith("read-products.tool.md") for f in result["files"])
    assert all(f["reasons"] for f in result["files"])
    result = invoke(
        capsys, project, "module", "inspect", "products", "--scope", "frontend", "--related", "none"
    )["data"]
    assert all(f["layer"] == "frontend" for f in result["files"])
    assert result["dependencies"] == []


def test_schema_inspection_enums_constraints_and_graph(project, capsys):
    result = invoke(capsys, project, "schema", "inspect", "product")["data"]
    fields = {f["name"]: f for f in result["fields"]}
    assert result["table"] == "products"
    assert fields["baseSellingPrice"]["optional"]
    assert fields["baseSellingPrice"]["column"] == "base_selling_price"
    assert fields["organization"]["foreign_keys"] == ["organizationId"]
    assert fields["organization"]["on_delete"] == "Cascade"
    assert result["unique_constraints"][0]["fields"] == ["organizationId", "name"]
    assert fields["status"]["kind"] == "enum"
    enum = invoke(capsys, project, "schema", "inspect", "ProductStatus")["data"]
    assert [v["name"] for v in enum["values"]] == ["active", "archived"]
    relations = invoke(capsys, project, "schema", "relations", "Product", "--direction", "incoming")["data"]
    assert {r["source"] for r in relations} == {"ProductVariant", "Invoice"}
    assert all(r["cardinality"] == "many-to-one" for r in relations)
    listing = invoke(capsys, project, "schema", "list", "--module", "products")["data"]
    assert {r["name"] for r in listing} == {"Product", "ProductVariant", "ProductStatus"}


def test_trace_route_mounts_handlers_models_and_clients(project, capsys):
    result = invoke(capsys, project, "trace", "route", "/api/products/", "--method", "GET")["data"]
    assert len(result["routes"]) == 1
    route = result["routes"][0]
    assert route["handlers"] == ["auth", "getProducts"]
    assert route["mounts"][0]["prefix"] == "/api/products"
    assert any(c["callee"] == "prisma.product.findMany" for c in route["prisma_candidates"])
    assert any(c["path"].startswith("frontend/") for c in result["client_candidates"])
    result = invoke(capsys, project, "trace", "route", "/api/products/:id", "--method", "PATCH")["data"]
    assert result["routes"][0]["method"] == "PATCH"


def test_trace_alias_and_reverse_impacts(project, capsys):
    trace = invoke(capsys, project, "trace", "symbol", "listProducts")["data"]
    assert any(i["local_name"] == "loadProducts" for i in trace["imports"])
    assert any(r["name"] == "loadProducts" for r in trace["references"])
    result = invoke(
        capsys, project, "impact", "file", "backend/src/core/features/products/product.service.ts"
    )["data"]
    assert any(r["path"].endswith("invoice.service.ts") for r in result["consumers"])
    model = invoke(capsys, project, "impact", "model", "Product")["data"]
    assert any(r.get("callee") == "prisma.product.findMany" for r in model["references"])
    field = invoke(capsys, project, "impact", "field", "Product.baseSellingPrice")["data"]
    assert any(
        r["evidence"] == "name_only_candidate" and r["path"].endswith("unrelated.ts")
        for r in field["references"]
    )
    assert any(r["evidence"] == "model_and_field_in_same_file" for r in field["references"])
    module = invoke(capsys, project, "module", "impact", "products")["data"]
    assert "invoices" in module["affected_modules"]


def test_module_checks_pass_then_fail_for_real_defects(project, capsys):
    result = invoke(capsys, project, "module", "check", "products")["data"]
    assert result["summary"]["fail"] == 0
    assert result["summary"]["pass"] > 0
    (project / "backend/ai/rag/tools/products/read-products.tool.md").unlink()
    result = invoke(capsys, project, "module", "check", "products", status=1)["data"]
    assert any(c["rule"] == "ai_tool_documentation" and c["status"] == "fail" for c in result["checks"])


def test_app_scope_and_configured_hub(project, capsys):
    assert invoke(capsys, project, "search", "hubneedle", "--app", "erp")["data"] == []
    assert len(invoke(capsys, project, "search", "hubneedle", "--app", "hub")["data"]) == 1
    invoke(capsys, project, "tree", "--app", "manager", status=2)
    invoke(capsys, project, "search", "needle", "hub", "--app", "erp", status=2)


def test_ignore_negation_preservation_and_explicit_target(project, capsys):
    ignore = project / "devkit/.ignore"
    content = "*.txt\n!notes.txt\nbackend/node_modules/\n.env\n"
    ignore.write_text(content)
    assert invoke(capsys, project, "search", "specialneedle", "notes.txt")["data"]
    assert (
        invoke(capsys, project, "search", "supersecretneedle", "backend/node_modules/dependency/index.js")[
            "data"
        ]
        == []
    )
    assert ignore.read_text() == content


def test_symlinks_and_escape_are_rejected(project, capsys):
    try:
        (project / "alias.ts").symlink_to(project / "backend/src/app.ts")
        (project / "linked").symlink_to(project / "backend", target_is_directory=True)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows account lacks symlink creation privilege")
        raise
    data = invoke(capsys, project, "find", "alias")["data"]
    assert not data
    invoke(capsys, project, "symbols", "alias.ts", status=2)


def test_escape_is_rejected_without_symlink_privileges(project, capsys):
    invoke(capsys, project, "search", "x", "../outside", status=2)


@pytest.mark.parametrize(
    "args",
    [
        ["module", "list"],
        ["schema", "list", "--kind", "enum"],
        ["tree", "--dirs-only"],
        ["impact", "file", "backend/prisma/schema.prisma"],
        ["module", "check", "all"],
    ],
)
def test_other_command_paths(project, capsys, args):
    result = invoke(capsys, project, *args)
    assert result["schema_version"] == "1.0"


def test_installed_module_entrypoint(project):
    process = subprocess.run(
        [sys.executable, "-m", "devkit", "--root", str(project), "schema", "list", "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert process.returncode == 0, process.stderr
    assert len(json.loads(process.stdout)["data"]) == 5
