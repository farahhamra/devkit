from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from devkit.cli import main
from devkit.project import Project


def invoke(root, capsys, *args, status=0, budget=200000):
    actual = main(["--root", str(root), "analyze", *args, "--json", "--max-output-chars", str(budget)])
    output = capsys.readouterr()
    assert actual == status, output.out + output.err
    assert not output.err
    assert len(output.out) <= budget
    return json.loads(output.out)


def write(project, content, name="brief.ts"):
    path = project / "backend/src" / name
    path.write_bytes(content.encode("utf-8"))
    return path.relative_to(project).as_posix()


def check_span(row, source, path):
    data = source.removeprefix("\ufeff").encode("utf-8")
    assert row["path"] == path
    start, end = row["start_byte"], row["end_byte"]
    assert 0 <= start <= end <= len(data)
    assert row["line"] == data.count(b"\n", 0, start) + 1
    assert row["column"] == start - data.rfind(b"\n", 0, start)
    final = end
    if end > start and data[end - 1 : end] == b"\n":
        final -= 1
        if final and data[final - 1 : final] == b"\r":
            final -= 1
    assert row["end_line"] == data.count(b"\n", 0, final) + 1
    assert row["end_column"] == final - data.rfind(b"\n", 0, final)
    return data[start:end].decode("utf-8")


def test_user_example_exact_ranges_and_parent_links(project, capsys):
    lines = [""] * 90
    lines[9] = "function X() {"
    lines[44] = "  function Y() {"
    lines[49] = "  }"
    lines[89] = "}"
    source = "\n".join(lines)
    path = write(project, source)
    result = invoke(project, capsys, path)
    data = result["data"]
    x, y = data["symbols"]
    assert (x["line"], x["end_line"]) == (10, 90)
    assert (y["line"], y["end_line"]) == (45, 50)
    assert y["parent_id"] == x["id"] and x["children_ids"] == [y["id"]]
    assert y["qualified_name"] == "X.Y"
    assert data["summary"]["source_complete"] and data["summary"]["structure_complete"]
    assert data["source"]["lines"][44]["text"] == "  function Y() {"
    for row in (x, y):
        check_span(row, source, path)
    assert result["coverage"]["status"] == "complete"
    assert result["coverage"]["counts"]["source_analyzed"] == 1
    assert result["coverage"]["stages"]["dependencies"] == "not_run"
    assert result["coverage"]["stages"]["schema"] == "not_run"


def test_all_source_backed_collections_have_ranges(project, capsys):
    source = """import { Router } from 'express';
import {
  Product
} from './model';
const router = Router();
export async function load() {
  return prisma.product.findMany({
    select: { id: true }
  });
}
router.get('/products', load);
export {
  router
};
"""
    path = write(project, source)
    data = invoke(project, capsys, path)["data"]
    for key in ("symbols", "imports", "exports", "calls", "references", "local_routes", "prisma_candidates"):
        assert data[key], key
        for row in data[key]:
            check_span(row, source, path)
    assert data["imports"][1]["line"] == 2 and data["imports"][1]["end_line"] == 4
    assert data["exports"][0]["line"] == 6 and data["exports"][0]["end_line"] == 10
    assert data["exports"][1]["line"] == 12 and data["exports"][1]["end_line"] == 14
    assert data["prisma_candidates"][0]["end_line"] == 9
    assert data["local_routes"][0]["local_route"] == "/products"
    assert data["local_routes"][0]["mount_resolution"] == "not_attempted"
    assert all(row["resolution"] == "not_attempted" for row in data["imports"])


def test_same_line_nesting_and_siblings(project, capsys):
    source = "function X(){function Y(){ return 1; } return Y();} function Z(){}"
    path = write(project, source)
    rows = invoke(project, capsys, path)["data"]["symbols"]
    x, y, z = rows
    assert all(row["line"] == row["end_line"] == 1 for row in rows)
    assert x["parent_id"] is None and z["parent_id"] is None
    assert y["parent_id"] == x["id"]
    assert y["end_column"] < x["end_column"] < z["column"]
    for row in rows:
        check_span(row, source, path)


