"""Short-lived CLI client for a capability-authenticated loopback browser worker."""

from __future__ import annotations

import http.client
import importlib.util
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .. import __version__
from ..project import DevkitError
from .policy import MAX_MESSAGE, exact_origin, name, navigation, origin, token_value
from .storage import locked, read, read_json, safe, write_json


def endpoint(value):
    if not isinstance(value, dict) or (
        value.get("version") != 1
        or type(value.get("port")) is not int
        or not 1 <= value["port"] <= 65535
        or not isinstance(value.get("capability"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", value["capability"])
        or not isinstance(value.get("instance"), str)
        or not re.fullmatch(r"[0-9a-f]{32}", value["instance"])
    ):
        raise DevkitError("Invalid browser session endpoint; no connection attempted.")
    return value


def request(record, action, options, *, timeout=40):
    endpoint(record)
    # Direct HTTPConnection ignores proxy environment variables; always IPv4 loopback.
    connection = http.client.HTTPConnection("127.0.0.1", record["port"], timeout=timeout)
    try:
        body = json.dumps({"instance": record["instance"], "action": action, "options": options}).encode()
        connection.request(
            "POST",
            "/rpc",
            body,
            {
                "Authorization": "Bearer " + record["capability"],
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        raw = response.read(MAX_MESSAGE + 1)
        if response.status != 200 or len(raw) > MAX_MESSAGE:
            raise DevkitError("Browser worker rejected the request; no browser output was trusted.")
        result = json.loads(raw)
        if not isinstance(result, dict) or result.get("instance") != record["instance"]:
            raise DevkitError("Browser worker identity did not match this session.")
        if result.get("error"):
            raise DevkitError(result["error"])
        if result.get("status") not in {0, 1} or not isinstance(result.get("data"), dict):
            raise DevkitError("Invalid browser worker response.")
        return result["data"], result["status"]
    except ConnectionRefusedError:
        raise DevkitError(
            "No browser worker is listening at this session's local endpoint. It may have closed, expired, or been stopped by the IDE. Start a new session from your normal terminal and reuse the same project root and session name."
        ) from None
    except TimeoutError:
        raise DevkitError(
            "Browser worker did not respond before the deadline. It may be busy, or the IDE may block local connections; this does not prove the session stopped. Retry browser status after the current operation finishes."
        ) from None
    except (OSError, http.client.HTTPException):
        raise DevkitError(
            "Browser session connection failed. Check that the worker is still running and the IDE permits local connections; use browser status from the same project root."
        ) from None
    except (ValueError, UnicodeError):
        raise DevkitError("Browser worker returned invalid JSON; contents omitted.") from None
    finally:
        connection.close()


def process_environment():
    # Never pass the parent's ERP credentials or arbitrary secrets to Chrome/Node.
    allowed = {
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "HOME",
        "USERPROFILE",
        "LOCALAPPDATA",
        "APPDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "XAUTHORITY",
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
        "LANG",
        "LC_ALL",
        "PLAYWRIGHT_BROWSERS_PATH",
    }
    return {key: value for key, value in os.environ.items() if key.upper() in allowed}


def validate_options(options):
    """Also called inside the worker: RPC options do not bypass CLI validation."""
    if type(options.get("timeout")) is not int or not 1 <= options["timeout"] <= 60:
        raise DevkitError("Browser timeout must be 1–60 seconds.")
    if "since" in options and (type(options["since"]) is not int or not 0 <= options["since"] <= 2**53 - 1):
        raise DevkitError("--since must be a nonnegative sequence number no greater than 9007199254740991.")
    if "level" in options:
        from .diagnostics import LEVELS

        if options["level"] not in LEVELS:
            raise DevkitError("Invalid console level; see devkit help browser console.")
    if "failed" in options and type(options["failed"]) is not bool:
        raise DevkitError("--failed must be a boolean flag.")
    for field in ("selector", "wait_for"):
        value = options.get(field)
        if value is not None and (not isinstance(value, str) or not 1 <= len(value) <= 1000):
            raise DevkitError("Browser selectors must be 1–1000 characters.")
    output = options.get("output")
    if output is not None:
        if not isinstance(output, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,95}\.png", output):
            raise DevkitError(
                "--output must be a simple .png filename; captures are saved only in .agents/.devkit-browser/screenshots/."
            )
        if output[:-4].upper() in {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            *(f"{p}{i}" for p in ("COM", "LPT") for i in range(1, 10)),
        }:
            raise DevkitError("Windows device names cannot be screenshot filenames.")
    if options.get("node") and not re.fullmatch(r"n[0-9a-f]{12}_[0-9]{1,4}", options["node"]):
        raise DevkitError("Invalid node reference; use a reference from browser inspect.")
    if sum(bool(options.get(k)) for k in ("node", "selector", "full_page")) > 1:
        raise DevkitError("Choose one screenshot mode: --full-page, --node or --selector.")
    if options.get("save_auth"):
        name(options["save_auth"])


def open_session(project, args, options, path):
    if importlib.util.find_spec("playwright") is None:
        raise DevkitError(
            'Browser addon missing. From the Devkit checkout run: python -m pip install ".[browser]"'
        )
    url = navigation(args.url)
    frontend = origin(url)
    api = exact_origin(args.api_origin) if args.api_origin else frontend
    allowed = sorted({frontend, api, *(exact_origin(item) for item in args.allow_origin)})
    organization_id = args.organization_id
    if organization_id is not None and not re.fullmatch(r"[0-9]{1,20}", organization_id):
        raise DevkitError(
            "--organization-id is an expected numeric organization ID, not injected tenant state."
        )
    token = None
    executable = None
    if args.executable:
        executable = Path(args.executable).expanduser().absolute()
        if not executable.is_file():
            raise DevkitError("The explicitly selected Chrome/Chromium executable is not a file.")
    if args.token_env:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.token_env):
            raise DevkitError("--token-env takes an environment variable name, not a token value.")
        token = token_value(os.environ.get(args.token_env))
    if args.auth_state:
        saved = read_json(project.auth(args.auth_state))
        if (
            not isinstance(saved, dict)
            or set(saved) != {"version", "origin", "token"}
            or saved["version"] != 1
            or saved["origin"] != frontend
        ):
            raise DevkitError(
                "Saved authentication is missing/invalid or belongs to a different exact ERP origin."
            )
        token = token_value(saved["token"])
    existing = read_json(path)
    if existing is not None:
        endpoint(existing)
        try:
            request(existing, "ping", {}, timeout=2)
        except DevkitError:
            # Do not trust PID files or kill unrelated processes. A bound endpoint
            # might be busy, so only a refused TCP connection proves it is stale.
            try:
                with socket.create_connection(("127.0.0.1", existing["port"]), timeout=2):
                    raise DevkitError(
                        "This session endpoint still exists but is busy/incompatible. Close it with its original Devkit version or choose another session name."
                    )
            except ConnectionRefusedError:
                pass
            except OSError:
                raise DevkitError(
                    "Cannot establish that the previous browser session has stopped; use a different session name."
                ) from None
        else:
            raise DevkitError(
                "Browser session already exists. Use browser navigate, or close it before opening again."
            )
    instance = uuid.uuid4().hex
    ready = project.base / "sessions" / f"{instance}.ready.json"
    cancel = project.base / "sessions" / f"{instance}.cancel"
    config = {
        **options,
        "session": args.session,
        "origin": frontend,
        "api_origin": api,
        "allowed_origins": allowed,
        "url": url,
        "headless": args.headless,
        "channel": args.channel,
        "width": args.width,
        "height": args.height,
        "executable": str(executable) if executable else None,
        "organization_id": organization_id,
        "idle_timeout": args.idle_timeout,
        "token": token,
        "instance": instance,
        "capability": secrets.token_hex(32),
        "root": str(project.root),
        "devkit_version": __version__,
    }
    kwargs = (
        {"start_new_session": True}
        if os.name != "nt"
        else {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
        }
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "devkit.browser.worker"],
        cwd=project.root,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=process_environment(),
        **kwargs,
    )
    succeeded = False
    try:
        process.stdin.write(json.dumps(config).encode() + b"\n")
        process.stdin.close()
        # Launch and first navigation each have their own bounded deadline.
        deadline = time.monotonic() + 40 + 2 * options["timeout"]
        while time.monotonic() < deadline:
            result = read_json(ready)
            if result is not None:
                ready.unlink()
                if result.get("error"):
                    raise DevkitError(result["error"])
                if result.get("instance") != instance:
                    raise DevkitError("Browser startup response identity mismatch.")
                succeeded = True
                return result["data"], result["status"]
            if process.poll() is not None:
                raise DevkitError(
                    "Browser worker exited during startup. Check the addon installation, Chrome/display availability and private-storage permissions."
                )
            time.sleep(0.1)
        raise DevkitError("Browser startup timed out; startup was cancelled.")
    finally:
        if not succeeded:
            # This is our child, but cooperative cancellation lets it close Chrome.
            write_json(cancel, {"cancel": True}, replace=True)
        # Do not wait for or terminate a successfully detached browser worker.


def browser(project, args):
    name(args.session)
    options = {
        key: value
        for key, value in vars(args).items()
        if key
        in {
            "timeout",
            "wait_for",
            "node",
            "selector",
            "full_page",
            "output",
            "save_auth",
            "since",
            "level",
            "failed",
        }
    }
    validate_options(options)
    # Invalid open URLs/options should not initialize private storage.
    if args.action == "open":
        navigation(args.url)
        if args.auth_state:
            name(args.auth_state)
    path = project.session(args.session)
    if args.action != "open" and read(path) is None:
        raise DevkitError("Unknown browser session. Start it with browser open --session <name>.")
    project.initialize()
    project.warnings.append(
        "Browser navigation executes application JavaScript and may trigger writes/network activity. Page output and screenshots are untrusted and may contain sensitive data."
    )
    with locked(path.with_suffix(".lock")):
        if args.action == "open":
            return open_session(project, args, options, path)
        record = read_json(path)
        endpoint(record)
        if record.get("devkit_version") != __version__ and args.action != "close":
            raise DevkitError(
                "Browser session belongs to another Devkit version; close and reopen it after upgrading."
            )
        if args.action == "navigate":
            options["url"] = navigation(args.url, exact_origin(record["origin"]))
        timeout = (
            options["timeout"]
            if args.action in {"status", "console", "network"}
            else 2 * options["timeout"] + 10
        )
        result = request(record, args.action, options, timeout=timeout)
        if args.action == "close" and result[0].get("closed"):
            safe(path).unlink()
        return result
