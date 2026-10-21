"""Builds and sends the HTML status email."""
import base64
import smtplib
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import config, filters

APP_DIR = Path(__file__).parent
LOGO = APP_DIR / "static" / "logo-email.png"
LOGO_CID = "statrep-logo"

env = Environment(
    loader=FileSystemLoader(APP_DIR / "templates"),
    autoescape=select_autoescape(),
)
filters.register(env)


def render_report(run, results, logo_src):
    return env.get_template("email.html").render(
        run=run, results=results, logo_src=logo_src,
        ssl_warn_days=config.SSL_WARN_DAYS, domain_warn_days=config.DOMAIN_WARN_DAYS,
        check_times=config.CHECK_TIMES, tz=config.TZ,
        dashboard_url=config.DASHBOARD_URL,
    )


def render_standalone(run, results):
    """Report as a self-contained HTML file (logo embedded), for download."""
    logo = "data:image/png;base64," + base64.b64encode(LOGO.read_bytes()).decode()
    return render_report(run, results, logo)


def send_report(run, results):
    down = [r for r in results if r.status == "DOWN"]
    if down:
        subject = f"[StatRep] {len(down)} of {run['total']} websites down – {run['started_at']}"
    else:
        subject = f"[StatRep] All {run['total']} websites operational – {run['started_at']}"

    msg = MIMEMultipart("related")
    msg["Subject"] = subject
    msg["From"] = f"StatRep <{config.MAIL_FROM}>"
    msg["To"] = ", ".join(config.MAIL_TO)
    msg.attach(MIMEText(render_report(run, results, f"cid:{LOGO_CID}"), "html", "utf-8"))

    logo = MIMEImage(LOGO.read_bytes(), "png")
    logo.add_header("Content-ID", f"<{LOGO_CID}>")
    logo.add_header("Content-Disposition", "inline", filename="statrep-logo.png")
    msg.attach(logo)

    if config.SMTP_PORT == 465:
        server = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=30)
    else:
        server = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30)
        server.starttls()
    with server:
        server.login(config.SMTP_USER, config.SMTP_PASS)
        server.send_message(msg)
