import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import requests

from app import checker, config


@pytest.mark.parametrize("ip, public", [
    ("8.8.8.8", True), ("2606:4700:4700::1111", True),
    ("127.0.0.1", False), ("10.1.2.3", False), ("192.168.1.1", False), ("169.254.169.254", False),
    ("::1", False), ("::ffff:127.0.0.1", False), ("0.0.0.0", False), ("224.0.0.1", False),
])
def test_is_public(ip, public):
    assert checker._is_public(ip) is public


@pytest.mark.parametrize("url", ["http://127.0.0.1:9/", "http://[::1]:9/", "http://169.254.169.254/latest/"])
def test_public_only_session_blocks_internal_addresses(url):
    with checker.new_session(public_only=True) as session, pytest.raises(checker.BlockedAddress):
        session.get(url, timeout=2)


def test_quick_check_reports_blocked_address():
    result = checker.check_site("http://127.0.0.1:9/", public_only=True)
    assert result.status == "DOWN" and result.error == checker.BLOCKED_ERROR


def test_json_path():
    data = {"a": {"items": [{"id": 7}]}, "ok": True}
    assert checker._json_path(data, "a.items.0.id") == 7
    assert checker._json_path(data, "a.items.-1.id") == 7
    assert checker._json_path(data, "ok") is True
    assert checker._json_path(data, "a.items.5") is checker._MISSING
    assert checker._json_path(data, "nope") is checker._MISSING


@pytest.mark.parametrize("got, want, same", [
    ("ok", "ok", True), (200, "200", True), (True, "true", True), (None, "null", True),
    ("ok", "OK", False), ({"a": 1}, {"a": 1}, True), ([1], "[1]", False),
])
def test_same(got, want, same):
    assert checker._same(got, want) is same


def _response(status=200, body=b'{"status": "ok"}', ctype="application/json"):
    raw = requests.Response()
    raw.status_code, raw.reason, raw.headers["Content-Type"] = status, "X", ctype
    return checker.Page(raw, body)


def _api(**kw):
    return {"expect_status": [], "expect_json": {}, "expect_text": None, **kw}


def test_api_expectations_pass_and_fail():
    checks = checker._api_expectations(_api(expect_status=[200], expect_json={"status": "ok"}, expect_text="ok"),
                                       _response())
    assert all(ok for _, ok, _ in checks) and len(checks) == 3

    checks = checker._api_expectations(_api(expect_status=[200], expect_json={"status": "up", "x": 1}),
                                       _response(503))
    failures = [detail for _, ok, detail in checks if not ok]
    assert failures == ["Expected HTTP 200, got 503", "status: expected 'up', got 'ok'", "Field 'x' is missing"]


def test_api_expectations_non_json_and_default_status():
    checks = checker._api_expectations(_api(expect_json={"a": 1}), _response(404, b"<html>", "text/html"))
    assert [d for _, ok, d in checks if not ok] == ["HTTP 404 X", "Response is not valid JSON"]


def test_page_decodes_once_with_sensible_charset():
    page = _response(body="Café".encode(), ctype="text/html")            # no charset -> utf-8
    assert page.text == "Café" and page.text is page.text                 # cached
    page = _response(body="Café".encode("latin-1"), ctype="text/html; charset=latin-1")
    assert page.text == "Café"
    page = _response(body=b"x", ctype="text/html; charset=bogus")         # unknown charset
    assert page.text == "x"


def test_parse_date_marks_date_only_values():
    assert isinstance(checker._parse_date("2025-01-31"), checker.DateOnly)
    full = checker._parse_date("2025-01-31T00:00:00Z")
    assert full == datetime(2025, 1, 31, tzinfo=timezone.utc) and not isinstance(full, checker.DateOnly)
    assert checker._parse_date("Tue, 01 Jul 2025 10:00:00 GMT").hour == 10
    assert checker._parse_date("not a date") is None


def test_rdap_cache_is_bounded_and_failures_retry_sooner(monkeypatch):
    monkeypatch.setattr(checker, "RDAP_CACHE_MAX", 3)
    monkeypatch.setattr(checker, "_rdap_cache", checker.OrderedDict())

    def fake_get(url, **kw):
        fake_get.calls += 1
        if "fail" in url:
            raise requests.ConnectionError()
        return SimpleNamespace(ok=True, status_code=200, json=lambda: {"events": []})
    fake_get.calls = 0
    monkeypatch.setattr(checker.requests, "get", fake_get)

    for d in ("a.com", "b.com", "c.com", "d.com", "fail.com"):
        checker._rdap_lookup(d)
    assert list(checker._rdap_cache) == ["c.com", "d.com", "fail.com"]
    assert checker._rdap_cache["fail.com"][2] == checker.RDAP_RETRY_SECONDS
    assert checker._rdap_cache["d.com"][2] == checker.RDAP_CACHE_SECONDS
    assert checker._rdap_cached("d.com") == {} and checker._rdap_cached("a.com") is None


