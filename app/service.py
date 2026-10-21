"""Runs a full check: check sites, email the report, keep the latest in memory."""
import logging
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from . import checker, config, mailer

log = logging.getLogger("statrep")
_lock = threading.Lock()

# Latest report, shown in the UI (no database: resets when the app restarts)
latest = {"run": None, "results": [], "note": None, "running": False}


def start_check(trigger="manual"):
    """Run a check in the background so the UI responds immediately."""
    if latest["running"]:
        return
    latest["running"] = True
    threading.Thread(target=run_check, args=(trigger,), daemon=True).start()


def run_check(trigger="manual"):
    """Check every site and email the report. Only one check runs at a time."""
    if not _lock.acquire(blocking=False):
        return
    latest["running"] = True
    try:
        started = datetime.now(ZoneInfo(config.TZ)).strftime("%Y-%m-%d %H:%M")
        results = checker.check_all(config.load_sites())
        run = {
            "started_at": started,
            "trigger": trigger,
            "total": len(results),
            "up": sum(r.status != "DOWN" for r in results),
            "emailed": False,
        }
        for r in results:
            log.info("%-4s %s %s", r.status, r.url, r.error or f"{r.ms}ms")

        if not config.EMAIL_CONFIGURED:
            note = ("error", "Email not sent: SMTP settings are missing in .env.")
        else:
            try:
                mailer.send_report(run, results)
                run["emailed"] = True
                note = ("info", f"Report emailed to {', '.join(config.MAIL_TO)}.")
            except Exception as e:  # noqa: BLE001 - show any mail failure in the UI
                log.exception("Email failed")
                note = ("error", f"Email failed: {e}")

        latest.update(run=run, results=results, note=note)
        log.info("%s check finished. %s", trigger, note[1])
    finally:
        latest["running"] = False
        _lock.release()