@pytest.mark.parametrize(
    "name",
    ["brief.ts", "brief.tsx", "brief.js", "brief.jsx", "brief.mts", "brief.cts", "brief.mjs", "brief.cjs"],
)
def test_supported_languages_and_callbacks(project, capsys, name):
    source = """export const outer = () => {
  const value = 1;
  const inner = function* () { yield value; };
  return [value].map(x => x + value);
};
export class Service {
  field = () => { return outer(); };
  method() { return this.field(); }
}
const object = { run() { return outer(); }, done: () => true };
"""
    path = write(project, source, name)
    result = invoke(project, capsys, path)
    assert result["coverage"]["status"] == "complete"
    rows = result["data"]["symbols"]
    names = {r["qualified_name"]: r for r in rows}
    assert {
        "outer",
        "outer.value",
        "outer.inner",
        "Service",
        "Service.field",
        "Service.method",
        "object",
        "object.run",
        "object.done",
    } <= names.keys()
    assert names["Service.field"]["kind"] == "function"
    assert len([r for r in rows if r["name"] == "inner"]) == 1
    assert any(r["anonymous"] and r["parent_id"] == names["outer"]["id"] for r in rows)
    for row in rows:
        check_span(row, source, path)


def test_types_properties_and_anonymous_defaults(project, capsys):
    source = """export interface Item { id: number; run(): void }
type Shape = { field: string };
enum State { Ready = 1 }
export default function() { return 1; }
"""
    path = write(project, source)
    result = invoke(project, capsys, path)
    kinds = {r["kind"] for r in result["data"]["symbols"]}
    assert {"interface", "property", "method", "type", "enum", "enum_member", "function"} <= kinds
    assert any(r["anonymous"] and r["exported"] for r in result["data"]["symbols"])


def test_symbol_selection_and_ambiguity(project, capsys):
    source = """function first() {
  function same() { const inside = 1; return inside; }
  function sibling() {}
  return same();
}
function second() { function same() {} }
"""
    path = write(project, source)
    result = invoke(project, capsys, path, "--symbol", "same", status=2)
    assert "Ambiguous" in result["error"]["message"]
    data = invoke(project, capsys, path, "--symbol", "first.same")["data"]
    first, same, inside = data["symbols"]
    assert [r["selection_role"] for r in data["symbols"]] == ["ancestor", "selected", "descendant"]
    assert same["parent_id"] == first["id"] and inside["parent_id"] == same["id"]
    assert data["source"]["line"] == data["source"]["end_line"] == 2
    assert data["calls"] == []
    assert invoke(project, capsys, path, "--symbol", same["id"])["data"] == data
    assert "Unknown symbol" in invoke(project, capsys, path, "--symbol", "Same", status=2)["error"]["message"]


@pytest.mark.parametrize("ending", ["\n", "\r\n"])
@pytest.mark.parametrize("bom", ["", "\ufeff"])
def test_encoding_and_exact_byte_columns(project, capsys, ending, bom):
    source = bom + ending.join(
        ["const café = '界'; function X(){return 'x';}", "const after = () => X();", ""]
    )
    path = write(project, source)
    data = invoke(project, capsys, path)["data"]
    assert data["summary"]["file_lines"] == 2
    for key in ("symbols", "calls", "references"):
        for row in data[key]:
            check_span(row, source, path)
    x = next(row for row in data["symbols"] if row["name"] == "X")
    assert x["column"] == len("const café = '界'; ".encode()) + 1
    assert (
        data["source_revision"]["sha256"]
        == hashlib.sha256(source.removeprefix("\ufeff").encode("utf-8")).hexdigest()
    )
    assert all(not r["text"].endswith("\r") for r in data["source"]["lines"])


def test_targeted_mode_never_scans_project_or_schema(project, capsys, monkeypatch):
    def forbidden(*args):
        pytest.fail("Targeted analyze must not enumerate the project or load schemas")

    monkeypatch.setattr(Project, "files", forbidden)
    monkeypatch.setattr(Project, "schema_paths", forbidden)
    data = invoke(project, capsys, "backend/src/app.ts")["data"]
    assert data["context"] == {"status": "not_requested"}


