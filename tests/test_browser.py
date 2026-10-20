"""Browser policy/storage unit tests; live browser tests are in test_browser_live.py."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import time

import pytest

from devkit.browser.client import endpoint, process_environment, validate_options
from devkit.browser.policy import (
    clean,
    exact_origin,
    expired,
    name,
    navigation,
    origin,
    safe_url,
    storage_state,
    token_value,
)
from devkit.browser.storage import IGNORE, BrowserContext, locked, read, write
from devkit.cli import build_parser, main
from devkit.project import DevkitError, Project


def jwt(exp=None):
    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": exp or time.time() + 3600}).encode()).decode().rstrip("=")
    )
    return f"eyJhbGciOiJIUzI1NiJ9.{payload}.synthetic_signature_not_a_real_credential"


@pytest.mark.parametrize(
    "value", ["../erp", "", "ERP", "a/b", "a\\b", "erp.", "con", "nul", "lpt1", "a" * 49, "a:b", "é"]
)
def test_browser_name_boundaries(value):
    with pytest.raises(DevkitError):
        name(value)


@pytest.mark.parametrize(
    "value",
    [
        "file:///tmp/a",
        "javascript:alert(1)",
        "data:text/html,x",
        "http://example.com",
        "http://localhost.evil",
        "http://user:pw@localhost:3000",
        "http://localhost:bad",
        "http://localhost:3000\\evil",
        "http://localhost:3000/\nfoo",
    ],
)
def test_unsafe_origins(value):
    with pytest.raises(DevkitError):
        origin(value)


def test_origins_navigation_redaction():
    assert origin("HTTPS://ERP.EXAMPLE:443/core?q=1") == "https://erp.example"
    assert origin("http://[::1]:3000/core") == "http://[::1]:3000"
    assert (
        navigation("/core/products?tab=all", "http://localhost:3000")
        == "http://localhost:3000/core/products?tab=all"
    )
    for value in ("//other.example/", "http://localhost:3001/", "/?token=secret", "/?api_key=secret"):
        with pytest.raises(DevkitError):
            navigation(value, "http://localhost:3000")
    with pytest.raises(DevkitError):
        exact_origin("http://localhost:3000/path")
    assert (
        safe_url("http://user:secret@localhost:3000/core?token=secret#secret") == "http://localhost:3000/core"
    )
    token = jwt()
    assert clean({"label": f"Bearer {token}", "x": ['value " with slash \\']}, [token]) == {
        "label": "Bearer [redacted]",
        "x": ['value " with slash \\'],
    }
    assert clean({"label": token[:40]}, [token]) == {"label": "[redacted]"}


def test_token_state_has_no_cookies_or_tenant_guess():
    token = jwt()
    assert token_value(token) == token
    assert not expired(token)
    assert expired(jwt(1))
    with pytest.raises(DevkitError, match="expired"):
        token_value(jwt(1))
    for value in (None, {}, "", "secret", "x" * 33000, "invalid token"):
        with pytest.raises(DevkitError):
            token_value(value)
    state = storage_state("http://localhost:3000", token)
    assert not state["cookies"]
    assert state["origins"][0]["localStorage"] == [{"name": "electroerp_token", "value": token}]
    assert "electroerp_org" not in json.dumps(state)


@pytest.mark.parametrize(
    "options",
    [
        {"timeout": 0},
        {"timeout": True},
        {"timeout": 61},
        {"timeout": 1, "output": "../x.png"},
        {"timeout": 1, "output": "C:\\x.png"},
        {"timeout": 1, "output": "con.png"},
        {"timeout": 1, "node": "n1"},
        {"timeout": 1, "selector": "x", "full_page": True},
        {"timeout": 1, "wait_for": ""},
    ],
)
def test_rpc_option_validation(options):
    with pytest.raises(DevkitError):
        validate_options(options)


def test_private_storage_and_non_overwriting_outputs(tmp_path):
    project = BrowserContext(tmp_path)
    assert not project.base.exists()
    project.initialize()
    assert read(project.base / ".gitignore") == IGNORE
    output = project.base / "screenshots/a.png"
    write(output, b"png")
    with pytest.raises(DevkitError, match="already exists"):
        write(output, b"replaced")
    assert read(output) == b"png"
    if os.name != "nt":
        assert project.base.stat().st_mode & 0o777 == 0o700
        assert output.stat().st_mode & 0o777 == 0o600
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    ignored = subprocess.run(
        ["git", "check-ignore", str(output)], cwd=tmp_path, capture_output=True, check=False
    )
    assert ignored.returncode == 0
    lock = project.base / "sessions/erp.lock"
    with locked(lock):
        if os.name != "nt":
            with pytest.raises(DevkitError, match="Another command"), locked(lock):
                pass
    with locked(lock):
        pass


def test_storage_rejects_links_and_conflicting_ignore(tmp_path):
    project = BrowserContext(tmp_path)
    project.initialize()
    source = tmp_path / "existing"
    source.write_bytes(b"keep")
    target = project.base / "screenshots/linked.png"
    os.link(source, target)
    with pytest.raises(DevkitError, match="hardlinked"):
        read(target)
    with pytest.raises(DevkitError):
        write(target, b"overwrite", replace=True)
    assert source.read_bytes() == b"keep"
    (project.base / ".gitignore").write_bytes(b"custom\n")
    with pytest.raises(DevkitError, match="Conflicting"):
        project.initialize()


def test_browser_help_missing_session_and_wrong_app_do_not_initialize(tmp_path, capsys):
    assert main(["help", "browser", "--json"]) == 0
    help_data = json.loads(capsys.readouterr().out)["data"]
    assert len(help_data) == 9 and all("read-only runtime guarantee" in row["scope"] for row in help_data)
    assert not list(tmp_path.iterdir())
    assert main(["browser", "inspect", "--session", "erp", "--root", str(tmp_path), "--json"]) == 2
    assert "Unknown browser session" in capsys.readouterr().out
    assert not list(tmp_path.iterdir())
    assert (
        main(
            [
                "browser",
                "open",
                "http://localhost:3000",
                "--session",
                "erp",
                "--app",
                "hub",
                "--root",
                str(tmp_path),
                "--json",
            ]
        )
        == 2
    )
    assert "ERP only" in capsys.readouterr().out
    assert not list(tmp_path.iterdir())


def test_visible_default_and_explicit_auth_options():
    parser, _ = build_parser()
    args = parser.parse_args(["browser", "open", "http://localhost:3000", "--session", "erp"])
    assert not args.headless and args.channel == "chrome"
    assert args.idle_timeout == 1800
    assert args.token_env is None and args.auth_state is None
    with pytest.raises(DevkitError):
        parser.parse_args(
            ["browser", "open", "http://localhost:3000", "--session", "erp", "--token", "secret"]
        )
    with pytest.raises(DevkitError):
        parser.parse_args(
            [
                "browser",
                "open",
                "http://localhost:3000",
                "--session",
                "erp",
                "--token-env",
                "TOKEN",
                "--auth-state",
                "saved",
            ]
        )


def test_child_environment_excludes_tokens_and_app_secrets(monkeypatch):
    monkeypatch.setenv("ELECTROERP_TOKEN", jwt())
    monkeypatch.setenv("DATABASE_URL", "must-not-be-inherited")
    monkeypatch.setenv("HTTPS_PROXY", "must-not-receive-local-rpc")
    environment = process_environment()
    assert {"ELECTROERP_TOKEN", "DATABASE_URL", "HTTPS_PROXY"}.isdisjoint(environment)


@pytest.mark.parametrize(
    "record",
    [
        None,
        {},
        {"version": 1, "port": "80"},
        {"version": 1, "port": True},
        {"version": 1, "port": 80, "capability": "short"},
    ],
)
def test_invalid_rpc_endpoints_rejected(record):
    with pytest.raises(DevkitError):
        endpoint(record)


def test_internal_browser_files_excluded_even_with_empty_ignore(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVKIT_CONFIG_DIR", str(tmp_path / "devkit"))
    (tmp_path / "devkit").mkdir()
    (tmp_path / "devkit/.ignore").write_text("", encoding="utf-8")
    browser = BrowserContext(tmp_path)
    browser.initialize()
    write(browser.auth("private"), b"never-index-browser-credentials")
    project = Project(str(tmp_path))
    assert all(".devkit-browser" not in str(path) for path in project.files())


def test_tracked_browser_artifacts_fail_closed(tmp_path):
    browser = BrowserContext(tmp_path)
    browser.initialize()
    auth = browser.auth("tracked")
    write(auth, b"synthetic-not-real")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "add", "-f", str(auth)], cwd=tmp_path, check=True)
    with pytest.raises(DevkitError, match="already tracked"):
        browser.initialize()
    assert read(auth) == b"synthetic-not-real"


def test_browser_argument_errors_do_not_echo_credentials(tmp_path, capsys):
    token = jwt()
    assert (
        main(
            [
                "browser",
                "open",
                "http://localhost:3000",
                "--session",
                "erp",
                "--token",
                token,
                "--root",
                str(tmp_path),
                "--json",
            ]
        )
        == 2
    )
    output = capsys.readouterr()
    assert token not in output.out + output.err
    assert "values omitted" in output.out
    assert not list(tmp_path.iterdir())


def test_browser_storage_rejects_directory_symlink(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / ".agents").mkdir()
    try:
        (tmp_path / ".agents/.devkit-browser").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation unavailable for this account")
    with pytest.raises(DevkitError, match="symlinks"):
        BrowserContext(tmp_path).initialize()
    assert not list(outside.iterdir())


def test_session_lock_excludes_other_process(tmp_path):
    import sys

    browser = BrowserContext(tmp_path)
    browser.initialize()
    lock = browser.base / "sessions/exclusive.lock"
    script = "from pathlib import Path; from devkit.browser.storage import locked; import sys;\nwith locked(Path(sys.argv[1])): pass"
    with locked(lock):
        result = subprocess.run([sys.executable, "-c", script, str(lock)], capture_output=True, check=False)
        assert result.returncode != 0 and b"Another command" in result.stderr
    result = subprocess.run([sys.executable, "-c", script, str(lock)], capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
