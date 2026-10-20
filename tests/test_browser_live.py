"""Real isolated-browser tests against synthetic local pages, never ElectroERP data."""

from __future__ import annotations

import base64
import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from devkit.browser.client import request
from devkit.browser.storage import INTERNAL, BrowserContext, read_json
from devkit.project import DevkitError


@pytest.fixture
def website():
    payload = base64.urlsafe_b64encode(json.dumps({"exp": time.time() + 3600}).encode()).decode().rstrip("=")
    token = f"eyJhbGciOiJIUzI1NiJ9.{payload}.synthetic_signature_not_a_real_credential"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            self.server.requests.append(self.path)
            if path == "/api/broken":
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            if path == "/api/not-found":
                self.send_response(404)
                self.send_header("X-Secret-Header", "never-show-response-header")
                self.end_headers()
                self.wfile.write(b"never-show-response-body")
                return
            if path == "/api/denied":
                self.send_response(403)
                self.end_headers()
                return
            if path == "/events":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                try:
                    self.wfile.write(b"data: ready\n\n")
                    self.wfile.flush()
                    while not self.server.stopped.wait(0.1):
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
                except OSError:
                    pass
                return
            if path == "/same-redirect":
                self.send_response(302)
                self.send_header("Location", "/core/products")
                self.end_headers()
                return
            if path in {"/redirect", "/api/redirect"}:
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{self.server.server_port}/stolen")
                self.end_headers()
                return
            if path == "/revision":
                body = str(self.server.revision).encode()
            else:
                auth = ""
                if path == "/manual":
                    auth = f"localStorage.setItem('electroerp_token', {json.dumps(token)});"
                if path.startswith("/core") or path == "/manual":
                    auth += """if (!localStorage.getItem('electroerp_token')) { location.replace('/auth/login'); }
                      else { localStorage.setItem('electroerp_org', JSON.stringify({id:'7',name:'Synthetic Org'})); }"""
                if path == "/denied":
                    auth += "fetch('/api/denied');"
                if path == "/clear":
                    auth += "localStorage.removeItem('electroerp_token'); localStorage.removeItem('electroerp_org');"
                if path == "/mutate":
                    auth += "setInterval(async () => { const r = await fetch('/revision'); const s = await r.text(); const e = document.querySelector('h1'); if (e.textContent !== s) e.textContent = s; }, 100);"
                if path == "/stream":
                    auth += """const events = new EventSource('/events?token=' + localStorage.getItem('electroerp_token'));
                      events.onmessage = () => { const e = document.createElement('p'); e.id='events-ready';
                        e.textContent='Stream connected'; document.querySelector('main').appendChild(e); };"""
                if path == "/fetch-redirect":
                    auth += """fetch('/api/redirect', {headers: {Authorization: 'Bearer ' + localStorage.getItem('electroerp_token')}})
                      .catch(() => {const e=document.createElement('p'); e.id='redirect-blocked'; e.textContent='Redirect blocked'; document.querySelector('main').appendChild(e);});"""
                if path == "/diagnostics":
                    auth += f"""document.addEventListener('DOMContentLoaded', async () => {{
                      console.log('Diagnostic log'); console.info('Diagnostic info');
                      console.warn('Diagnostic warning'); console.error('Diagnostic error');
                      console.log('password=never-show-console-password');
                      console.log({json.dumps(token)});
                      console.log('https://user:never-show-url-password@localhost/path?secret=never-show-query#never-show-fragment');
                      console.log({{password:'never-expand-object'}});
                      setTimeout(() => {{ throw new Error('Uncaught diagnostic error password=never-show-error-password'); }}, 0);
                      await fetch('/api/not-found?secret=never-show-network-query', {{headers: {{'X-Secret-Header':'never-show-request-header'}}}});
                      await fetch('/api/broken').catch(() => {{}});
                      const e=document.createElement('p'); e.id='diagnostics-ready'; document.querySelector('main').appendChild(e);
                      e.textContent='Diagnostics ready';
                    }});"""
                if path == "/console-flood":
                    auth += "for(let n=0;n<250;n++) console.log('Flood record ' + n);"
                body = (
                    f"""<!doctype html><html><head><title>Synthetic ERP</title><script>{auth}</script></head>
                  <body><main><h1>Products</h1><section id="summary" style="width:320px;height:120px;background:#acf">Summary</section>
                  <a href="/core/products?token={token}">Catalog</a><button>Save</button>
                  <input type="password" value="never-show-password"><input value="never-show-form-value">
                  <div style="height:1200px"></div><h2>Footer</h2></main></body></html>"""
                ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain" if path == "/revision" else "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.requests = []
    server.revision = 0
    server.stopped = threading.Event()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", server, token
    finally:
        server.stopped.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def live(tmp_path):
    if os.environ.get("DEVKIT_TEST_BROWSER") != "1":
        pytest.skip("Set DEVKIT_TEST_BROWSER=1 to run real isolated-browser integration tests")
    import importlib.util

    assert importlib.util.find_spec("playwright"), "DEVKIT_TEST_BROWSER=1 requires the browser extra"
    root = tmp_path / "ERP with spaces"
    root.mkdir()
    sessions = set()

    def invoke(*args, code=0, session="erp", env=None, headed=False):
        command = [
            sys.executable,
            "-m",
            "devkit",
            "browser",
            *args,
            "--root",
            str(root),
            "--session",
            session,
            "--json",
            "--timeout",
            "3",
        ]
        if args[0] == "open":
            sessions.add(session)
            command += ["--channel", "chromium"]
            if not headed:
                command += ["--headless"]
            if os.environ.get("DEVKIT_TEST_BROWSER_EXECUTABLE"):
                command += ["--executable", os.environ["DEVKIT_TEST_BROWSER_EXECUTABLE"]]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env={**os.environ, **(env or {})},
        )
        assert result.returncode == code, result.stdout + result.stderr
        assert not result.stderr
        return json.loads(result.stdout)

    yield invoke, root
    for session in sessions:
        record = read_json(BrowserContext(root).session(session))
        if record:
            try:
                request(record, "close", {"timeout": 3}, timeout=10)
            except DevkitError:
                pass


