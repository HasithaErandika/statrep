"""Settings are read from the environment at import time, so set safe test values first."""
import os

os.environ.update({
    "TZ": "Asia/Colombo",
    "CHECK_TIMES": "06:00,20:00",
    "SMTP_USER": "user",
    "SMTP_PASS": "pass",
    "MAIL_FROM": "statrep@example.com",
    "MAIL_TO": "a@example.com,b@example.com,c@example.com",
    "CHECK_RETRIES": "0",
    "RUN_COOLDOWN": "60",
    "QUICK_CHECKS_PER_MIN": "3",
})
for name in ("AUTH_USER", "AUTH_PASSWORD", "TRUSTED_PROXIES"):
    os.environ.pop(name, None)
