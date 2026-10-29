import threading
import time

import pytest

from app import checker, config, service


@pytest.fixture(autouse=True)
def no_real_checks(monkeypatch):
    calls = []

    def fake_check(sites, apis):
        calls.append(time.monotonic())
        time.sleep(0.3)
        return [checker.Result("https://a.com", "a.com", "UP", 200, 10)], []
    monkeypatch.setattr(checker, "check_everything", fake_check)
    monkeypatch.setattr(config, "EMAIL_CONFIGURED", False)
    monkeypatch.setattr(config, "EMAIL_PROBLEM", "SMTP settings are missing in .env")
    monkeypatch.setattr(service, "_last_manual", None)
    service.latest.update(running=False)
    return calls


def test_scheduled_run_waits_for_running_check(no_real_checks):
    manual = threading.Thread(target=service.run_check, args=("manual",))
    manual.start()
    time.sleep(0.05)
    service.run_check("scheduled")          # must wait for the manual run, not skip
    manual.join()
    assert len(no_real_checks) == 2
    assert service.latest["run"]["trigger"] == "scheduled"


def test_manual_run_is_dropped_while_another_runs(no_real_checks):
    other = threading.Thread(target=service.run_check, args=("scheduled",))
    other.start()
    time.sleep(0.05)
    service.run_check("manual")
    other.join()
    assert len(no_real_checks) == 1


def test_manual_cooldown(monkeypatch):
    monkeypatch.setattr(service, "run_check", lambda trigger, email=True: service.latest.update(running=False))
    assert service.start_check("manual") == 0
    wait = service.start_check("manual")
    assert 0 < wait <= config.RUN_COOLDOWN


def test_missing_email_settings_are_reported():
    service.run_check("manual")
    assert service.latest["note"] == ("error", "Email not sent: SMTP settings are missing in .env.")
    assert service.latest["run"]["seconds"] >= 0.3


def test_single_check_updates_latest_without_email(monkeypatch):
    site = {"url": "https://a.com", "name": "A"}
    monkeypatch.setattr(config, "load_sites", lambda: [site])
    monkeypatch.setattr(service, "_last_single", {})
    monkeypatch.setattr(checker, "check_site", lambda s: checker.Result(s["url"], s["name"], "DOWN", error="x"))
    sent = []
    monkeypatch.setattr(service, "_email", lambda *a: sent.append(a) or ("info", "ok"))
    service.run_check("manual")
    assert service.find_site("https://a.com") == site and service.find_site("https://other.com") is None
    result, wait = service.check_single(site)
    assert result.status == "DOWN" and wait == 0 and not sent[1:]   # only the full run emailed
    assert service.latest["results"][0] is result                   # shown in the main list
    assert service.check_single(site)[1] > 0                        # per-site cooldown


def test_email_latest_sends_current_results(monkeypatch):
    sent = []
    monkeypatch.setattr(service, "_email", lambda run, results, apis: sent.append((run, results)) or ("info", "ok"))
    monkeypatch.setitem(service.latest, "run", None)
    assert service.email_latest()[0][0] == "error" and not sent               # no results yet
    service.run_check("manual")
    monkeypatch.setattr(service, "_last_manual", None)
    note, wait = service.email_latest()
    assert note == ("info", "ok") and wait == 0
    run, results = sent[-1]
    assert run["trigger"] == "resend" and results == service.latest["results"]
    assert service.email_latest()[1] > 0                            # shares the manual cooldown


def test_check_without_email_but_scheduled_always_emails(monkeypatch):
    sent = []
    monkeypatch.setattr(service, "_email", lambda *a: sent.append(a) or ("info", "ok"))
    service.run_check("manual", email=False)
    assert not sent and service.latest["note"] == ("info", "Not emailed.")
    service.run_check("scheduled", email=False)
    assert len(sent) == 1


def test_cooldown_right_after_boot(monkeypatch):
    # time.monotonic() is close to 0 just after a reboot (e.g. a fresh CI runner): no wait yet
    monkeypatch.setattr(service.time, "monotonic", lambda: 5.0)
    monkeypatch.setattr(service, "run_check", lambda trigger, email=True: service.latest.update(running=False))
    assert service.start_check("manual") == 0
    assert service.start_check("manual") == config.RUN_COOLDOWN
