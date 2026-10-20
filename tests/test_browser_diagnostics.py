"""Diagnostics contracts without Playwright, a real browser, or application data."""

from __future__ import annotations

import gc
import json
import weakref
from types import SimpleNamespace
from typing import ClassVar

import pytest

from devkit.browser.client import request, validate_options
from devkit.browser.diagnostics import CAPACITY, Diagnostics, diagnostic_url, span, text
from devkit.browser.policy import MAX_MESSAGE, clean
from devkit.cli import build_parser, main
from devkit.project import DevkitError


class SyntheticRequest:
    method = "GET"
    resource_type = "fetch"
    url = "http://localhost:5000/api/products?token=not-a-real-secret#private"
    failure = None
    timing: ClassVar[dict] = {
        "domainLookupStart": 0,
        "domainLookupEnd": 2,
        "connectStart": 2,
        "secureConnectionStart": -1,
        "connectEnd": 3,
        "requestStart": 3,
        "responseStart": 13,
        "responseEnd": -1,
    }

    @property
    def headers(self):
        pytest.fail("Diagnostics must not inspect headers")

    @property
    def post_data(self):
        pytest.fail("Diagnostics must not inspect bodies")


def console(message, level="log"):
    return SimpleNamespace(
        type=level,
        text=message,
        location={
            "url": "http://localhost/app.js?credential=hidden#fragment",
            "lineNumber": 9,
            "columnNumber": 2,
        },
    )


@pytest.mark.parametrize("action", ["status", "console", "network"])
def test_diagnostic_help_and_missing_session_are_side_effect_free(tmp_path, capsys, action):
    assert main(["help", "browser", action, "--root", str(tmp_path), "--json"]) == 0
    row = json.loads(capsys.readouterr().out)["data"][0]
    assert f"browser {action}" in row["command"]
    assert "--wait-for" not in row["usage"]
    assert main(["browser", action, "--session", "missing", "--root", str(tmp_path), "--json"]) == 2
    assert "Unknown browser session" in capsys.readouterr().out
    assert not list(tmp_path.iterdir())


def test_diagnostic_cli_filters_and_rpc_validation():
    parser, _ = build_parser()
    args = parser.parse_args(["browser", "console", "--session", "erp", "--since", "10", "--level", "error"])
    assert args.since == 10 and args.level == "error"
    assert parser.parse_args(["browser", "network", "--session", "erp", "--failed"]).failed
    for invalid in (
        {"since": -1},
        {"since": True},
        {"since": 2**53},
        {"level": "invalid"},
        {"failed": "yes"},
    ):
        with pytest.raises(DevkitError):
            validate_options({"timeout": 3, **invalid})
    with pytest.raises(DevkitError):
        parser.parse_args(["browser", "console", "--session", "erp", "--since", "-1"])


def test_console_redaction_locations_filter_and_non_destructive_reads():
    token = "eyJhbGciOiJIUzI1NiJ9.eyJleHAiOjQwMDAwMDAwMDB9.synthetic_signature"
    capture = Diagnostics(lambda: [token])
    capture.observe(
        "console",
        console(f"hello {token} password=secret-value https://user:pass@localhost/path?q=secret#frag"),
    )
    capture.observe("console", console("Problem found", "warning"))
    capture.observe("error", SimpleNamespace(message="Uncaught failure", stack="RAW STACK SECRET"))
    report = capture.report("console", {})
    serialized = json.dumps(report)
    assert all(
        secret not in serialized
        for secret in (token, "secret-value", "user:pass", "q=secret", "#frag", "RAW STACK")
    )
    assert report["messages"][0]["source"] == {"url": "http://localhost/app.js", "line": 10, "column": 3}
    assert report == capture.report("console", {})
    assert len(capture.report("console", {"since": 1, "level": "warning"})["messages"]) == 1
    errors = capture.report("console", {"level": "error"})["messages"]
    assert errors[0]["kind"] == "page_error" and errors[0]["source"] is None
    assert not capture.report("console", {"since": 3})["messages"]
    assert capture.report("console", {"since": 99})["capture"]["cursor_ahead"]
    report["messages"][0]["message"] = "modified copy"
    assert capture.report("console", {})["messages"][0]["message"] != "modified copy"


@pytest.mark.parametrize(
    "value,secret",
    [
        ('{"password":"secret-one"}', "secret-one"),
        ("password secret-two", "secret-two"),
        ("Authorization: Bearer secret-three", "secret-three"),
        ("Cookie: session=secret-four; csrf=secret-five", "secret-five"),
        ('api_key="secret-six"', "secret-six"),
        ("Basic c2VjcmV0", "c2VjcmV0"),
        ("https://localhost/route?code=secret-seven#part", "secret-seven"),
        ("data:text/plain,secret-eight", "secret-eight"),
        ("file:///Users/user/secret-nine", "secret-nine"),
    ],
)
def test_sensitive_console_patterns(value, secret):
    assert secret not in text(value, [])[0]