def test_opt_in_context_with_source_spans_and_scope(project, capsys):
    path = "backend/src/core/features/products/product.service.ts"
    result = invoke(project, capsys, path, "--app", "erp", "--with-context", "--symbol", "localOnly")
    data, coverage = result["data"], result["coverage"]
    context = data["context"]
    assert context["scope"] == "file" and context["scan_scope"] == "selected_app"
    assert "products" in context["modules"]
    assert context["schema_candidates"][0]["model"] == "Product"
    assert coverage["counts"]["source_analyzed"] > 1
    assert coverage["details"]["scope"]["context_scan"] == "selected_app"
    assert coverage["details"]["scope"]["path"] == path
    for key in ("imports", "consumers", "schema_candidates"):
        assert context[key]
        for row in context[key]:
            check_span(row, (project / row["path"]).read_text(encoding="utf-8"), row["path"])
    assert context["imports"][0]["resolution"] == "external_not_indexed"


def test_missing_context_does_not_hide_target_briefing(project, capsys):
    (project / "backend/prisma/schema.prisma").unlink()
    result = invoke(project, capsys, "backend/src/app.ts", "--with-context", "--app", "erp")
    assert result["data"]["summary"]["source_complete"]
    assert result["data"]["context"]["schema_status"] == "unavailable"
    assert result["coverage"]["status"] == "partial"


def test_invalid_schema_context_preserves_briefing(project, capsys):
    (project / "backend/prisma/schema.prisma").write_text("model Broken {", encoding="utf-8")
    result = invoke(project, capsys, "backend/src/app.ts", "--with-context", "--app", "erp")
    assert result["data"]["summary"]["source_complete"]
    assert result["data"]["context"]["status"] == "partial"
    assert "context_unavailable" in result["coverage"]["gaps"]


@pytest.mark.parametrize("budget", [2048, 4000, 12000, 32000])
def test_display_truncation_counts_are_truthful(project, capsys, budget):
    source = "\n".join(f"export function func{i}() {{ return '" + "x" * 500 + "'; }" for i in range(160))
    path = write(project, source)
    result = invoke(project, capsys, path, budget=budget)
    assert result["truncated"] and result["coverage"]["output_truncated"]
    data = result["data"]
    summary = data["summary"]
    assert summary["file_lines"] == 160 and summary["symbols_total"] == 160
    assert not summary["source_complete"] and not summary["structure_complete"]
    assert summary["source_lines_returned"] == len(data.get("source", {}).get("lines", []))
    assert summary["symbols_returned"] == len(data.get("symbols", []))


def test_shortened_source_line_is_not_complete(project, capsys):
    path = write(project, "// " + "x" * 16000)
    result = invoke(project, capsys, path, budget=8000)
    assert not result["data"]["summary"]["source_complete"]
    assert result["data"]["summary"]["source_lines_requested"] == 1


def test_max_results_limits_source_and_structure(project, capsys):
    path = write(project, "function first() {\n function inner() {}\n}\nfunction last() {}")
    data = invoke(project, capsys, path, "--max-results", "1")["data"]
    assert not data["summary"]["source_complete"] and not data["summary"]["structure_complete"]
    assert data["summary"]["source_lines_returned"] == data["summary"]["symbols_returned"] == 1


def test_cache_upgrade_warm_bypass_and_invalid_outline(project, capsys):
    path = write(project, "function outer(){ function inner(){} }")
    assert main(["--root", str(project), "symbols", path, "--json"]) == 0
    capsys.readouterr()
    cold = invoke(project, capsys, path)
    warm = invoke(project, capsys, path)
    bypass = invoke(project, capsys, path, "--no-cache")
    assert cold["data"] == warm["data"] == bypass["data"]
    assert cold["coverage"]["details"]["cache"]["source_parsed"] == 1
    assert warm["coverage"]["details"]["cache"]["source_cached"] == 1
    with sqlite3.connect(project / "devkit/.cache/source.sqlite3") as conn:
        raw = conn.execute("SELECT data FROM sources WHERE path=?", (path,)).fetchone()[0]
        record = json.loads(raw.split("\n", 1)[1])
        record["info"]["outline"][1]["parent_id"] = "missing"
        payload = json.dumps(record)
        conn.execute(
            "UPDATE sources SET data=? WHERE path=?",
            (hashlib.sha256(payload.encode()).hexdigest() + "\n" + payload, path),
        )
    recovered = invoke(project, capsys, path)
    assert recovered["data"] == cold["data"]
    assert any("corrupt cache" in w for w in recovered["warnings"])


