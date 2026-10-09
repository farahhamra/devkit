from __future__ import annotations

import json
import os

import pytest

from devkit.cli import main
from devkit.modules import Modules
from devkit.project import DevkitError, Project
from devkit.schema import Schema, parse_schema
from devkit.source import SourceIndex


def test_reexport_alias_and_star_chains(project):
    folder = project / "backend/src/core/features/products"
    (folder / "barrel.ts").write_text("export * from './index';")
    (folder / "consumer.ts").write_text(
        "import { loadProducts as fetchItems } from './barrel'; fetchItems();"
    )
    index = SourceIndex(Project(project))
    trace = index.symbol_trace("listProducts")
    assert any(i["local_name"] == "fetchItems" for i in trace["imports"])
    assert any(r["name"] == "fetchItems" for r in trace["references"])
    assert any(e["exported_name"] == "loadProducts" for e in trace["exports"])


def test_commonjs_and_javascript_symbols(project):
    folder = project / "backend/src/core/features/products"
    (folder / "legacy.js").write_text(
        "const { listProducts: read } = require('./product.service'); function legacy() { return read(); }"
    )
    index = SourceIndex(Project(project))
    trace = index.symbol_trace("listProducts")
    assert any(i["local_name"] == "read" for i in trace["imports"])
    assert any(s["name"] == "legacy" and s["kind"] == "function" for s in index.symbols)


def test_namespace_import_and_custom_alias_resolution(project):
    config = project / "devkit/electroerp.yaml"
    config.write_text(config.read_text() + "\nimport_aliases:\n  '@backend/': backend/src\n")
    (project / "frontend/src/consumer.ts").write_text(
        "import * as service from '@backend/core/features/products/product.service'; service.listProducts();"
    )
    index = SourceIndex(Project(project))
    imported = index.files["frontend/src/consumer.ts"]["imports"][0]
    assert imported["resolved"] == "backend/src/core/features/products/product.service.ts"
    assert any(
        r.get("expression") == "service.listProducts"
        for r in index.symbol_trace("listProducts")["references"]
    )


def test_parse_errors_and_unresolved_dynamic_imports_warn(project):
    target = project / "frontend/src/broken.ts"
    target.write_text("export const broken = ;\nimport(runtimePath);")
    p = Project(project)
    SourceIndex(p)
    assert any("Parse errors" in w for w in p.warnings)
    assert any("Dynamic import" in w for w in p.warnings)


def test_route_detection_ignores_other_get_calls_and_handles_nested_mounts(project):
    app = project / "backend/src/app.ts"
    app.write_text(
        "import express from 'express'; import core from './core/routes'; const app = express(); app.use('/api', core); const cache = new Map(); cache.get('/not-a-route');"
    )
    (project / "backend/src/core/routes.ts").write_text(
        "import { Router as makeRouter } from 'express'; import products from './features/products/product.routes'; const router = makeRouter(); router.use('/products', products); export default router;"
    )
    index = SourceIndex(Project(project))
    routes = index.routes()
    assert {r["route"] for r in routes} == {"/api/products/", "/api/products/:id"}
    assert len(routes[0]["mounts"]) == 2


def test_scoped_module_paths_do_not_confuse_plugin_categories(project):
    path = project / "backend/src/plugins/supermarket/categories/categories.service.ts"
    path.parent.mkdir(parents=True)
    path.write_text("export const categories = [];")
    p = Project(project)
    modules = Modules(p, Schema(p))
    relative = p.rel(path)
    assert relative not in {f["path"] for f in modules.files("products")}
    assert relative in {f["path"] for f in modules.files("supermarket")}


def test_shared_hub_traversal_is_bounded(project):
    path = project / "backend/prisma/schema.prisma"
    path.write_text(
        path.read_text().replace("products Product[]", "products Product[]\n audits Audit[]")
        + "\nmodel Audit { id Int @id organizationId Int organization Organization @relation(fields: [organizationId], references: [id]) }"
    )
    schema = Schema(Project(project))
    bounded = schema.relations("ProductVariant", depth=3)
    expanded = schema.relations("ProductVariant", depth=3, include_shared=True)
    assert not any(e["source"] == "Audit" and e["target"] == "Organization" for e in bounded)
    assert any(e["source"] == "Audit" and e["target"] == "Organization" for e in expanded)


@pytest.mark.parametrize(
    "configuration",
    [
        "apps:\n  erp:\n    schemas: bad",
        "apps:\n  erp:\n    roots: [null]",
        "modules:\n  products:\n    models: bad",
        "modules:\n  products:\n    app: unknown",
        "import_aliases: []",
        "shared_models: bad",
        "version: 2",
        "- invalid",
        "apps:\n  erp:\n    roots: [../../outside]",
        "modules:\n  products:\n    paths: [../../outside]",
    ],
)
def test_invalid_config_is_structured_error(project, configuration, capsys):
    (project / "devkit/electroerp.yaml").write_text(configuration)
    assert main(["--root", str(project), "tree", "--json"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["error"]["code"] == 2


def test_unclosed_schema_reports_error():
    with pytest.raises(DevkitError, match="Unclosed"):
        parse_schema("model Product { id Int")


def test_empty_project_cannot_pass_checks_or_schema_queries(tmp_path, capsys):
    assert main(["--root", str(tmp_path), "module", "check", "all", "--json"]) == 2
    assert "No available modules" in json.loads(capsys.readouterr().out)["error"]["message"]
    assert main(["--root", str(tmp_path), "schema", "list", "--json"]) == 2
    assert "No readable Prisma" in json.loads(capsys.readouterr().out)["error"]["message"]


def test_config_dir_override_and_no_overwrite(project, tmp_path, monkeypatch):
    override = tmp_path / "dedicated"
    override.mkdir()
    ignore = override / ".ignore"
    ignore.write_text("notes.txt\n")
    monkeypatch.setenv("DEVKIT_CONFIG_DIR", str(override))
    p = Project(project)
    assert p.ignore_file == ignore
    assert project / "notes.txt" not in p.files()
    assert ignore.read_text() == "notes.txt\n"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO only")
def test_fifo_does_not_block_file_discovery(project):
    os.mkfifo(project / "pipe.ts")
    assert project / "pipe.ts" not in Project(project).files()


def test_schema_ambiguity_requires_app_selection(project):
    hub = project / "hub/schema.prisma"
    hub.write_text("model Product { id Int @id }")
    with pytest.raises(DevkitError, match="Ambiguous"):
        Schema(Project(project)).get("Product")
    assert Schema(Project(project, app="hub")).get("Product")["path"] == "hub/schema.prisma"


def test_module_check_detects_missing_imported_handler_and_unknown_model(project, capsys):
    controller = project / "backend/src/core/features/products/product.controller.ts"
    controller.write_text(
        controller.read_text().replace("function getProducts", "function renamedGetProducts")
    )
    service = project / "backend/src/core/features/products/product.service.ts"
    service.write_text(service.read_text().replace("prisma.product.findMany", "prisma.nonexistent.findMany"))
    assert main(["--root", str(project), "module", "check", "products", "--json"]) == 1
    checks = json.loads(capsys.readouterr().out)["data"]["checks"]
    assert any(c["rule"] == "imported_export" and c["status"] == "fail" for c in checks)
    assert any(c["rule"] == "prisma_reference" and c["status"] == "fail" for c in checks)