def test_shared_session_screenshots_and_no_secret_output(live, website):
    invoke, root = live
    url, _, token = website
    opened = invoke(
        "open",
        url + "/core/products",
        "--token-env",
        "ERP_TEST_TOKEN",
        "--wait-for",
        "main",
        env={"ERP_TEST_TOKEN": token},
    )
    assert opened["data"]["authentication"] == "client_token_present"
    assert opened["data"]["organization_id"] == "7"
    assert opened["data"]["readiness"] == "ready"
    inspection = invoke("inspect")
    rendered = json.dumps(inspection)
    assert (
        token not in rendered
        and "never-show-password" not in rendered
        and "never-show-form-value" not in rendered
    )
    assert "?token=" not in rendered
    heading = next(row["node"] for row in inspection["data"]["nodes"] if row["tag"] == "h1")
    node = invoke("screenshot", "--node", heading)["data"]
    assert node["element"] and Path(node["path"]).read_bytes().startswith(b"\x89PNG\r\n")
    full = invoke("screenshot", "--full-page", "--output", "whole.png")["data"]
    assert full["full_page"] and full["captured"]
    assert str(root) in full["path"]
    selected = invoke("screenshot", "--selector", "#summary")["data"]
    assert selected["element"]
    invoke("screenshot", "--output", "whole.png", code=2)
    invoke("navigate", "/core/products", "--wait-for", "main")
    assert "Stale" in invoke("screenshot", "--node", heading, code=2)["error"]["message"]
    closed = invoke("close")["data"]
    assert closed["closed"] and not closed["logged_out"]
    assert not (root / INTERNAL / "sessions/erp.json").exists()
    assert not list((root / INTERNAL / "auth").iterdir())


def test_manual_login_saved_token_and_origin_binding(live, website):
    invoke, root = live
    url, _, _ = website
    invoke("open", url + "/manual", "--wait-for", "main")
    invoke("close", "--save-auth", "local-login")
    saved = read_json(root / INTERNAL / "auth/local-login.json")
    assert set(saved) == {"version", "origin", "token"}
    assert saved["origin"] == url
    invoke("open", url + "/core/products", "--auth-state", "local-login", "--wait-for", "main")
    assert invoke("inspect")["data"]["authentication"] == "client_token_present"
    invoke("close", "--save-auth", "local-login", code=2)
    invoke("close")
    invoke("open", "http://localhost:1", "--auth-state", "local-login", code=2)


def test_redirects_timeout_api_denials_and_origin_policy(live, website):
    invoke, _ = live
    url, server, _ = website
    result = invoke("open", url + "/core/products", "--wait-for", "main", code=1)
    assert result["data"]["readiness"] in {"login_required", "authentication_required"}
    assert not invoke("screenshot", code=1)["data"]["captured"]
    invoke("navigate", "/auth/login")
    assert invoke("screenshot")["data"]["captured"]
    invoke("navigate", "http://localhost:1/", code=2)
    invoke("navigate", "/redirect", code=2)
    assert "/stolen" not in server.requests
    invoke("navigate", "/manual", "--wait-for", "#missing", code=1)
    invoke("navigate", "/denied", "--wait-for", "#missing", code=1)
    assert invoke("inspect", code=1)["data"]["readiness"] == "access_denied"
    assert invoke("status")["data"]["readiness"] == "access_denied"
    assert any(row["status"] == 403 for row in invoke("network", "--failed")["data"]["requests"])
    invoke("console")
    invoke("close")
    assert not any("logout" in value for value in server.requests)