def test_partial_parser_diagnostics_have_ranges_on_warm_cache(project, capsys):
    source = "export function broken() { const x = ; }"
    path = write(project, source)
    for _ in range(2):
        result = invoke(project, capsys, path)
        assert result["coverage"]["status"] == "partial"
        assert result["data"]["diagnostics"]
        for row in result["data"]["diagnostics"]:
            check_span(row, source, path)


@pytest.mark.parametrize(
    "path",
    [
        "backend",
        "notes.txt",
        ".env",
        "backend/node_modules/dependency/index.js",
        "not-found.ts",
        "../escape.ts",
    ],
)
def test_invalid_or_excluded_inputs(project, capsys, path):
    result = invoke(project, capsys, path, status=2)
    assert result["error"]["message"]
    assert "supersecretneedle" not in json.dumps(result)


def test_other_app_and_binary_refused(project, capsys):
    invoke(project, capsys, "hub/src/index.ts", "--app", "erp", status=2)
    path = project / "backend/src/binary.ts"
    path.write_bytes(b"\0secret")
    result = invoke(project, capsys, "backend/src/binary.ts", status=2)
    assert "binary_content" in result["coverage"]["gaps"]


def test_empty_file_and_human_output(project, capsys):
    path = write(project, "")
    data = invoke(project, capsys, path)["data"]
    assert data["summary"]["file_lines"] == 0
    assert data["summary"]["source_complete"] and data["source"]["line"] is None
    write(project, "function outer(){function inner(){}}")
    assert main(["--root", str(project), "analyze", path]) == 0
    captured = capsys.readouterr()
    assert "parent_id:" in captured.out and "end_line:" in captured.out
    assert "backend/src/brief.ts:1:1" in captured.out
    assert "Coverage: complete" in captured.err


def test_reexports_require_and_export_aliases_have_spans(project, capsys):
    source = """const imported = require('./value');
export { thing as other } from './things';
function local() { function hidden() {} }
export { local as renamed };
export * from './more';
"""
    path = write(project, source)
    data = invoke(project, capsys, path)["data"]
    assert {r["kind"] for r in data["imports"]} == {"require", "reexport"}
    assert len(data["exports"]) == 3
    for key in ("imports", "exports"):
        for row in data[key]:
            check_span(row, source, path)
    assert next(r for r in data["symbols"] if r["name"] == "local")["exported"]
    assert not next(r for r in data["symbols"] if r["name"] == "hidden")["exported"]


def test_jsx_component_and_event_callback_spans(project, capsys):
    source = """export const Card = () => (
  <button onClick={() => save('界')}>Save</button>
);
"""
    path = write(project, source, "Card.tsx")
    result = invoke(project, capsys, path)
    assert result["coverage"]["status"] == "complete"
    rows = result["data"]["symbols"]
    assert rows[1]["anonymous"] and rows[1]["parent_id"] == rows[0]["id"]
    for key in ("symbols", "calls", "references", "exports"):
        for row in result["data"][key]:
            check_span(row, source, path)


def test_context_resolves_local_imports_and_reports_missing_roots(project, capsys):
    manifest = project / "devkit/electroerp.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "roots: [backend, frontend]", "roots: [backend, absent_frontend]"
        ),
        encoding="utf-8",
    )
    targeted = invoke(project, capsys, "backend/src/app.ts", "--app", "erp")
    assert targeted["coverage"]["status"] == "complete"
    broad = invoke(project, capsys, "backend/src/app.ts", "--app", "erp", "--with-context")
    assert broad["coverage"]["gaps"]["configured_root_missing_or_not_directory"] == 1
    imports = broad["data"]["context"]["imports"]
    assert imports[1]["resolution"] == "resolved"
    assert imports[1]["resolved"].endswith("product.routes.ts")


def test_explicit_symlink_target_is_rejected(project, capsys):
    target = project / "backend/src/link.ts"
    try:
        target.symlink_to(project / "backend/src/app.ts")
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks unavailable without platform privileges")
    result = invoke(project, capsys, "backend/src/link.ts", status=2)
    assert "symlink" in result["error"]["message"].lower()
