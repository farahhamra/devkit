"""Private per-session worker; no public CLI, remote debugging port or RPC eval."""

from __future__ import annotations

import hmac
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from ..project import DevkitError
from .client import validate_options
from .policy import MAX_MESSAGE, clean, navigation, redact
from .storage import BrowserContext, read, write_json


def serve(config):
    from .engine import BrowserEngine

    project = BrowserContext(config["root"], app="erp")
    project.initialize()
    instance = config["instance"]
    capability = config.pop("capability")
    ready = project.base / "sessions" / f"{instance}.ready.json"
    cancel = project.base / "sessions" / f"{instance}.cancel"
    engine = None
    server = None
    published = False
    last_use = time.monotonic()
    expected_url = config["url"]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            nonlocal last_use, expected_url
            if (
                self.path != "/rpc"
                or self.headers.get("Host") != f"127.0.0.1:{server.server_port}"
                or self.headers.get("Origin") is not None
                or self.headers.get("Transfer-Encoding") is not None
                or self.headers.get("Content-Type") != "application/json"
                or not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + capability)
            ):
                self.send_error(403)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= MAX_MESSAGE:
                    raise ValueError
                payload = json.loads(self.rfile.read(size))
                if payload.get("instance") != instance or payload.get("action") not in {
                    "ping",
                    "navigate",
                    "inspect",
                    "screenshot",
                    "close",
                }:
                    raise ValueError
                action, options = payload["action"], payload.get("options")
                if not isinstance(options, dict):
                    raise TypeError
                if action != "ping":
                    validate_options(options)
                if action == "navigate":
                    options["url"] = navigation(options["url"], config["origin"])
                    expected_url = options["url"]
                if action == "screenshot":
                    options["expected_url"] = expected_url
                last_use = time.monotonic()
                data, code = engine.run(action, options)
                last_use = time.monotonic()
                result = {"instance": instance, "data": data, "status": code}
            except DevkitError as exc:
                result = {"instance": instance, "error": str(exc)}
            except Exception:  # noqa: BLE001 -- RPC boundary must not disclose driver/page secrets
                result = {
                    "instance": instance,
                    "error": "Invalid request or browser operation failed; raw details omitted to protect credentials.",
                }
            # Labels, selectors, URLs and driver exception messages are never trusted.
            raw = json.dumps(clean(result, engine.secrets), ensure_ascii=True).encode()
            if len(raw) > MAX_MESSAGE:
                raw = json.dumps(
                    {
                        "instance": instance,
                        "error": "Browser response exceeds its transport limit; narrow the operation.",
                    }
                ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(raw)
            except OSError:
                pass

    class Server(HTTPServer):
        def get_request(self):
            connection, address = super().get_request()
            connection.settimeout(2)
            return connection, address

        def handle_error(self, *_):
            pass

    try:
        engine = BrowserEngine(project, config)
        server = Server(("127.0.0.1", 0), Handler)
        server.timeout = 0.1
        data, code = engine.run("navigate", config)
        if read(cancel) is not None:
            return
        record = {
            "version": 1,
            "devkit_version": config["devkit_version"],
            "instance": instance,
            "port": server.server_port,
            "capability": capability,
            "origin": config["origin"],
        }
        write_json(project.session(config["session"]), record, replace=True)
        data.update(idle_timeout_seconds=config["idle_timeout"], allowed_origins=config["allowed_origins"])
        write_json(ready, clean({"instance": instance, "data": data, "status": code}, engine.secrets))
        published = True
        last_use = time.monotonic()
        while not engine.closed:
            if read(cancel) is not None or time.monotonic() - last_use >= config["idle_timeout"]:
                break
            server.handle_request()
            if engine.closed or engine.page.is_closed() or not engine.browser.is_connected():
                break
            # Pump Playwright events while idle so visible manual login/navigation works.
            engine.page.wait_for_timeout(50)
    except Exception as exc:  # noqa: BLE001 -- no unredacted worker tracebacks
        message = (
            str(exc)
            if isinstance(exc, DevkitError)
            else "Browser worker stopped unexpectedly; raw details omitted."
        )
        if engine:
            message = redact(message, engine.secrets)
        try:
            if not published and read(ready) is None:
                write_json(ready, {"instance": instance, "error": message})
        except (DevkitError, OSError):
            pass
    finally:
        if server:
            server.server_close()
        if engine and not engine.closed:
            try:
                engine.close()
            except Exception:  # noqa: BLE001, S110 -- best-effort cleanup after driver failure
                pass
        cancel.unlink(missing_ok=True)


def main():
    try:
        raw = sys.stdin.buffer.readline(MAX_MESSAGE + 1)
        sys.stdin.close()
        if not raw.endswith(b"\n") or len(raw) > MAX_MESSAGE:
            return 2
        serve(json.loads(raw))
        return 0
    except Exception:  # noqa: BLE001 -- bootstrap has no safe diagnostic channel
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
