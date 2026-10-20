"""Bounded, memory-only browser observations. Never read HTTP bodies or headers."""

from __future__ import annotations

import copy
import math
import re
import weakref
from collections import OrderedDict, deque
from datetime import UTC, datetime
from urllib.parse import urlsplit

from .policy import redact, safe_url

CAPACITY = 200
LEVELS = ("all", "error", "warning", "info", "log", "debug", "other")
URL = re.compile(r"\b(?:https?|wss?)://[^\s<>\"'`]+", re.IGNORECASE)
PRIVATE_URL = re.compile(r"\b(?:data|file|blob):[^\s<>\"'`]+", re.IGNORECASE)
CREDENTIAL = re.compile(
    r"(?i)(?<![\w-])(?:[\"']?)(?:[\w-]*token|password|passwd|[\w-]*secret|"
    r"api[_-]?key|authorization|(?:set[-_])?cookie)(?:[\"']?)\s*[:=]\s*"
    r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\r\n,}]+)"
)
AUTHORIZATION = re.compile(r"(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9_./+=-]+")
JWT_PREFIX = re.compile(r"\beyJ[A-Za-z0-9_-]{6,}(?:\.[A-Za-z0-9_-]*){0,2}")
SENSITIVE_LINE = re.compile(r"(?im)\b(?:authorization|(?:set[-_])?cookie)\s*[:=]\s*[^\r\n]+")
SPACED_CREDENTIAL = re.compile(
    r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|refresh[_-]?token)\s+\S+"
)


def now():
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def text(value, secrets, limit=512):
    # Bound redaction work too. Prefix redaction in policy.redact protects known
    # credentials crossing this cutoff. Console text is still not a PII sanitizer.
    value = str(value)
    clipped = len(value) > 16384
    value = redact(value[:16384], secrets)
    value = JWT_PREFIX.sub("[redacted]", value)
    value = SENSITIVE_LINE.sub("[credential redacted]", value)
    value = CREDENTIAL.sub("[credential redacted]", value)
    value = SPACED_CREDENTIAL.sub("[credential redacted]", value)
    value = AUTHORIZATION.sub("[credential redacted]", value)
    value = PRIVATE_URL.sub("[non-HTTP URL omitted]", value)
    value = URL.sub(lambda match: safe_url(match[0]), value)
    # Prevent terminal control/escape injection; retain readable error lines.
    value = "".join(c for c in value if c in "\n\t" or (ord(c) >= 32 and not 127 <= ord(c) < 160))
    return value[:limit], clipped or len(value) > limit


def diagnostic_url(value, secrets, limit=512):
    try:
        if urlsplit(value).scheme not in {"http", "https", "ws", "wss"}:
            return "[non-HTTP URL omitted]"
        return text(safe_url(value), secrets, limit)[0]
    except (TypeError, ValueError):
        return "[invalid URL omitted]"


def span(timing, start, end):
    left, right = timing.get(start), timing.get(end)
    if (
        type(left) not in {int, float}
        or type(right) not in {int, float}
        or not math.isfinite(left)
        or not math.isfinite(right)
        or left < 0
        or right < left
    ):
        return None
    return round(right - left, 3)


def timings(request):
    timing = request.timing
    return {
        "dns": span(timing, "domainLookupStart", "domainLookupEnd"),
        "connect": span(timing, "connectStart", "connectEnd"),
        "tls": span(timing, "secureConnectionStart", "connectEnd"),
        "ttfb": span(timing, "requestStart", "responseStart"),
        "download": span(timing, "responseStart", "responseEnd"),
        "total": span({**timing, "zero": 0}, "zero", "responseEnd"),
    }