def test_bounded_console_and_gap_reporting_and_unicode_transport():
    capture = Diagnostics(list)
    for _ in range(CAPACITY + 20):
        message = console("😀" * 20000)
        message.location["url"] = "http://localhost/" + "😀" * 512
        capture.observe("console", message)
    report = capture.report("console", {})
    assert len(report["messages"]) == CAPACITY
    assert report["capture"]["evicted"] == 20
    assert report["capture"]["history_gap"]
    assert not capture.report("console", {"since": 20})["capture"]["history_gap"]
    assert report["messages"][0]["message_truncated"]
    assert len(json.dumps(clean(report), ensure_ascii=True).encode()) < MAX_MESSAGE
    assert "\x1b" not in text("hello\x1b[31m", [])[0]
    assert diagnostic_url("javascript:secret", []) == "[non-HTTP URL omitted]"


def test_legacy_console_locations_and_assertions():
    capture = Diagnostics(list)
    message = console("Assertion failed", "assert")
    message.location = {"url": "http://localhost/script.js", "line": 0, "column": 4}
    capture.observe("console", message)
    row = capture.report("console", {"level": "error"})["messages"][0]
    assert row["source"]["line"] == 1 and row["source"]["column"] == 5


def test_oversized_unknown_token_does_not_expose_truncated_prefix():
    value = "eyJhbGciOiJIUzI1NiJ9." + "a" * 20000 + ".synthetic_signature"
    rendered, clipped = text(value, [])
    assert clipped and "eyJ" not in rendered and "aaaaa" not in rendered


def test_network_transitions_filters_timings_and_since_include_updates():
    capture = Diagnostics(list)
    item = SyntheticRequest()
    capture.observe("request", item)
    pending = capture.report("network", {})["requests"][0]
    assert pending["state"] == "pending" and pending["status"] is None
    capture.observe("response", SimpleNamespace(request=item, status=503))
    response = capture.report("network", {"since": pending["sequence"]})["requests"][0]
    assert response["request_id"] == pending["request_id"]
    assert response["state"] == "response" and response["status"] == 503
    assert response["timings_ms"] == {
        "dns": 2,
        "connect": 1,
        "tls": None,
        "ttfb": 10,
        "download": None,
        "total": None,
    }
    item.timing = {**item.timing, "responseEnd": 20}
    capture.observe("finished", item)
    finished = capture.report("network", {"failed": True})["requests"][0]
    assert finished["state"] == "finished" and finished["timings_ms"]["total"] == 20
    assert finished["url"] == "http://localhost:5000/api/products"
    assert "token" not in json.dumps(capture.report("network", {}))
    assert not capture.report("network", {"since": finished["sequence"]})["requests"]


def test_network_failure_and_eviction_do_not_retain_payload_objects():
    capture = Diagnostics(list)
    item = SyntheticRequest()
    capture.observe("request", item)
    item.failure = "net::ERR_CONNECTION_REFUSED"
    capture.observe("failed", item)
    assert capture.report("network", {"failed": True})["requests"][0]["failure"] == item.failure
    item.failure = "arbitrary secret request URL"
    capture.observe("failed", item)
    assert capture.report("network", {})["requests"][0]["failure"] == "request_failed"
    for _ in range(CAPACITY + 1):
        capture.observe("request", SyntheticRequest())
    capture.observe("finished", item)
    report = capture.report("network", {})
    assert report["capture"]["evicted"] == 2
    assert report["capture"]["history_gap"]
    assert report["capture"]["late_updates_ignored"] == 1
    ref = weakref.ref(item)
    del item
    gc.collect()
    assert ref() is None
    assert len(report["requests"]) == CAPACITY
    assert all(row["timings_ms"]["total"] is None for row in report["requests"])
    retained = SyntheticRequest()
    capture.observe("request", retained)
    retained_ref = weakref.ref(retained)
    del retained
    gc.collect()
    assert retained_ref() is None
    assert len(capture.report("network", {})["requests"]) == CAPACITY


@pytest.mark.parametrize("start,end", [(-1, 4), (10, 4), (None, 4), (0, float("nan")), (True, 4)])
def test_unavailable_timing_is_never_invented(start, end):
    assert span({"start": start, "end": end}, "start", "end") is None


def test_capture_callback_error_does_not_escape_or_leak():
    capture = Diagnostics(list)
    capture.observe("console", object())
    assert capture.report("console", {})["capture"]["observation_errors"] == 1


@pytest.mark.parametrize(
    "exception,expected",
    [
        (ConnectionRefusedError(), "No browser worker is listening"),
        (TimeoutError(), "did not respond before the deadline"),
        (ConnectionResetError(), "connection failed"),
    ],
)
def test_session_transport_errors_are_distinguished(monkeypatch, exception, expected):
    class Connection:
        def request(self, *_args, **_kwargs):
            raise exception

        def close(self):
            pass

    monkeypatch.setattr("devkit.browser.client.http.client.HTTPConnection", lambda *_a, **_k: Connection())
    with pytest.raises(DevkitError, match=expected):
        request(
            {"version": 1, "port": 12345, "instance": "a" * 32, "capability": "b" * 64},
            "status",
            {"timeout": 3},
        )