def test_mutation_stale_refs_and_no_token_resurrection(live, website):
    invoke, _ = live
    url, server, token = website
    invoke("open", url + "/mutate", "--token-env", "ERP_TEST_TOKEN", env={"ERP_TEST_TOKEN": token})
    first = invoke("inspect")["data"]
    reference = first["nodes"][0]["node"]
    server.revision = 42
    time.sleep(0.3)
    invoke("screenshot", "--node", reference, code=2)
    invoke("navigate", "/clear")
    invoke("navigate", "/core/products", code=1)
    assert invoke("inspect", code=1)["data"]["authentication"] == "no_client_token"


def test_rpc_security_and_session_isolation(live, website):
    invoke, root = live
    url, _, _ = website
    invoke("open", url + "/manual")
    invoke("open", url + "/auth/login", session="second")
    assert invoke("inspect", session="second", code=1)["data"]["authentication"] == "no_client_token"
    invoke("open", url + "/", code=2)
    record = read_json(BrowserContext(root).session("erp"))
    for headers in (
        {},
        {"Origin": url, "Authorization": "Bearer " + record["capability"]},
        {"Host": "attacker.example", "Authorization": "Bearer " + record["capability"]},
    ):
        connection = http.client.HTTPConnection("127.0.0.1", record["port"], timeout=5)
        connection.request("POST", "/rpc", b"{}", {"Content-Type": "application/json", **headers})
        response = connection.getresponse()
        assert response.status == 403
        assert record["capability"] not in response.read().decode()
        connection.close()
    invoke("close")
    assert invoke("inspect", session="second", code=1)["data"]["session"] == "second"


def test_expected_organization_is_assertion_not_injection(live, website):
    invoke, _ = live
    url, _, _ = website
    result = invoke("open", url + "/manual", "--organization-id", "8", code=1)
    assert result["data"]["readiness"] == "wrong_organization"
    assert result["data"]["organization_id"] == "7"


def test_visible_browser(live, website):
    if os.environ.get("DEVKIT_TEST_BROWSER_HEADED") != "1":
        pytest.skip("Visible-browser smoke requires a desktop/display")
    invoke, _ = live
    url, _, _ = website
    assert not invoke("open", url + "/auth/login", headed=True)["data"]["headless"]
    status = invoke("status")["data"]
    assert status["session_health"] == "connected" and status["worker_responding"]
    assert status["window_visibility"] == "not_verified"
    assert status["readiness"] == "login_required"
    assert invoke("screenshot")["data"]["captured"]
    invoke("close")


def test_streaming_readiness_and_bounded_inspection(live, website):
    invoke, _ = live
    url, server, token = website
    result = invoke(
        "open",
        url + "/stream",
        "--token-env",
        "ERP_TEST_TOKEN",
        "--wait-for",
        "#events-ready",
        env={"ERP_TEST_TOKEN": token},
    )
    assert result["data"]["readiness"] == "ready"
    assert any(path.startswith("/events?") for path in server.requests)
    inspected = invoke("inspect", "--max-results", "2", "--max-output-chars", "2048")
    assert inspected["truncated"] and len(inspected["data"]["nodes"]) <= 2
    assert token not in json.dumps(inspected)
    assert invoke("screenshot", "--selector", "#events-ready")["data"]["captured"]
    network = invoke("network")["data"]
    stream = next(row for row in network["requests"] if row["resource_type"] == "eventsource")
    assert stream["status"] == 200 and stream["state"] == "response"
    assert stream["timings_ms"]["total"] is None
    assert token not in json.dumps(network) and "?token=" not in json.dumps(network)
    invoke("close")


