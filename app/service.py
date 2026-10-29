"""Runs a full check: check sites and APIs, email the report, keep the latest in memory."""
import logging
import smtplib
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import checker, config, mailer

log = logging.getLogger("statrep")
_lock = threading.Lock()
_cooldown_lock = threading.Lock()
SCHEDULED_WAIT = 15 * 60  # a scheduled run waits this long for a running check to finish
SINGLE_COOLDOWN = 20      # seconds before the same website can be checked on its own again

# Latest report, shown in the UI (no database: resets when the app restarts)
latest = {"run": None, "results": [], "apis": [], "note": None, "running": False}
_last_manual = None  # time of the last manual run/email; None = never
_last_single = {}  # site url -> time of its last single-site check (per-site cooldown)
_single_slots = threading.BoundedSemaphore(3)  # single-site checks running at the same time


def _cooldown_left(last, cooldown):
    """Whole seconds until `cooldown` has passed since `last` (a time.monotonic() value; None = never).

    Not "last = 0": monotonic time starts near 0 at boot, so that would block the first runs
    after a (CI or server) reboot.
    """
    return 0 if last is None else int(last + cooldown - time.monotonic())


def start_check(trigger="manual", email=True):
    """Run a check in the background so the UI responds immediately.

    Returns seconds to wait if a manual run was requested too soon after the last one, else 0.
    """
    global _last_manual
    with _cooldown_lock:
        if latest["running"]:
            return 0
        wait = _cooldown_left(_last_manual, config.RUN_COOLDOWN)
        if wait > 0:
            return wait
        _last_manual = time.monotonic()
        latest["running"] = True
    threading.Thread(target=run_check, args=(trigger, email), daemon=True).start()
    return 0


def _mail_error(e):
    """Short reason for the public page; full details go to the log."""
    if isinstance(e, smtplib.SMTPAuthenticationError):
        return "SMTP login was rejected (check SMTP_USER / SMTP_PASS)"
    if isinstance(e, smtplib.SMTPSenderRefused):
        return "the sender address was refused (check MAIL_FROM)"
    if isinstance(e, (OSError, smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError)):
        return "could not connect to the SMTP server"
    return "the SMTP server returned an error (see logs)"


def _email(run, results, apis):
    """Email the report and return the note shown on the page: (level, message)."""
    if not config.EMAIL_CONFIGURED:
        return "error", f"Email not sent: {config.EMAIL_PROBLEM}."
    total = len(config.MAIL_TO)
    try:
        failed = mailer.send_report(run, results, apis)
    except Exception as e:  # noqa: BLE001 - show any mail failure in the UI
        log.exception("Email failed")
        return "error", f"Email failed: {_mail_error(e)}."
    run["emailed"] = len(failed) < total
    if failed:
        return "error", (f"Report emailed to {total - len(failed)} of {total} recipients; "
                         f"failed for: {', '.join(map(mailer.mask, failed))}.")
    return "info", f"Report emailed to {total} recipient{'s' if total > 1 else ''}."


def find_site(url):
    """The sites.yml entry with this url, or None: single checks only accept configured sites."""
    return next((s for s in config.load_sites() if s["url"] == url and not s.get("config_error")), None)


def check_single(site):
    """Check one website from sites.yml again, without emailing a report.

    Runs in the request (one site takes seconds). Returns (result, wait): wait is the seconds
    left if this site was checked too recently, and then result is None.
    """
    url = site["url"]
    with _cooldown_lock:
        wait = _cooldown_left(_last_single.get(url), SINGLE_COOLDOWN)
        if wait > 0:
            return None, wait
        _last_single[url] = time.monotonic()
    with _single_slots:
        result = checker.check_site(site)
    log.info("%-4s %s %s (single check)", result.status, url, result.error or f"{result.ms}ms")
    # Show the fresh result in the main list too, if the last full run included this site
    latest["results"] = [result if r.url == url else r for r in latest["results"]]
    return result, 0


def email_latest():
    """Email the results on the page now, including websites re-checked one by one.

    Returns (note, wait): wait is the seconds left before another manual email/run is allowed.
    """
    global _last_manual
    with _cooldown_lock:
        if latest["running"] or not latest["run"]:
            return ("error", "There are no finished results to email yet."), 0
        wait = _cooldown_left(_last_manual, config.RUN_COOLDOWN)
        if wait > 0:
            return None, wait
        _last_manual = time.monotonic()
    results, apis = latest["results"], latest["apis"]
    run = {**latest["run"], "trigger": "resend", "emailed": False,
           "started_at": datetime.now(ZoneInfo(config.TZ)).strftime("%Y-%m-%d %H:%M"),
           "up": sum(r.status != "DOWN" for r in results + apis)}
    note = _email(run, results, apis)
    latest["run"]["emailed"] = latest["run"]["emailed"] or run["emailed"]
    latest["note"] = note
    return note, 0


def run_check(trigger="manual", email=True):
    """Check every site and API and email the report (always for scheduled runs; manual runs
    can skip the email). Only one check runs at a time.

    A manual run is dropped if a check is already running; a scheduled run waits for it to
    finish, so the scheduled report is never silently skipped.
    """
    if trigger == "scheduled":
        acquired = _lock.acquire(timeout=SCHEDULED_WAIT)
    else:
        acquired = _lock.acquire(blocking=False)
    if not acquired:
        log.warning("%s check skipped: another check is still running", trigger)
        return
    latest["running"] = True
    try:
        started = datetime.now(ZoneInfo(config.TZ)).strftime("%Y-%m-%d %H:%M")
        t0 = time.monotonic()
        results, apis = checker.check_everything(config.load_sites(), config.load_apis())
        run = {
            "started_at": started,
            "trigger": trigger,
            "total": len(results) + len(apis),
            "up": sum(r.status != "DOWN" for r in results + apis),
            "emailed": False,
            "seconds": round(time.monotonic() - t0, 1),
        }
        for r in results + apis:
            log.info("%-4s %s %s", r.status, r.name if r.kind == "api" else r.url, r.error or f"{r.ms}ms")

        note = _email(run, results, apis) if email or trigger == "scheduled" else ("info", "Not emailed.")
        latest.update(run=run, results=results, apis=apis, note=note)
        log.info("%s check of %d targets finished in %.1fs. %s", trigger, run["total"], run["seconds"], note[1])
    finally:
        latest["running"] = False
        _lock.release()
