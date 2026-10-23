import base64
from datetime import datetime, timezone

import pytest

from app import config, filters, main
from app.checker import DateOnly


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "_quick_hits", main.defaultdict(main.deque))
    return main.create_app().test_client()


def test_security_headers(client):
    r = client.get("/")
    assert r.status_code == 200
    csp = r.headers["Content-Security-Policy"]
    assert "script-src 'self';" in csp and "frame-ancestors 'none'" in csp
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "same-origin"   # not no-referrer: that sends Origin: null
    assert "<script>" not in r.get_data(as_text=True)      # no inline scripts


@pytest.mark.parametrize("origin, status", [
    ("http://localhost", 302),               # same origin: allowed
    (None, 302),                             # no Origin (curl, old browsers): allowed, cooldown applies
    ("https://evil.example", 403),
    ("null", 403),
])
def test_post_origin_check(client, monkeypatch, origin, status):
    monkeypatch.setattr(main.service, "start_check", lambda trigger: 0)
    headers = {"Origin": origin} if origin else {}
    assert client.post("/run", headers=headers).status_code == status


def test_quick_check_blocks_internal_and_rate_limits(client):
    r = client.post("/api/check", json={"urls": ["http://127.0.0.1:9/"]})
    assert r.status_code == 200 and "Private or internal" in r.get_json()["html"]
    client.post("/api/check", json={"urls": []})
    client.post("/api/check", json={"urls": []})
    assert client.post("/api/check", json={"urls": ["a.com"]}).status_code == 429


def test_quick_check_validates_input(client):
    assert client.post("/api/check", json={"urls": ["ftp://x"]}).status_code == 400
    assert client.post("/api/check", json={"urls": [f"s{i}.com" for i in range(11)]}).status_code == 400


def test_optional_basic_auth(client, monkeypatch):
    monkeypatch.setattr(config, "AUTH_USER", "admin")
    monkeypatch.setattr(config, "AUTH_PASSWORD", "s3cret")
    assert client.get("/").status_code == 401
    assert client.get("/api/status").status_code == 401
    assert client.get("/health").status_code == 200        # Docker health check stays open
    good = base64.b64encode(b"admin:s3cret").decode()
    bad = base64.b64encode(b"admin:wrong").decode()
    assert client.get("/", headers={"Authorization": f"Basic {good}"}).status_code == 200
    assert client.get("/", headers={"Authorization": f"Basic {bad}"}).status_code == 401


def test_scheduler_jobs_have_grace_time():
    main.create_app()
    jobs = main.scheduler.get_jobs()
    assert {j.id for j in jobs} >= {"check-06:00", "check-20:00"}
    assert all(j.misfire_grace_time == 300 and j.coalesce for j in jobs)


def test_filters():
    assert filters.humanlist(["06:00", "07:45", "16:30"]) == "06:00, 07:45 and 16:30"
    assert filters.humanlist(["06:00"]) == "06:00"
    assert filters.fmtdate(DateOnly(2025, 1, 31, tzinfo=timezone.utc)) == "31 Jan 2025"
    # A real timestamp at UTC midnight keeps its (local) time
    assert filters.fmtdate(datetime(2025, 1, 31, tzinfo=timezone.utc)) == "31 Jan 2025, 05:30"