class Diagnostics:
    def __init__(self, secrets):
        self.secrets = secrets  # callable: manual login can change the known token set
        self.started_at = now()
        self.console = deque(maxlen=CAPACITY)
        # Weak keys do not retain Request objects and their potentially huge/secret
        # post data. Only sanitized fixed-shape records are retained here.
        self.requests = OrderedDict()
        self.console_sequence = 0
        self.network_sequence = 0
        self.request_count = 0
        self.console_evicted = 0
        self.network_evicted = 0
        self.network_evicted_through = 0
        self.late_updates_ignored = 0
        self.observation_errors = 0

    def observe(self, event, value):
        try:
            getattr(self, "on_" + event)(value)
        except Exception:  # noqa: BLE001 -- diagnostics must not break navigation or leak raw errors
            self.observation_errors += 1

    def append_console(self, kind, level, message, location=None):
        message, clipped = text(message, self.secrets())
        source = None
        if location and location.get("url"):
            source = {"url": diagnostic_url(location["url"], self.secrets(), 256)}
            for field in ("line", "column"):
                number = location.get(field + "Number", location.get(field))
                source[field] = number + 1 if type(number) is int and number >= 0 else None
        self.console_sequence += 1
        if len(self.console) == CAPACITY:
            self.console_evicted += 1
        self.console.append(
            {
                "sequence": self.console_sequence,
                "timestamp": now(),
                "kind": kind,
                "level": level,
                "message": message,
                "message_truncated": clipped,
                "source": source,
            }
        )

    def on_console(self, message):
        level = message.type
        if level == "assert":
            level = "error"
        if level not in LEVELS[1:]:
            level = "other"
        # No args/json_value/object expansion: objects can contain credentials
        # or execute application accessors. Only the browser's text is used.
        self.append_console("console", level, message.text, message.location)

    def on_error(self, error):
        # Raw stacks can contain source snippets, URLs and arbitrary application
        # data. Only a bounded message is retained; location is unknown.
        self.append_console("page_error", "error", error.message)

    def touch(self, key, row):
        self.network_sequence += 1
        row["sequence"] = self.network_sequence
        row["updated_at"] = now()
        self.requests[key] = row
        self.requests.move_to_end(key)
        if len(self.requests) > CAPACITY:
            _, removed = self.requests.popitem(last=False)
            self.network_evicted += 1
            self.network_evicted_through = max(self.network_evicted_through, removed["sequence"])

    def on_request(self, request):
        self.request_count += 1
        method = request.method
        resource = request.resource_type
        row = {
            "request_id": f"r{self.request_count}",
            "started_at": now(),
            "url": diagnostic_url(request.url, self.secrets()),
            "method": method
            if method in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "CONNECT", "TRACE"}
            else "OTHER",
            "resource_type": resource
            if resource
            in {
                "document",
                "stylesheet",
                "image",
                "media",
                "font",
                "script",
                "texttrack",
                "xhr",
                "fetch",
                "eventsource",
                "websocket",
                "manifest",
            }
            else "other",
            "state": "pending",
            "status": None,
            "failure": None,
            "timings_ms": dict.fromkeys(("dns", "connect", "tls", "ttfb", "download", "total")),
        }
        self.touch(weakref.ref(request), row)

    def update_request(self, request, state, status=None):
        key = weakref.ref(request)
        row = self.requests.get(key)
        if row is None:
            self.late_updates_ignored += 1
            return
        row["state"] = state
        if status is not None:
            row["status"] = status if type(status) is int and 100 <= status <= 599 else None
        if state == "failed":
            failure = request.failure
            # Never return a raw browser error: it can contain URLs or credentials.
            row["failure"] = (
                failure
                if isinstance(failure, str) and re.fullmatch(r"net::ERR_[A-Z_0-9]{1,80}", failure)
                else "request_failed"
            )
        self.touch(key, row)
        row["timings_ms"] = timings(request)

    def on_response(self, response):
        self.update_request(response.request, "response", response.status)

    def on_finished(self, request):
        self.update_request(request, "finished")

    def on_failed(self, request):
        self.update_request(request, "failed")

    def report(self, stream, options):
        since = options.get("since", 0)
        console = stream == "console"
        rows = list(self.console) if console else list(self.requests.values())
        evicted = self.console_evicted if console else self.network_evicted
        evicted_through = self.console_evicted if console else self.network_evicted_through
        sequence = self.console_sequence if console else self.network_sequence
        selected = [row for row in rows if row["sequence"] > since]
        level = options.get("level", "all")
        if console and level != "all":
            selected = [row for row in selected if row["level"] == level]
        if not console and options.get("failed"):
            selected = [row for row in selected if row["state"] == "failed" or (row["status"] or 0) >= 400]
        # Copy before CLI serialization; reading never clears or mutates capture.
        return {
            "capture": {
                "started_at": self.started_at,
                "capacity": CAPACITY,
                "retained": len(rows),
                "evicted": evicted,
                "evicted_through_sequence": evicted_through,
                "since": since,
                "latest_sequence": sequence,
                "history_gap": since < evicted_through,
                "cursor_ahead": since > sequence,
                "matching": len(selected),
                "observation_errors": self.observation_errors,
                **(
                    {
                        "requests_observed": self.request_count,
                        "late_updates_ignored": self.late_updates_ignored,
                    }
                    if not console
                    else {}
                ),
                "scope": "Main page and its frame events since session open; not a complete browser/network audit",
            },
            "messages" if console else "requests": copy.deepcopy(selected),
        }
