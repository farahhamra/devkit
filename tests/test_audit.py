"""Audit contracts, conservative unknowns, scanner boundaries and real optional engines."""

import json
import os
import sys
import zipfile
from types import SimpleNamespace

import pytest
import yaml

from devkit.audit.engines import Inputs, environment, vulnerability_severity
from devkit.cli import main
from devkit.project import DevkitError, Project


def invoke(root, capsys, *args, status=0, budget=200000):
    code = main(
        ["--root", str(root), "audit", *args, "--app", "erp", "--json", "--max-output-chars", str(budget)]
    )
    out = capsys.readouterr()
    assert code == status, out.out + out.err
    assert not out.err
    assert len(out.out) <= budget
    return json.loads(out.out)


def write(root, name, content):
    path = root / "backend/src/audit-test" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path.relative_to(root).as_posix()


def policy(root, data):
    path = root / "devkit/audit.yaml"
    path.write_text(yaml.safe_dump({"version": 1, **data}), encoding="utf-8")


def route_setup(
    root,
    *,
    mount="app.use('/api', auth, router);",
    registration="router.get('/items', permissions(['items.read']), roles('admin'), handler);",
    extra="",
):
    guards = write(
        root,
        "guards.ts",
        "export function auth(req,res,next) { next(); }\nexport function permissions(values) { return auth; }\nexport function roles(values) { return auth; }",
    )
    write(
        root,
        "routes.ts",
        "import { Router } from 'express';\nimport { permissions, roles } from './guards';\nconst router = Router();\nfunction handler(req,res) {}\n"
        + registration
        + "\nexport default router;",
    )
    app = write(
        root,
        "app.ts",
        "import express from 'express';\nimport {auth} from './guards';\nimport router from './routes';\nconst app = express();\n"
        + mount
        + "\n"
        + extra,
    )
    policy(
        root,
        {
            "entrypoints": [{"path": app, "router": "app"}],
            "guards": {
                "authentication": [{"path": guards, "symbol": "auth"}],
                "permissions": [{"path": guards, "symbol": "permissions", "mode": "all"}],
                "roles": [{"path": guards, "symbol": "roles", "mode": "any"}],
            },
            "routes": [
                {
                    "app": "erp",
                    "method": "GET",
                    "route": "/api/items",
                    "authentication": "required",
                    "permissions": ["items.read"],
                    "roles": ["admin"],
                }
            ],
        },
    )
    return "backend/src/audit-test"


def test_ordered_imported_mount_and_guard_spans(project, capsys):
    target = route_setup(project)
    result = invoke(project, capsys, "routes", target)
    checks = result["data"]["checks"]
    assert [c["status"] for c in checks] == ["pass", "pass", "pass"]
    assert result["data"]["summary"]["fail"] == 0
    for row in checks:
        assert row["line"] == row["end_line"] == 5
        assert len(row["middleware"]) == 3
        assert row["middleware"][0]["path"].endswith("app.ts")
        assert row["middleware"][0]["definition"]["symbol"] == "auth"


@pytest.mark.parametrize(
    "mount",
    [
        "app.use('/api', router); app.use(auth);",
        "app.use('/api', router, auth);",
        "app.use('/other', auth); app.use('/api', router);",
    ],
)
def test_late_or_wrong_prefix_auth_does_not_protect(project, capsys, mount):
    target = route_setup(project, mount=mount)
    result = invoke(project, capsys, "routes", target, status=1)
    assert result["data"]["summary"]["fail"] == 1
    assert result["data"]["findings"][0]["rule"] == "route_authentication"


def test_prior_prefix_auth_and_barrel_alias(project, capsys):
    target = route_setup(project, mount="app.use('/api', auth); app.use('/api', router);")
    app = project / target / "app.ts"
    write(project, "barrel.ts", "export {auth as authenticate} from './guards';")
    app.write_text(
        app.read_text().replace("{auth} from './guards'", "{authenticate as auth} from './barrel'")
    )
    result = invoke(project, capsys, "routes", target)
    assert result["data"]["summary"]["pass"] == 3


@pytest.mark.parametrize(
    "registration",
    [
        "router.get('/items', permissions(someValues), roles('admin'), handler);",
        "if (enabled) router.get('/items', permissions(['items.read']), roles('admin'), handler);",
        "router.route('/items').get(handler);",
    ],
)
def test_dynamic_guards_and_registrations_unknown(project, capsys, registration):
    target = route_setup(project, registration=registration)
    result = invoke(project, capsys, "routes", target)
    assert result["data"]["summary"]["unknown"]
    assert result["coverage"]["status"] == "partial"


