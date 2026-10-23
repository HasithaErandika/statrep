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
    monkeypatch.setattr(service, "_last_manual", 0.0)
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
    monkeypatch.setattr(service, "run_check", lambda trigger: service.latest.update(running=False))
    assert service.start_check("manual") == 0
    wait = service.start_check("manual")
    assert 0 < wait <= config.RUN_COOLDOWN


def test_missing_email_settings_are_reported():
    service.run_check("manual")
    assert service.latest["note"] == ("error", "Email not sent: SMTP settings are missing in .env.")
    assert service.latest["run"]["seconds"] >= 0.3
