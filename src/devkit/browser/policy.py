"""ERP browser input policy and credential-safe output. No Playwright import here."""

from __future__ import annotations

import base64
import json
import re
import time
from urllib.parse import urljoin, urlsplit, urlunsplit

from ..project import DevkitError

TOKEN_KEY = "electroerp_token"
ORG_KEY = "electroerp_org"
MAX_MESSAGE = 2 * 1024 * 1024
SENSITIVE = re.compile(r"token|password|secret|authorization|cookie|signature|api.?key", re.IGNORECASE)
JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")


def name(value: str) -> str:
    devices = {"CON", "PRN", "AUX", "NUL", *(f"{p}{i}" for p in ("COM", "LPT") for i in range(1, 10))}
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", value):
        raise DevkitError(
            "Browser names must be 1–48 lowercase ASCII letters, digits, hyphens or underscores, starting with a letter."
        )
    if value.upper() in devices:
        raise DevkitError("Windows device names cannot be browser names.")
    return value


def origin(value: str) -> str:
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise DevkitError("Invalid browser URL or port (value omitted).") from None
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or any(ord(c) < 33 or c == "\\" for c in value)
    ):
        raise DevkitError(
            "Browser URLs require HTTP(S), a host, and no embedded credentials or control characters."
        )
    host = parts.hostname.lower()
    if parts.scheme == "http" and host not in {"localhost", "127.0.0.1", "::1"}:
        raise DevkitError("Plain HTTP browser origins must be loopback; use HTTPS for other hosts.")
    host = f"[{host}]" if ":" in host else host.encode("idna").decode("ascii")
    default = 443 if parts.scheme == "https" else 80
    return f"{parts.scheme}://{host}" + (f":{port}" if port is not None and port != default else "")


def exact_origin(value: str) -> str:
    result = origin(value)
    parts = urlsplit(value)
    if parts.path not in {"", "/"} or parts.query or parts.fragment:
        raise DevkitError("An allowed origin is scheme://host[:port], without a path, query or fragment.")
    return result


def navigation(value: str, frontend: str | None = None) -> str:
    from urllib.parse import parse_qsl

    target = urljoin(frontend + "/", value) if frontend else value
    actual = origin(target)
    if frontend and actual != frontend:
        raise DevkitError("Navigation must stay on the session's exact ERP frontend origin.")
    parts = urlsplit(target)
    if any(SENSITIVE.search(key) for key, _ in parse_qsl(parts.query)) or JWT.search(target):
        raise DevkitError(
            "Do not put authentication credentials in browser URLs; use --token-env or manual login."
        )
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, parts.fragment))


def safe_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        return redact(urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, "", "")))
    except ValueError:
        return "[invalid URL omitted]"


def redact(value: str, secrets=()) -> str:
    for secret in secrets:
        if secret:
            value = value.replace(secret, "[redacted]")
            # DOM labels/URLs have bounded lengths. A truncated JWT must not
            # expose its leading credential bytes just because the full token
            # is no longer present in the string.
            if len(secret) >= 32:
                value = re.sub(re.escape(secret[:32]) + r"[A-Za-z0-9_.-]*", "[redacted]", value)
    return JWT.sub("[redacted]", value)


def clean(value, secrets=()):
    if isinstance(value, dict):
        return {key: clean(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [clean(item, secrets) for item in value]
    if isinstance(value, str):
        return redact(value, secrets)
    return value


def expired(token: str | None) -> bool:
    """Expiry is a client-side hint only; never a signature/authorization verification."""
    if not token:
        return False
    try:
        body = token.split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        exp = payload.get("exp")
        return type(exp) in {int, float} and exp <= time.time() + 5
    except (ValueError, IndexError, AttributeError):
        return False


def token_value(value: str | None) -> str:
    if (
        not isinstance(value, str)
        or not 32 <= len(value) <= 32768
        or not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", value)
    ):
        raise DevkitError(
            "Token environment variable must contain an ERP JWT (32–32768 characters); value omitted."
        )
    if expired(value):
        raise DevkitError(
            "The supplied token is expired according to its client-side expiry claim; obtain a valid session."
        )
    return value


def storage_state(frontend: str, token: str | None) -> dict:
    # Do not manufacture organization state, import cookies, or continuously re-inject a token.
    return {
        "cookies": [],
        "origins": [{"origin": frontend, "localStorage": [{"name": TOKEN_KEY, "value": token}]}]
        if token
        else [],
    }