def test_diagnostics_across_invocations_redaction_and_failed_requests(live, website):
    invoke, _ = live
    url, _, token = website
    invoke("open", url + "/diagnostics", "--wait-for", "#diagnostics-ready")
    status = invoke("status")["data"]
    assert status["session_health"] == "connected" and status["browser_connected"] and status["page_open"]
    assert status["window_visibility"] == "headless" and status["headless"]
    assert status["authentication"] == "no_client_token" and status["organization_id"] is None
    assert "nodes" not in status and status["idle_timeout_seconds"] == 1800
    report = invoke("console")["data"]
    messages = report["messages"]
    assert report["capture"]["observation_errors"] == 0
    assert {"log", "info", "warning", "error"} <= {row["level"] for row in messages}
    assert any(
        row["kind"] == "page_error" and "Uncaught diagnostic error" in row["message"] for row in messages
    )
    assert all(row["level"] == "error" for row in invoke("console", "--level", "error")["data"]["messages"])
    encoded = json.dumps(report)
    assert token not in encoded and "never-show" not in encoded and "never-expand-object" not in encoded
    network = invoke("network")["data"]
    assert network["capture"]["observation_errors"] == 0
    missing = next(row for row in network["requests"] if "/api/not-found" in row["url"])
    assert missing["status"] == 404 and missing["state"] == "finished"
    assert missing["timings_ms"]["total"] is not None
    failed = next(row for row in network["requests"] if "/api/broken" in row["url"])
    assert failed["state"] == "failed" and failed["failure"].startswith("net::ERR_")
    assert "never-show" not in json.dumps(network) and "?secret=" not in json.dumps(network)
    assert not invoke("network", "--since", str(network["capture"]["latest_sequence"]))["data"]["requests"]
    assert all(
        row["state"] == "failed" or (row["status"] or 0) >= 400
        for row in invoke("network", "--failed")["data"]["requests"]
    )
    truncated = invoke("console", "--max-results", "1", "--max-output-chars", "2048")
    assert truncated["truncated"] and len(truncated["data"]["messages"]) <= 1
    cursor = messages[-1]["sequence"]
    assert not invoke("console", "--since", str(cursor))["data"]["messages"]
    invoke("navigate", "/manual")
    authenticated = invoke("status")["data"]
    assert (
        authenticated["authentication"] == "client_token_present" and authenticated["organization_id"] == "7"
    )
    assert any(row["message"] == "Diagnostic log" for row in invoke("console")["data"]["messages"])
    reference = invoke("inspect")["data"]["nodes"][0]["node"]
    invoke("console")
    invoke("network")
    invoke("status")
    assert invoke("screenshot", "--node", reference)["data"]["captured"]
    invoke("close")
    invoke("status", code=2)


def test_diagnostics_retention_and_session_isolation(live, website):
    invoke, _ = live
    url, _, _ = website
    invoke("open", url + "/console-flood")
    data = invoke("console", "--level", "log", "--max-results", "1000", "--max-output-chars", "1000000")[
        "data"
    ]
    assert data["capture"]["retained"] == 200 and data["capture"]["evicted"] >= 50
    assert data["capture"]["history_gap"]
    assert data["messages"][-1]["message"] == "Flood record 249"
    invoke("open", url + "/auth/login", session="second")
    assert not invoke("console", "--level", "log", session="second")["data"]["messages"]
    assert not invoke("network", session="second", code=0)["data"]["capture"]["history_gap"]
    assert invoke("status", session="second")["data"]["readiness"] == "login_required"
    invoke("close")
    invoke("open", url + "/auth/login")
    assert not invoke("console", "--level", "log")["data"]["messages"]


def test_api_redirect_blocked_before_destination_and_same_origin_redirect_allowed(live, website):
    invoke, _ = live
    url, server, token = website
    invoke(
        "open",
        url + "/fetch-redirect",
        "--token-env",
        "ERP_TEST_TOKEN",
        "--wait-for",
        "#redirect-blocked",
        env={"ERP_TEST_TOKEN": token},
    )
    assert "/stolen" not in server.requests
    result = invoke("navigate", "/same-redirect", "--wait-for", "main")
    assert result["data"]["url"] == url + "/core/products"
    assert result["data"]["readiness"] == "ready"
    network = invoke("network")["data"]
    assert any(
        row["url"].endswith("/api/redirect") and row["state"] == "failed" for row in network["requests"]
    )
    assert any(row["url"].endswith("/same-redirect") and row["status"] == 302 for row in network["requests"])


def test_confirmed_stale_session_can_reopen_without_pid_kill(live, website):
    import socket

    invoke, root = live
    url, _, _ = website
    invoke("open", url + "/auth/login")
    record = read_json(BrowserContext(root).session("erp"))
    request(record, "close", {"timeout": 3})  # Simulate a client that lost its cleanup response.
    deadline = time.monotonic() + 5
    while True:
        try:
            with socket.create_connection(("127.0.0.1", record["port"]), timeout=1):
                pass
        except ConnectionRefusedError:
            break
        assert time.monotonic() < deadline
        time.sleep(0.05)
    invoke("open", url + "/auth/login")
    assert read_json(BrowserContext(root).session("erp"))["instance"] != record["instance"]
    invoke("close")