def test_check_everything_two_phases_at_scale(monkeypatch):
    """40 sites + 5 APIs: timed requests run at most TIMING_CONCURRENCY at once and before any
    details work; details then run up to CHECK_CONCURRENCY at once."""
    import threading
    lock, active, peak, log = threading.Lock(), {"measure": 0, "finish": 0}, {"measure": 0, "finish": 0}, []

    def tracked(phase, seconds):
        def run(self):
            with lock:
                active[phase] += 1
                peak[phase] = max(peak[phase], active[phase])
                log.append(phase)
            time.sleep(seconds)
            with lock:
                active[phase] -= 1
            if phase == "finish":
                return checker.Result(self.url, self.url, "UP", kind="site" if isinstance(self, checker.SiteCheck) else "api")
        return run

    for cls in (checker.SiteCheck, checker.ApiCheck):
        monkeypatch.setattr(cls, "measure", tracked("measure", 0.05))
        monkeypatch.setattr(cls, "finish", tracked("finish", 0.3))
    monkeypatch.setattr(checker.ApiCheck, "url", "api", raising=False)
    monkeypatch.setattr(config, "TIMING_CONCURRENCY", 10)
    monkeypatch.setattr(config, "CONCURRENCY", 40)

    sites = [{"url": f"https://s{i}.example"} for i in range(40)]
    apis = [{"name": f"a{i}", "url": f"https://a{i}.example", "display_url": ""} for i in range(5)]
    start = time.monotonic()
    results, api_results = checker.check_everything(sites, apis)
    elapsed = time.monotonic() - start

    assert [r.url for r in results] == [s["url"] for s in sites]          # config order kept
    assert [r.kind for r in api_results] == ["api"] * 5
    assert peak == {"measure": 10, "finish": 40}
    assert log.index("finish") == 45                                       # all timing done first
    assert elapsed < 1.5, f"took {elapsed:.2f}s; checks are not running concurrently"


def test_config_error_entries_are_reported_not_checked():
    r = checker.check_site({"url": "", "name": "bad", "config_error": "Config error: x"})
    assert (r.status, r.error) == ("DOWN", "Config error: x")
    r = checker.check_api({"name": "api", "display_url": "", "config_error": "Config error: y"})
    assert (r.status, r.error, r.kind) == ("DOWN", "Config error: y", "api")


@pytest.mark.parametrize("code, error, kind, label", [
    (403, "HTTP 403 Forbidden", "site", "Forbidden"),
    (401, "HTTP 401 Unauthorized", "site", "Login required"),
    (404, "HTTP 404 Not Found", "site", "Not found"),
    (418, "HTTP 418 I'm a teapot", "site", "Client error"),
    (502, "HTTP 502 Bad Gateway", "site", "Bad gateway"),
    (599, "HTTP 599", "site", "Server error"),
    (200, "Page is empty", "site", "Empty page"),
    (None, "Timed out after 20s", "site", "Timeout"),
    (None, "Could not connect within 10s", "site", "Unreachable"),
    (None, "Could not connect (DNS or server down)", "site", "No connection"),
    (None, "SSL certificate error", "site", "SSL error"),
    (None, "Config error: url must be an http(s) address", "site", "Config error"),
    (200, "status: expected \"ok\", got \"fail\"", "api", "Unexpected response"),
])
def test_down_reason(code, error, kind, label):
    r = checker.Result("https://a.com", "a", "DOWN", code, error=error, kind=kind)
    assert r.down_reason[0] == label
    assert checker.Result("https://a.com", "a", "UP", 200).down_reason is None


def test_api_expected_error_code_is_not_reported_as_http_error():
    # An API expected to return 404 that fails on its JSON check: the 404 itself is fine
    r = checker.Result("https://a.com", "a", "DOWN", 404, error="x", kind="api", checks=[("HTTP status is 404", True, None)])
    assert r.down_reason[0] == "Unexpected response"
