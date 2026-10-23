import smtplib
import ssl

import pytest

from app import checker, config, mailer

RUN = {"started_at": "2026-10-23 06:00", "trigger": "scheduled", "total": 2, "up": 1, "emailed": False}
RESULTS = [checker.Result("https://a.com", "a.com", "UP", 200, 120),
           checker.Result("https://b.com", "b.com", "DOWN", error="Timed out after 20s")]


class FakeSMTP:
    """Records deliveries; behaviour per recipient is set in `plan` ('refuse' or 'drop')."""
    instances, delivered, plan = 0, [], {}

    def __init__(self, *args, **kwargs):
        FakeSMTP.instances += 1

    def starttls(self, context=None):
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname

    def login(self, user, password):
        pass

    def send_message(self, msg):
        to = msg["To"]
        assert msg.get_all("To") == [to]                     # one recipient per copy
        action = FakeSMTP.plan.pop(to, None)
        if action == "refuse":
            raise smtplib.SMTPRecipientsRefused({to: (550, b"no such user")})
        if action == "drop":
            raise smtplib.SMTPServerDisconnected("connection lost")
        FakeSMTP.delivered.append(to)

    def quit(self):
        pass


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    FakeSMTP.instances, FakeSMTP.delivered, FakeSMTP.plan = 0, [], {}
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(config, "MAIL_TO", ["a@x.com", "b@x.com", "c@x.com", "d@x.com", "e@x.com"])


def test_each_recipient_gets_own_copy():
    assert mailer.send_report(RUN, RESULTS, []) == []
    assert FakeSMTP.delivered == config.MAIL_TO and FakeSMTP.instances == 1


def test_refused_recipient_does_not_stop_the_rest():
    FakeSMTP.plan = {"b@x.com": "refuse"}
    assert mailer.send_report(RUN, RESULTS, []) == ["b@x.com"]
    assert FakeSMTP.delivered == ["a@x.com", "c@x.com", "d@x.com", "e@x.com"]


def test_reconnects_once_after_disconnect():
    FakeSMTP.plan = {"c@x.com": "drop"}
    assert mailer.send_report(RUN, RESULTS, []) == []
    assert FakeSMTP.delivered == config.MAIL_TO and FakeSMTP.instances == 2


def test_second_disconnect_marks_remaining_as_failed():
    FakeSMTP.plan = {"b@x.com": "drop"}
    original = FakeSMTP.send_message

    def drop_again_after_reconnect(self, msg):
        if FakeSMTP.instances == 2 and msg["To"] == "d@x.com":
            raise smtplib.SMTPServerDisconnected("again")
        original(self, msg)
    FakeSMTP.send_message = drop_again_after_reconnect
    try:
        assert mailer.send_report(RUN, RESULTS, []) == ["d@x.com", "e@x.com"]
    finally:
        FakeSMTP.send_message = original
    assert FakeSMTP.delivered == ["a@x.com", "b@x.com", "c@x.com"]


def test_message_has_text_and_html_parts_and_logo():
    msg = mailer._build_message(RUN, RESULTS, [])
    types = [part.get_content_type() for part in msg.walk()]
    assert types == ["multipart/related", "multipart/alternative", "text/plain", "text/html", "image/png"]
    text = msg.get_payload(0).get_payload(0).get_payload(decode=True).decode()
    assert "1 of 2 websites are down" in text and "DOWN  b.com  Timed out after 20s" in text
    assert msg["Subject"].startswith("[StatRep] 1 of 2 websites down")


def test_mask():
    assert mailer.mask("john@example.com") == "j***@example.com"
