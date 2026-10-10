from __future__ import annotations

import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from devkit import cache
from devkit.analysis import Analysis
from devkit.cli import main
from devkit.modules import Modules
from devkit.project import Project
from devkit.schema import Schema
from devkit.source import SourceIndex


def test_document_references_are_not_owners(project):
    doc = project / "backend/ai/rag/tools/inventory/check-stock.tool.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("Uses Product and ProductVariant with Inventory.")
    implementation = project / "backend/src/core/features/ai/tools/inventory/check-stock.tool.ts"
    implementation.parent.mkdir(parents=True)
    implementation.write_text("export const checkStock = () => true;")
    p = Project(project)
    modules = Modules(p, Schema(p))
    reference = next(f for f in modules.files("products") if f["path"] == p.rel(doc))
    assert reference["ownership"] == "reference"
    assert reference["evidence"] == "text_reference"
    assert "products" not in modules.owners_of_file(p.rel(doc))
    assert "inventory" in modules.owners_of_file(p.rel(doc))
    assert modules.check("products")["summary"]["fail"] == 0
    own_doc = next(f for f in modules.files("products") if f["path"].endswith("read-products.tool.md"))
    assert own_doc["ownership"] == "owned"
    assert own_doc["evidence"] == "convention"
    assert len(own_doc["reasons"]) == 2


def test_directory_ownership_wins_over_document_name(project):
    doc = project / "backend/ai/rag/tools/inventory/read-products.tool.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("Product reference")
    p = Project(project)
    modules = Modules(p, Schema(p))
    owners = modules.owners_of_file(p.rel(doc))
    assert "inventory" in owners
    assert "products" not in owners


