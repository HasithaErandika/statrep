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
    results, api_results, ip_results = checker.check_everything(sites, apis, [])
    elapsed = time.monotonic() - start

    assert [r.url for r in results] == [s["url"] for s in sites]          # config order kept
    assert [r.kind for r in api_results] == ["api"] * 5 and ip_results == []
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


LINUX_OK = """PING 10.0.0.1 (10.0.0.1) 56(84) bytes of data.
64 bytes from 10.0.0.1: icmp_seq=1 ttl=64 time=10.5 ms
64 bytes from 10.0.0.1: icmp_seq=2 ttl=64 time=20.5 ms
64 bytes from 10.0.0.1: icmp_seq=3 ttl=64 time=30.5 ms

--- 10.0.0.1 ping statistics ---
3 packets transmitted, 3 received, 0% packet loss, time 2003ms
rtt min/avg/max/mdev = 10.500/20.500/30.500/8.165 ms
"""
LINUX_LOSS = """64 bytes from 10.0.0.1: icmp_seq=1 ttl=64 time=4.00 ms
3 packets transmitted, 1 received, 66.6667% packet loss, time 2003ms
rtt min/avg/max/mdev = 4.000/4.000/4.000/0.000 ms
"""
LINUX_NONE = "3 packets transmitted, 0 received, 100% packet loss, time 2050ms\n"
LINUX_LOCAL = """64 bytes from 127.0.0.1: icmp_seq=1 ttl=64 time=0.025 ms
64 bytes from 127.0.0.1: icmp_seq=2 ttl=64 time=0.040 ms
64 bytes from 127.0.0.1: icmp_seq=3 ttl=64 time=0.052 ms
rtt min/avg/max/mdev = 0.025/0.039/0.052/0.011 ms
"""
WINDOWS_OK = """Pinging 10.0.0.1 with 32 bytes of data:
Reply from 10.0.0.1: bytes=32 time<1ms TTL=64
Reply from 10.0.0.1: bytes=32 time=2ms TTL=64
Reply from 10.0.0.1: bytes=32 time=3ms TTL=64

Ping statistics for 10.0.0.1:
    Packets: Sent = 3, Received = 3, Lost = 0 (0% loss),
Approximate round trip times in milli-seconds:
    Minimum = 0ms, Maximum = 3ms, Average = 2ms
"""
WINDOWS_UNREACHABLE = """Reply from 10.0.0.9: Destination host unreachable.
Reply from 10.0.0.9: Destination host unreachable.
Reply from 10.0.0.9: Destination host unreachable.
    Packets: Sent = 3, Received = 3, Lost = 0 (0% loss),
"""


def fake_ping(monkeypatch, stdout, returncode=0, stderr=""):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)
    monkeypatch.setattr(checker.subprocess, "run", run)
    return calls


@pytest.mark.parametrize("stdout, returncode, status, ms, received, low, high", [
    (LINUX_OK, 0, "UP", 20, 3, 10.5, 30.5),
    (LINUX_LOSS, 0, "SLOW", 4, 1, 4.0, 4.0),            # packet loss counts as degraded
    (LINUX_LOCAL, 0, "UP", 1, 3, 0.0, 0.1),             # below 1 ms must not read as "no response"
    (WINDOWS_OK, 0, "UP", 2, 3, 1.0, 3.0),
])
def test_ping_replies(monkeypatch, stdout, returncode, status, ms, received, low, high):
    fake_ping(monkeypatch, stdout, returncode)
    r = checker._ping({"ip": "10.0.0.1", "name": "Box"})
    assert (r.status, r.ms, r.error, r.kind) == (status, ms, None, "ip")
    assert (r.ping["received"], r.ping["min"], r.ping["max"]) == (received, low, high)
    assert r.down_reason is None


@pytest.mark.parametrize("stdout, returncode", [
    (LINUX_NONE, 1),
    (WINDOWS_UNREACHABLE, 0),                           # Windows exits with 0 here
])
def test_ping_without_replies_is_down(monkeypatch, stdout, returncode):
    fake_ping(monkeypatch, stdout, returncode)
    r = checker._ping({"ip": "10.0.0.9", "name": "Box"})
    assert (r.status, r.ms, r.ping["loss"]) == ("DOWN", None, 100)
    assert r.down_reason[0] == "No reply"


def test_ping_slow_limit(monkeypatch):
    fake_ping(monkeypatch, LINUX_OK)
    monkeypatch.setattr(config, "PING_SLOW_MS", 15)
    assert checker._ping({"ip": "10.0.0.1", "name": "Box"}).status == "SLOW"


@pytest.mark.parametrize("problem", [FileNotFoundError("ping"), checker.subprocess.TimeoutExpired("ping", 1)])
def test_ping_that_cannot_run_is_not_reported_as_no_reply(monkeypatch, problem):
    def run(cmd, **kwargs):
        raise problem
    monkeypatch.setattr(checker.subprocess, "run", run)
    r = checker._ping({"ip": "10.0.0.1", "name": "Box"})
    assert r.status == "DOWN" and r.down_reason[0] == "Ping failed" and r.ping == {}


def test_ping_refused_by_the_system(monkeypatch):
    fake_ping(monkeypatch, "", 2, "ping: socktype: SOCK_RAW: Operation not permitted")
    assert checker._ping({"ip": "10.0.0.1", "name": "Box"}).down_reason[0] == "Ping failed"


@pytest.mark.parametrize("os_name, platform, expected", [
    ("posix", "linux", ["ping", "-n", "-c", "3", "-W", "2", "10.0.0.1"]),
    ("posix", "darwin", ["ping", "-n", "-c", "3", "-W", "2000", "10.0.0.1"]),
    ("nt", "win32", ["ping", "-n", "3", "-w", "2000", "10.0.0.1"]),
])
def test_ping_command_per_system(monkeypatch, os_name, platform, expected):
    monkeypatch.setattr(checker.os, "name", os_name)
    monkeypatch.setattr(checker.sys, "platform", platform)
    assert checker._ping_command("10.0.0.1") == expected


def test_ping_check_retries_only_when_down(monkeypatch):
    monkeypatch.setattr(config, "RETRIES", 2)
    monkeypatch.setattr(checker.time, "sleep", lambda s: None)
    calls = fake_ping(monkeypatch, LINUX_NONE, 1)
    check = checker.PingCheck({"ip": "10.0.0.9", "name": "Box"})
    check.measure()
    assert len(calls) == 3 and check.finish().status == "DOWN"
    calls = fake_ping(monkeypatch, LINUX_OK)
    check.measure()
    assert len(calls) == 1 and check.finish().status == "UP"


def test_ip_config_error_is_reported_not_pinged(monkeypatch):
    calls = fake_ping(monkeypatch, LINUX_OK)
    check = checker.PingCheck({"ip": "nope", "name": "bad", "config_error": "Config error: z"})
    check.measure()
    r = check.finish()
    assert (r.status, r.kind, r.down_reason[0]) == ("DOWN", "ip", "Config error") and not calls


def test_check_everything_splits_sites_apis_and_ips(monkeypatch):
    fake_ping(monkeypatch, LINUX_OK)
    sites, apis, ips = checker.check_everything(
        [{"url": "", "name": "s", "config_error": "Config error: x"}],
        [{"name": "a", "display_url": "", "config_error": "Config error: y"}],
        [{"ip": "10.0.0.1", "name": "Box"}])
    assert [[r.kind for r in group] for group in (sites, apis, ips)] == [["site"], ["api"], ["ip"]]