def test_any_guard_does_not_satisfy_all_permissions(project, capsys):
    target = route_setup(
        project,
        registration="router.get('/items', permissions(['items.read', 'other']), roles('admin'), handler);",
    )
    path = project / "devkit/audit.yaml"
    data = yaml.safe_load(path.read_text())
    data["guards"]["permissions"][0]["mode"] = "any"
    path.write_text(yaml.safe_dump(data))
    result = invoke(project, capsys, "routes", target, status=1)
    assert [c["rule"] for c in result["data"]["checks"] if c["status"] == "fail"] == ["route_permissions"]


def test_same_named_untrusted_guard_does_not_pass(project, capsys):
    target = route_setup(project)
    write(project, "different.ts", "export function auth(req,res,next) { next(); }")
    app = project / target / "app.ts"
    app.write_text(app.read_text().replace("{auth} from './guards'", "{auth} from './different'"))
    result = invoke(project, capsys, "routes", target, status=1)
    assert result["data"]["summary"]["fail"] == 1


def test_multiple_same_file_routers_do_not_leak_auth(project, capsys):
    target = route_setup(project, mount="app.use('/api', router);")
    write(
        project,
        "routes.ts",
        "import { Router } from 'express';\nimport {auth,permissions,roles} from './guards';\nconst router = Router();\nconst privateRouter=Router();\nprivateRouter.use(auth);\nfunction handler(){}\nrouter.get('/items', permissions('items.read'), roles('admin'), handler);\nexport default router;",
    )
    result = invoke(project, capsys, "routes", target, status=1)
    assert result["data"]["summary"]["fail"] == 1


def test_imported_router_mutation_preserves_actual_span_and_is_unknown(project, capsys):
    target = route_setup(project, extra="router.use(auth);")
    result = invoke(project, capsys, "routes", target)
    row = next(c for c in result["data"]["checks"] if c["rule"] == "route_registration")
    assert row["status"] == "unknown"
    assert row["path"].endswith("/app.ts")
    assert row["line"] == row["end_line"] == 6


def test_missing_policy_is_unknown_not_clean_security(project, capsys):
    result = invoke(project, capsys, "routes")
    assert result["data"]["policy"]["configured"] is False
    assert result["data"]["summary"]["unknown"] > 0
    assert result["data"]["summary"]["complete"] is False


def test_strict_unknown_check_exits_one(project, capsys):
    result = invoke(project, capsys, "routes", "--strict", status=1)
    assert result["data"]["summary"]["strict"] is True
    assert result["data"]["summary"]["fail"] == 0


def test_missing_guard_catalog_does_not_invent_failures(project, capsys):
    target = route_setup(project)
    path = project / "devkit/audit.yaml"
    value = yaml.safe_load(path.read_text())
    value.pop("guards")
    path.write_text(yaml.safe_dump(value))
    result = invoke(project, capsys, "routes", target)
    assert result["data"]["summary"]["unknown"] == 3
    assert result["data"]["summary"]["fail"] == 0


def test_extra_guard_arguments_are_not_silently_ignored(project, capsys):
    target = route_setup(
        project,
        registration="router.get('/items', permissions(['items.read'], extraValues), roles('admin'), handler);",
    )
    result = invoke(project, capsys, "routes", target)
    assert (
        next(c for c in result["data"]["checks"] if c["rule"] == "route_permissions")["status"] == "unknown"
    )


@pytest.mark.parametrize(
    "body",
    [
        "version: 1\nversion: 1\n",
        "version: 1\nroutez: []\n",
        "version: true\n",
        "version: 1\nunused:\n  entry: ['../escape.ts']\n",
        "version: 1\nentrypoints: nope\n",
    ],
)
def test_invalid_policy_rejected(project, capsys, body):
    (project / "devkit/audit.yaml").write_text(body)
    result = invoke(project, capsys, "routes", status=2)
    assert "error" in result


def isolation_setup(root, source):
    path = write(root, "scope.ts", source)
    policy(
        root,
        {
            "isolation": {
                "models": [
                    {
                        "app": "erp",
                        "model": "Product",
                        "field": "organizationId",
                        "receivers": ["prisma", "tx"],
                        "trusted_values": ["ctx.organizationId"],
                    }
                ]
            }
        },
    )
    return path


