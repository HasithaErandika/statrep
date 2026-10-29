"""Builds and sends the HTML status email."""
import base64
import logging
import smtplib
import ssl
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import config, filters

APP_DIR = Path(__file__).parent
LOGO = APP_DIR / "static" / "logo-email.png"
LOGO_CID = "statrep-logo"
LOGO_BYTES = LOGO.read_bytes()
LOGO_DATA_URI = "data:image/png;base64," + base64.b64encode(LOGO_BYTES).decode()
log = logging.getLogger("statrep")

env = Environment(
    loader=FileSystemLoader(APP_DIR / "templates"),
    autoescape=select_autoescape(),
)
filters.register(env)


def render_report(run, results, apis, logo_src):
    return env.get_template("email.html").render(
        run=run, results=results, apis=apis, logo_src=logo_src,
        ssl_warn_days=config.SSL_WARN_DAYS, domain_warn_days=config.DOMAIN_WARN_DAYS,
        check_times=config.CHECK_TIMES, tz=config.TZ,
        dashboard_url=config.DASHBOARD_URL,
    )


def render_standalone(run, results, apis):
    """Report as a self-contained HTML file (logo embedded), for download."""
    return render_report(run, results, apis, LOGO_DATA_URI)


def mask(addr):
    """j***@example.com: recipients are not shown in full on the public page."""
    user, _, domain = addr.partition("@")
    return f"{user[:1]}***@{domain}"


def _connect():
    # Verify the server certificate: smtplib does not by default, which exposes the SMTP login
    ctx = ssl.create_default_context()
    if config.SMTP_PORT == 465:
        server = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=30, context=ctx)
    else:
        server = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30)
        if config.SMTP_TLS:
            server.starttls(context=ctx)
    if config.SMTP_USER:  # no login for relays that allow the server by IP
        server.login(config.SMTP_USER, config.SMTP_PASS)
    return server


def subject_noun(apis):
    return "services" if apis else "websites"


def render_text(run, results, apis):
    """Plain-text version of the report, for text-only mail clients and spam filters."""
    everything = results + apis
    down = [r for r in everything if r.status == "DOWN"]
    slow = [r for r in everything if r.status == "SLOW"]
    noun = subject_noun(apis)
    if down:
        headline = f"{len(down)} of {run['total']} {noun} are down"
    elif slow:
        headline = f"All {noun} reachable, {len(slow)} responding slowly"
    else:
        headline = f"All {run['total']} {noun} are operational"
    lines = [f"StatRep status report – {run['started_at']}", "", headline, ""]
    for title, items in (("Websites", results), ("APIs", apis)):
        if items:
            lines.append(f"{title}:")
            for r in sorted(items, key=lambda r: {"DOWN": 0, "SLOW": 1}.get(r.status, 2)):
                detail = r.error or (f"{r.ms} ms" if r.ms else "")
                lines.append(f"  {r.status:<4}  {r.name}  {detail}".rstrip())
            lines.append("")
    if config.DASHBOARD_URL:
        lines.append(f"Dashboard: {config.DASHBOARD_URL}")
    return "\n".join(lines)


def _build_message(run, results, apis):
    down = [r for r in results + apis if r.status == "DOWN"]
    noun = subject_noun(apis)
    if down:
        subject = f"[StatRep] {len(down)} of {run['total']} {noun} down – {run['started_at']}"
    else:
        subject = f"[StatRep] All {run['total']} {noun} operational – {run['started_at']}"

    # related( alternative( text, html ), logo )
    body = MIMEMultipart("alternative")
    body.attach(MIMEText(render_text(run, results, apis), "plain", "utf-8"))
    body.attach(MIMEText(render_report(run, results, apis, f"cid:{LOGO_CID}"), "html", "utf-8"))
    msg = MIMEMultipart("related")
    msg["Subject"] = subject
    msg["From"] = f"StatRep <{config.MAIL_FROM}>"
    msg.attach(body)

    logo = MIMEImage(LOGO_BYTES, "png")
    logo.add_header("Content-ID", f"<{LOGO_CID}>")
    logo.add_header("Content-Disposition", "inline", filename="statrep-logo.png")
    msg.attach(logo)
    return msg


def _close(server):
    try:
        server.quit()
    except (smtplib.SMTPException, OSError):
        pass


def send_report(run, results, apis):
    """Send each recipient their own copy over one SMTP connection.

    If the connection drops part-way, it reconnects once and carries on with the remaining
    recipients. Returns the recipients that did not get the report; raises only if the server
    can't be reached or logged into at the start.
    """
    msg = _build_message(run, results, apis)
    pending, failed, reconnected = list(config.MAIL_TO), [], False
    server = _connect()
    try:
        while pending:
            addr = pending[0]
            del msg["To"]
            msg["To"] = addr
            try:
                server.send_message(msg)
                pending.pop(0)
            except (smtplib.SMTPRecipientsRefused, smtplib.SMTPDataError):
                log.exception("Recipient refused: %s", addr)
                failed.append(pending.pop(0))
            except (smtplib.SMTPServerDisconnected, OSError):
                if reconnected:
                    log.exception("SMTP connection lost again; not sent to %d recipients", len(pending))
                    failed += pending
                    break
                log.warning("SMTP connection lost, reconnecting")
                reconnected = True
                _close(server)
                try:
                    server = _connect()
                except (smtplib.SMTPException, OSError):
                    log.exception("SMTP reconnect failed; not sent to %d recipients", len(pending))
                    failed += pending
                    break
    finally:
        _close(server)
    return failed