def test_symbols_does_not_enumerate_project(project, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("Scoped symbols must not enumerate the whole project")

    monkeypatch.setattr(Project, "files", forbidden)
    assert main(["--root", str(project), "symbols", "backend/src/app.ts", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert {s["path"] for s in result["data"]} == {"backend/src/app.ts"}


def test_cache_warm_edit_rename_delete(project):
    cold = SourceIndex(Project(project))
    warm = SourceIndex(Project(project))
    assert cold.stats["parsed_files"] == len(cold.files)
    assert warm.stats == {"parsed_files": 0, "cached_files": len(cold.files)}
    assert (cold.files, cold.symbols, cold.calls, cold.references) == (
        warm.files,
        warm.symbols,
        warm.calls,
        warm.references,
    )
    path = project / "backend/src/added.ts"
    path.write_text("export const before = 1;")
    added = SourceIndex(Project(project))
    assert added.stats["parsed_files"] == 1
    original_stat = path.stat()
    path.write_text("export const after_ = 1;")
    os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    changed = SourceIndex(Project(project))
    assert changed.stats["parsed_files"] == 1
    assert any(s["name"] == "after_" for s in changed.symbols)
    renamed = path.with_name("renamed.ts")
    path.rename(renamed)
    index = SourceIndex(Project(project))
    assert "backend/src/added.ts" not in index.files
    assert "backend/src/renamed.ts" in index.files
    renamed.unlink()
    assert "backend/src/renamed.ts" not in SourceIndex(Project(project)).files
    with sqlite3.connect(project / "devkit/.cache/source.sqlite3") as db:
        rows = db.execute("SELECT path FROM sources").fetchall()
    assert not any(p in {"backend/src/added.ts", "backend/src/renamed.ts"} for (p,) in rows)


@pytest.mark.parametrize("change", ["config", "ignore", "parser"])
def test_cache_settings_invalidation(project, monkeypatch, change):
    cold = SourceIndex(Project(project))
    if change == "config":
        config = project / "devkit/electroerp.yaml"
        config.write_text(config.read_text() + "\nimport_aliases: {'~': 'backend/src'}\n")
    elif change == "ignore":
        ignore = project / "devkit/.ignore"
        ignore.write_text(ignore.read_text() + "\nbackend/src/app.ts\n")
    else:
        monkeypatch.setattr(cache, "CACHE_VERSION", 999)
    index = SourceIndex(Project(project))
    assert index.stats["cached_files"] == 0
    assert index.stats["parsed_files"] == len(cold.files) - (change == "ignore")


def test_cache_reresolves_imports_against_live_catalog(project):
    target = project / "backend/src/new-consumer.ts"
    target.write_text("import { value } from './new-target'; export const result = value;")
    cold = SourceIndex(Project(project))
    assert cold.files["backend/src/new-consumer.ts"]["imports"][0]["resolved"] is None
    dependency = project / "backend/src/new-target.ts"
    dependency.write_text("export const value = 1;")
    warm = SourceIndex(Project(project))
    assert warm.stats["parsed_files"] == 1
    assert warm.files["backend/src/new-consumer.ts"]["imports"][0]["resolved"] == "backend/src/new-target.ts"
    assert (
        warm.dependencies("backend/src/new-target.ts", reverse=True)[0]["path"]
        == "backend/src/new-consumer.ts"
    )
    dependency.unlink()
    assert (
        SourceIndex(Project(project)).files["backend/src/new-consumer.ts"]["imports"][0]["resolved"] is None
    )


@pytest.mark.parametrize("failure", ["database", "entry", "unwritable", "locked"])
def test_cache_failure_falls_back_to_live_analysis(project, monkeypatch, failure):
    cold = SourceIndex(Project(project))
    db_path = project / "devkit/.cache/source.sqlite3"
    lock = None
    if failure == "database":
        db_path.write_bytes(b"not a SQLite database")
    elif failure == "entry":
        with sqlite3.connect(db_path) as db:
            db.execute("UPDATE sources SET data='{}'")
    elif failure == "unwritable":

        def denied(*args, **kwargs):
            raise sqlite3.OperationalError("readonly database")

        monkeypatch.setattr(cache.sqlite3, "connect", denied)
    else:
        lock = sqlite3.connect(db_path)
        lock.execute("BEGIN EXCLUSIVE")
    try:
        p = Project(project)
        result = SourceIndex(p)
        assert result.symbols == cold.symbols
        assert result.stats["parsed_files"] == len(cold.files)
        assert any("cache" in warning.lower() for warning in p.warnings)
    finally:
        if lock:
            lock.rollback()
            lock.close()


def test_parallel_cache_writers(project):
    Project(project)  # initialize the ignore policy before concurrent readers
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: SourceIndex(Project(project)), range(2)))
    warm = SourceIndex(Project(project))
    assert results[0].symbols == results[1].symbols == warm.symbols
    assert warm.stats["parsed_files"] == 0


def test_no_cache_creates_no_database(project, capsys):
    assert main(["--root", str(project), "symbols", "--no-cache", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]
    assert not (project / "devkit/.cache").exists()


def test_parser_diagnostics_are_incomplete_not_compiler_verdict(project):
    folder = project / "frontend/src/app/core/products"
    (folder / "Card.tsx").write_text("export const Card = () => <h2>Sales & Revenue Performance</h2>;")
    (folder / "broken.ts").write_text("export const broken = ;")
    for _ in range(2):
        p = Project(project)
        result = Modules(p, Schema(p)).check("products")
        errors = [
            row for row in result["checks"] if row["rule"] == "source_parse" and row["status"] == "warning"
        ]
        assert len(errors) == 2
        assert result["summary"]["fail"] == 0
        assert all(row["diagnostics"][0]["line"] == 1 for row in errors)
        jsx = next(row for row in errors if row["path"].endswith("Card.tsx"))
        assert "ampersand" in jsx["diagnostics"][0]["hint"]
        assert any("partial" in warning for warning in p.warnings)


def test_call_arguments_are_bounded(project):
    path = project / "backend/src/large.ts"
    path.write_text("register(() => {" + "work();" * 1000 + "});")
    index = SourceIndex(Project(project), paths=[path])
    call = next(c for c in index.calls if c["callee"] == "register")
    assert call["arguments_truncated"]
    assert len(call["arguments"][0]) <= 241


def test_route_summary_preserves_candidates_and_full_is_opt_in(project):
    analysis = Analysis(Project(project))
    summary = analysis.trace_route("/api/products")
    full = analysis.trace_route("/api/products", full=True)
    assert len(summary["routes"][0]["definitions"]) < len(full["routes"][0]["definitions"])
    assert summary["routes"][0]["prisma_candidates"] == full["routes"][0]["prisma_candidates"]
    assert {d["name"] for d in summary["routes"][0]["definitions"]} == {"getProducts"}


@pytest.mark.parametrize("json_mode", [True, False])
def test_total_output_budget_includes_long_lines(project, capsys, json_mode):
    (project / "huge.txt").write_text("needle " + "x" * 50000)
    args = ["--root", str(project), "search", "needle", "huge.txt", "--max-output-chars", "2048"]
    assert main(args + (["--json"] if json_mode else [])) == 0
    captured = capsys.readouterr()
    assert len(captured.out + captured.err) <= 2048
    if json_mode:
        result = json.loads(captured.out)
        assert result["truncated"]
        assert result["truncation"][0]["reason"] == "total_output_budget"
    else:
        assert "truncated" in captured.err


def test_output_truncation_preserves_check_failure_status(project, capsys):
    (project / "backend/src/core/features/products/product.service.ts").unlink()
    assert (
        main(["--root", str(project), "module", "check", "products", "--max-output-chars", "2048", "--json"])
        == 1
    )
    captured = capsys.readouterr()
    assert len(captured.out) <= 2048
    result = json.loads(captured.out)
    assert result["data"]["summary"]["fail"] > 0
    assert result["truncated"]


@pytest.mark.parametrize("json_mode", [True, False])
def test_full_trace_budget_bounds_nested_evidence(project, capsys, json_mode):
    service = project / "backend/src/core/features/products/product.service.ts"
    service.write_text(
        service.read_text()
        + "\n"
        + "\n".join(
            f"export function candidate{i}() {{ return prisma.product.findMany({{where: {{id: {i}}}}}); }}"
            for i in range(100)
        )
    )
    args = ["--root", str(project), "trace", "route", "/api/products", "--full", "--max-output-chars", "2048"]
    assert main(args + (["--json"] if json_mode else [])) == 0
    captured = capsys.readouterr()
    assert len(captured.out + captured.err) <= 2048
    if json_mode:
        result = json.loads(captured.out)
        assert result["truncated"]
        assert result["data"]["query"] == "/api/products"


def test_tree_budget_reports_actual_shown_count(project, capsys):
    assert (
        main(["--root", str(project), "tree", "--depth", "10", "--max-output-chars", "2048", "--json"]) == 0
    )
    captured = capsys.readouterr()
    assert len(captured.out) <= 2048
    result = json.loads(captured.out)

    def count(node):
        return sum(1 + count(child) for child in node.get("children", []))

    assert result["truncated"]
    assert result["data"]["shown_nodes"] == count(result["data"])