@pytest.mark.parametrize(
    ("where", "status"),
    [
        ("{organizationId: ctx.organizationId}", "pass"),
        ("{organizationId: {equals: ctx.organizationId}}", "pass"),
        ("{AND: [{id: 3}, {organizationId: ctx.organizationId}]}", "pass"),
        ("{OR: [{organizationId: ctx.organizationId}, {organizationId: ctx.organizationId, id: 4}]}", "pass"),
        ("{OR: [{organizationId: ctx.organizationId}, {id: 4}]}", "fail"),
        ("{NOT: {organizationId: ctx.organizationId}}", "fail"),
        ("{id: 1}", "fail"),
        ("{note: 'organizationId: ctx.organizationId'}", "fail"),
        ("{organizationId: req.query.organizationId}", "unknown"),
        ("{organizationId: ctx.organizationId, ...other}", "unknown"),
        ("filter", "unknown"),
    ],
)
def test_scope_predicate_semantics(project, capsys, where, status):
    path = isolation_setup(project, "prisma.product.findMany({where: " + where + "});")
    result = invoke(project, capsys, "isolation", path, status=1 if status == "fail" else 0)
    assert result["data"]["checks"][0]["status"] == status
    assert result["data"]["checks"][0]["line"] == 1
    assert result["data"]["checks"][0]["end_line"] == 1


@pytest.mark.parametrize(
    ("source", "status"),
    [
        ("prisma.product.findMany({select:{organizationId:true}})", "fail"),
        ("prisma.product.findMany()", "fail"),
        ("prisma.product.create({data:{organizationId:ctx.organizationId,name:'a'}})", "pass"),
        ("prisma.product.createMany({data:[{organizationId:ctx.organizationId},{name:'b'}]})", "fail"),
        ("prisma.product.update({where:{id:2},data:{organizationId:ctx.organizationId}})", "fail"),
        ("prisma.product.upsert({where:{organizationId:ctx.organizationId}})", "unknown"),
        ("other.product.findMany({where:{organizationId:ctx.organizationId}})", "unknown"),
        ("prisma['product'].findMany({where:{organizationId:ctx.organizationId}})", "unknown"),
    ],
)
def test_isolation_create_update_and_receiver_contracts(project, capsys, source, status):
    path = isolation_setup(project, source)
    result = invoke(project, capsys, "isolation", path, status=1 if status == "fail" else 0)
    assert result["data"]["checks"][0]["status"] == status


def test_audit_limits_preserve_full_counts(project, capsys):
    path = isolation_setup(project, "prisma.product.findMany();\n" * 150)
    result = invoke(project, capsys, "isolation", path, "--max-results", "1", status=1, budget=3200)
    assert result["truncated"]
    assert result["data"]["summary"]["fail"] == 150
    assert result["data"]["summary"]["findings"] == 150


def test_scanner_environment_strips_execution_hooks(monkeypatch, tmp_path):
    for name in ("NODE_OPTIONS", "PYTHONPATH", "SEMGREP_APP_TOKEN", "OSV_SCANNER_CONFIG", "GITHUB_TOKEN"):
        monkeypatch.setenv(name, "sensitive-do-not-forward")
    env = environment(tmp_path)
    assert "sensitive-do-not-forward" not in env.values()
    assert env["SEMGREP_SEND_METRICS"] == "off"


def test_snapshot_boundary_positions_and_ignore(project, tmp_path):
    path = write(project, "unicode.ts", "const café = '😀';\r\nconst unused = 1;\r\n")
    write(project, "secret.env", "DO_NOT_STAGE")
    context = Project(project, app="erp")
    args = SimpleNamespace(action="security", path="backend/src/audit-test")
    inputs = Inputs(context, args, tmp_path / "stage")
    assert list(inputs.snapshots) == [path]
    location = inputs.location(path, utf16=(2, 7, 2, 13))
    snapshot = inputs.snapshots[path]
    assert snapshot.content[location["start_byte"] : location["end_byte"]] == b"unused"
    for bad in ("../escape.ts", "/etc/passwd", "backend/src/nope.ts"):
        with pytest.raises(DevkitError):
            inputs.path(bad)
    with pytest.raises(DevkitError):
        inputs.location(path, 0, 10000)


def test_missing_engine_is_error_not_clean(project, capsys, monkeypatch):
    path = write(project, "candidate.ts", "eval(input);")
    monkeypatch.setattr(
        "devkit.audit.engines.executable", lambda _: (_ for _ in ()).throw(DevkitError("missing engine"))
    )
    result = invoke(project, capsys, "security", path, status=2)
    assert result["coverage"]["status"] == "partial"


