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

# Latest report, shown in the UI (no database: resets when the app restarts)
latest = {"run": None, "results": [], "apis": [], "note": None, "running": False}
_last_manual = 0.0


def start_check(trigger="manual"):
    """Run a check in the background so the UI responds immediately.

    Returns seconds to wait if a manual run was requested too soon after the last one, else 0.
    """
    global _last_manual
    with _cooldown_lock:
        if latest["running"]:
            return 0
        wait = int(_last_manual + config.RUN_COOLDOWN - time.monotonic())
        if wait > 0:
            return wait
        _last_manual = time.monotonic()
        latest["running"] = True
    threading.Thread(target=run_check, args=(trigger,), daemon=True).start()
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


def run_check(trigger="manual"):
    """Check every site and API and email the report. Only one check runs at a time.

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

        if not config.EMAIL_CONFIGURED:
            note = ("error", f"Email not sent: {config.EMAIL_PROBLEM}.")
        else:
            total = len(config.MAIL_TO)
            try:
                failed = mailer.send_report(run, results, apis)
                run["emailed"] = len(failed) < total
                if failed:
                    note = ("error", f"Report emailed to {total - len(failed)} of {total} recipients; "
                                     f"failed for: {', '.join(map(mailer.mask, failed))}.")
                else:
                    note = ("info", f"Report emailed to {total} recipient{'s' if total > 1 else ''}.")
            except Exception as e:  # noqa: BLE001 - show any mail failure in the UI
                log.exception("Email failed")
                note = ("error", f"Email failed: {_mail_error(e)}.")

        latest.update(run=run, results=results, apis=apis, note=note)
        log.info("%s check of %d targets finished in %.1fs. %s", trigger, run["total"], run["seconds"], note[1])
    finally:
        latest["running"] = False
        _lock.release()
