"""All settings come from environment variables (see .env.example)."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SITES_FILE = os.environ.get("SITES_FILE", str(BASE_DIR / "config" / "sites.txt"))

# Schedule: comma-separated HH:MM times, every day
TZ = os.environ.get("TZ", "Asia/Colombo")
CHECK_TIMES = [t.strip() for t in os.environ.get("CHECK_TIMES", "07:30,19:30").split(",") if t.strip()]

# Checks
TIMEOUT = int(os.environ.get("CHECK_TIMEOUT", "20"))   # seconds per request
RETRIES = int(os.environ.get("CHECK_RETRIES", "1"))    # extra attempts before DOWN
SLOW_MS = int(os.environ.get("SLOW_MS", "5000"))       # slower than this = SLOW
SSL_WARN_DAYS = int(os.environ.get("SSL_WARN_DAYS", "14"))
DOMAIN_WARN_DAYS = int(os.environ.get("DOMAIN_WARN_DAYS", "30"))

# Email
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp-relay.brevo.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
MAIL_FROM = os.environ.get("MAIL_FROM", SMTP_USER)
MAIL_TO = [a.strip() for a in os.environ.get("MAIL_TO", "").split(",") if a.strip()]

DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "")  # adds an "Open dashboard" button to emails

EMAIL_CONFIGURED =bool(SMTP_USER and SMTP_PASS and MAIL_TO)


def load_sites():
    """Read config/sites.txt: one URL per line, '#' for comments."""
    path = Path(SITES_FILE)
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")]