def test_unknown_dependency_severity_is_not_assumed_low():
    assert vulnerability_severity({"id": "TEST-1"}, []) == "unknown"
    assert (
        vulnerability_severity({"id": "TEST-1"}, [{"ids": ["TEST-1"], "max_severity": "9.8"}]) == "critical"
    )


def test_scanner_timeout_kills_and_reports_error(tmp_path):
    from devkit.audit.engines import run

    with pytest.raises(DevkitError, match="timed out"):
        run([sys.executable, "-c", "import time; time.sleep(3)"], tmp_path, 0.1)


def test_scanner_failure_withholds_raw_source_or_secret(tmp_path, capsys):
    from devkit.audit.engines import run

    with pytest.raises(DevkitError, match="exit code 3") as error:
        run(
            [sys.executable, "-c", "import sys; print('secret-fixture-value', file=sys.stderr); sys.exit(3)"],
            tmp_path,
            5,
        )
    assert "secret-fixture-value" not in str(error.value)
    assert not capsys.readouterr().out


def test_semgrep_skipped_inputs_and_invalid_spans(project, capsys, monkeypatch):
    path = write(project, "candidate.ts", "eval(input);")
    monkeypatch.setattr("devkit.audit.engines.executable", lambda _: "semgrep")
    monkeypatch.setattr("devkit.audit.engines.version", lambda *a: "1.180.0")
    report = {"results": [], "errors": [], "paths": {"scanned": []}}
    monkeypatch.setattr("devkit.audit.engines.run", lambda *a, **k: (json.dumps(report), 0))
    result = invoke(project, capsys, "security", path)
    assert result["coverage"]["status"] == "partial"
    assert result["data"]["summary"]["unknown"] == 1
    report["results"] = [
        {
            "check_id": "dynamic-eval",
            "path": path,
            "start": {"offset": 0},
            "end": {"offset": 999},
            "extra": {"severity": "ERROR"},
        }
    ]
    invoke(project, capsys, "security", path, status=2)


def test_malformed_scanner_json_is_error(project, capsys, monkeypatch):
    path = write(project, "candidate.ts", "eval(input);")
    monkeypatch.setattr("devkit.audit.engines.executable", lambda _: "semgrep")
    monkeypatch.setattr("devkit.audit.engines.version", lambda *a: "1.180.0")
    monkeypatch.setattr("devkit.audit.engines.run", lambda *a, **k: ("not JSON", 0))
    result = invoke(project, capsys, "security", path, status=2)
    assert "Malformed JSON" in result["error"]["message"]


engines = pytest.mark.skipif(
    os.environ.get("DEVKIT_TEST_AUDIT_ENGINES") != "1",
    reason="Optional real audit engines; set DEVKIT_TEST_AUDIT_ENGINES=1 after installation",
)


@engines
def test_real_semgrep_severity_and_secret_redaction(project, capsys):
    path = write(project, "security.ts", "const password = 'do-not-echo-this-test-secret';\neval(input);\n")
    result = invoke(project, capsys, "security", path, status=1)
    assert "do-not-echo-this-test-secret" not in json.dumps(result)
    assert {row["rule"] for row in result["data"]["findings"]} >= {
        "security_dynamic-eval",
        "security_hardcoded-secret",
    }
    assert result["coverage"]["stages"]["audit"] == "complete"
    assert result["data"]["findings"][0]["end_line"] >= 1


@engines
def test_real_unused_local_and_graph(project, capsys):
    path = write(
        project, "main.ts", "import {used} from './lib';\nconsole.log(used());\nconst forgotten = 1;"
    )
    write(project, "lib.ts", "export function used(){return 1;}\nexport function unused(){return 2;}")
    write(project, "orphan.ts", "export const orphan=2;")
    result = invoke(project, capsys, "unused", "backend/src/audit-test", "--entry", path)
    rules = {row["rule"] for row in result["data"]["findings"]}
    assert {"unused_local", "unused_exports", "unused_files"} <= rules
    assert {e["name"] for e in result["data"]["engines"]} == {"knip", "eslint"}
    for row in result["data"]["findings"]:
        assert row["line"] <= row["end_line"]


@engines
def test_real_jscpd_both_occurrences(project, capsys):
    code = "export function calculate(value) {\n const doubled = value * 2;\n const total = doubled + 10;\n console.log(total);\n return total;\n}\n"
    write(project, "a.ts", code)
    write(project, "b.ts", code)
    result = invoke(
        project, capsys, "duplicates", "backend/src/audit-test", "--min-lines", "3", "--min-tokens", "10"
    )
    assert result["data"]["findings"]
    row = result["data"]["findings"][0]
    assert row["path"] != row["related"][0]["path"]
    assert row["line"] < row["end_line"]


@engines
def test_real_unused_multiple_packages_and_no_project_config_execution(project, capsys):
    backend = write(project, "main.ts", "export const used = 1;")
    (project / "backend/package.json").write_text(
        '{"name":"backend","dependencies":{"lodash":"4.17.15"},"scripts":{"prepare":"this-must-not-run"}}'
    )
    (project / "frontend/main.ts").write_text("export const entry = 1;")
    (project / "frontend/package.json").write_text('{"name":"frontend","dependencies":{"moment":"2.20.0"}}')
    sentinel = project / "executed-sentinel"
    malicious = (
        "require('fs').writeFileSync("
        + json.dumps(str(sentinel))
        + ", 'executed'); throw Error('project config executed');"
    )
    (project / "backend/knip.config.js").write_text(malicious)
    (project / "backend/eslint.config.js").write_text(malicious)
    result = invoke(project, capsys, "unused", "--entry", backend, "--entry", "frontend/main.ts")
    deps = [r for r in result["data"]["findings"] if r["rule"] == "unused_dependencies"]
    assert {r["name"] for r in deps} == {"lodash", "moment"}
    assert not sentinel.exists()
    assert {r["path"] for r in deps} == {"backend/package.json", "frontend/package.json"}


@engines
def test_real_unused_without_entries_is_local_only_and_partial(project, capsys):
    path = write(project, "local.ts", "const café='😀'; const unused=1;")
    result = invoke(project, capsys, "unused", path)
    assert {r["name"] for r in result["data"]["engines"]} == {"eslint"}
    assert result["data"]["summary"]["unknown"] == 1
    assert result["coverage"]["status"] == "partial"


@engines
def test_real_unused_workspace_without_entry_is_unknown(project, capsys):
    backend = write(project, "main.ts", "export const used=1;")
    (project / "backend/package.json").write_text('{"name":"backend"}')
    (project / "frontend/main.ts").write_text("export const entry=1;")
    (project / "frontend/package.json").write_text('{"name":"frontend"}')
    result = invoke(project, capsys, "unused", "--entry", backend)
    assert any(r["rule"] == "knip_workspace_entries" for r in result["data"]["checks"])
    assert not any(
        r["path"] == "frontend/main.ts" and r["rule"] == "unused_files" for r in result["data"]["findings"]
    )


@engines
def test_real_osv_offline_advisory_and_lockfile_span(project, capsys, tmp_path):
    # An intentionally synthetic advisory database exercises the real matcher without network.
    database = tmp_path / "offline-db"
    (database / "osv-scalibr/npm").mkdir(parents=True)
    advisory = {
        "schema_version": "1.6.0",
        "id": "TEST-DEVKIT-1",
        "modified": "2026-01-01T00:00:00Z",
        "published": "2026-01-01T00:00:00Z",
        "summary": "Synthetic integration-test advisory",
        "database_specific": {"severity": "HIGH"},
        "affected": [
            {
                "package": {"name": "devkit-audit-fixture", "ecosystem": "npm"},
                "ranges": [{"type": "SEMVER", "events": [{"introduced": "0"}, {"fixed": "2.0.0"}]}],
            }
        ],
    }
    with zipfile.ZipFile(database / "osv-scalibr/npm/all.zip", "w") as archive:
        archive.writestr("TEST-DEVKIT-1.json", json.dumps(advisory))
    path = project / "backend/package-lock.json"
    path.write_text(
        json.dumps(
            {
                "name": "fixture",
                "version": "1.0.0",
                "lockfileVersion": 3,
                "packages": {
                    "": {"name": "fixture"},
                    "node_modules/devkit-audit-fixture": {
                        "version": "1.0.0",
                        "resolved": "https://registry.npmjs.org/devkit-audit-fixture/-/devkit-audit-fixture-1.0.0.tgz",
                    },
                },
            },
            indent=2,
        )
    )
    result = invoke(
        project,
        capsys,
        "dependencies",
        "backend/package-lock.json",
        "--offline",
        "--offline-db",
        str(database),
        status=1,
    )
    assert result["data"]["engines"][0]["network"] is False
    row = result["data"]["findings"][0]
    assert row["advisory"] == "TEST-DEVKIT-1"
    assert row["severity"] == "high"
    assert row["path"] == "backend/package-lock.json"
    assert row["line"] < row["end_line"]
