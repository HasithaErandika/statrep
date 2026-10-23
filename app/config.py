"""All settings come from environment variables (see .env.example)."""
import logging
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

log = logging.getLogger("statrep")
EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")

BASE_DIR = Path(__file__).resolve().parent.parent
SITES_FILE = os.environ.get("SITES_FILE", str(BASE_DIR / "config" / "sites.yml"))
LEGACY_SITES_FILE = BASE_DIR / "config" / "sites.txt"
APIS_FILE = os.environ.get("APIS_FILE", str(BASE_DIR / "config" / "apis.yml"))


class ConfigError(SystemExit):
    """A bad setting in .env: stop with one clear line instead of a traceback."""

    def __init__(self, message):
        message = f"StatRep configuration error: {message}"
        print(message, file=sys.stderr, flush=True)  # gunicorn may not show a SystemExit message
        super().__init__(message)


def _int(name, default, minimum=0, maximum=None):
    raw = os.environ.get(name, "").strip() or str(default)
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name}={raw!r} must be a whole number") from None
    if value < minimum or (maximum is not None and value > maximum):
        limit = f"between {minimum} and {maximum}" if maximum is not None else f"at least {minimum}"
        raise ConfigError(f"{name}={value} must be {limit}")
    return value


def _times(raw):
    times = []
    for t in (t.strip() for t in raw.split(",")):
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", t)
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            raise ConfigError(f"CHECK_TIMES entry {t!r} must be HH:MM, e.g. 06:00,16:30")
        times.append(f"{int(m.group(1)):02d}:{m.group(2)}")
    return sorted(set(times))


# Schedule: comma-separated HH:MM times, every day
TZ = os.environ.get("TZ", "Asia/Colombo")
try:
    ZoneInfo(TZ)
except (ZoneInfoNotFoundError, ValueError):
    raise ConfigError(f"TZ={TZ!r} is not a known time zone, e.g. Asia/Colombo") from None
CHECK_TIMES = _times(os.environ.get("CHECK_TIMES", "") or "06:00,07:45,16:30,20:00")

# Checks
TIMEOUT = _int("CHECK_TIMEOUT", 20, 1, 120)       # seconds per request
RETRIES = _int("CHECK_RETRIES", 1, 0, 5)          # extra attempts before DOWN
SLOW_MS = _int("SLOW_MS", 5000, 1)                # slower than this = SLOW
API_SLOW_MS = _int("API_SLOW_MS", 2000, 1)        # default SLOW limit for APIs
SSL_WARN_DAYS = _int("SSL_WARN_DAYS", 14)
DOMAIN_WARN_DAYS = _int("DOMAIN_WARN_DAYS", 30)
CONCURRENCY = _int("CHECK_CONCURRENCY", 40, 1, 200)  # sites/APIs checked at the same time (details)
TIMING_CONCURRENCY = _int("TIMING_CONCURRENCY", 10, 1, 200)  # timed requests at the same time

# Email
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp-relay.brevo.com")
SMTP_PORT = _int("SMTP_PORT", 587, 1, 65535)
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
MAIL_FROM = os.environ.get("MAIL_FROM", "") or SMTP_USER


def _recipients(raw):
    """MAIL_TO: addresses separated by commas, semicolons or spaces; invalid ones are skipped."""
    out = []
    for addr in re.split(r"[,;\s]+", raw):
        if not addr:
            continue
        if not EMAIL_RE.match(addr):
            log.warning("Ignoring invalid MAIL_TO address: %r", addr)
        elif addr.lower() not in (a.lower() for a in out):
            out.append(addr)
    return out


MAIL_TO = _recipients(os.environ.get("MAIL_TO", ""))

DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "")  # adds an "Open dashboard" button to emails

# Abuse limits for the public page
RUN_COOLDOWN = _int("RUN_COOLDOWN", 120)                  # seconds between manual runs
QUICK_CHECKS_PER_MIN = _int("QUICK_CHECKS_PER_MIN", 6, 1)  # per client IP

# Optional login for the page (HTTP Basic auth); off unless both are set
AUTH_USER = os.environ.get("AUTH_USER", "")
AUTH_PASSWORD = os.environ.get("AUTH_PASSWORD", "")
if bool(AUTH_USER) != bool(AUTH_PASSWORD):
    raise ConfigError("set both AUTH_USER and AUTH_PASSWORD, or neither")

# Number of reverse proxies in front of the app whose X-Forwarded-For can be trusted (0 = none)
TRUSTED_PROXIES = _int("TRUSTED_PROXIES", 0, 0, 5)

if not (SMTP_USER and SMTP_PASS and MAIL_TO):
    EMAIL_PROBLEM = "SMTP settings are missing in .env"
elif not EMAIL_RE.match(MAIL_FROM):
    EMAIL_PROBLEM = f"MAIL_FROM {MAIL_FROM!r} is not a valid email address"
else:
    EMAIL_PROBLEM = None
EMAIL_CONFIGURED = EMAIL_PROBLEM is None


def _read_yaml_list(path):
    """(entries, None) from a YAML list file, or (None, error message) if it can't be used."""
    try:
        data = yaml.safe_load(path.read_text()) or []
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        return None, f"Config error: invalid YAML{f' (line {mark.line + 1})' if mark else ''}"
    if not isinstance(data, list):
        return None, "Config error: the file must be a list (entries starting with '- ')"
    return data, None


def _site_entry(raw):
    """Normalise one sites.yml entry: a URL, or a mapping with url and an optional name."""
    if isinstance(raw, str):
        raw = {"url": raw}
    if not isinstance(raw, dict):
        return {"url": "", "name": str(raw), "config_error": "Config error: entry must be a URL or have a url"}
    url = str(raw.get("url") or "").strip()
    if url and "://" not in url:
        url = "https://" + url
    site = {"url": url, "name": raw.get("name") and str(raw["name"])}
    unknown = set(raw) - {"url", "name"}
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        return {**site, "name": site["name"] or url or "Site", "config_error": "Config error: url must be an http(s) address"}
    if unknown:
        return {**site, "config_error": f"Config error: unknown option: {', '.join(sorted(map(str, unknown)))}"}
    return site


def load_sites():
    """Read config/sites.yml: a list of URLs, or {url, name} entries (see the examples in that file).

    Falls back to the old config/sites.txt (one URL per line) if sites.yml doesn't exist.
    """
    path = Path(SITES_FILE)
    if not path.exists():
        if LEGACY_SITES_FILE.exists():
            return [_site_entry(line.strip()) for line in LEGACY_SITES_FILE.read_text().splitlines()
                    if line.strip() and not line.strip().startswith("#")]
        return []
    data, error = _read_yaml_list(path)
    if error:
        return [{"url": "", "name": path.name, "config_error": error}]
    return [_site_entry(raw) for raw in data]


API_METHODS = {"GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"}
ENV_REF = re.compile(r"\$\{(\w+)\}")


def _fill_env(value, missing):
    """Replace ${VAR} with the environment variable, in strings nested anywhere in value."""
    if isinstance(value, str):
        def sub(m):
            if m.group(1) not in os.environ:
                missing.add(m.group(1))
                return ""
            return os.environ[m.group(1)]
        return ENV_REF.sub(sub, value)
    if isinstance(value, dict):
        return {k: _fill_env(v, missing) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill_env(v, missing) for v in value]
    return value


def _api_entry(i, raw):
    """Normalise one apis.yml entry. Problems become a config_error shown in the report."""
    if not isinstance(raw, dict):
        return {"name": f"API #{i}", "display_url": "", "config_error": "Entry must be a mapping (name, url, ...)"}
    name = str(raw.get("name") or f"API #{i}")
    url = str(raw.get("url") or "")
    method = str(raw.get("method", "GET")).upper()
    api = {"name": name, "display_url": url, "method": method}

    status = raw.get("expect_status")
    status = [status] if isinstance(status, int) else status
    unknown = set(raw) - {"name", "url", "method", "headers", "body", "expect_status", "expect_json",
                          "expect_text", "slow_ms", "timeout", "retry", "follow_redirects"}
    if not url.lower().startswith(("http://", "https://")):
        error = "url must start with http:// or https://"
    elif method not in API_METHODS:
        error = f"Unsupported method {method}"
    elif status is not None and not (isinstance(status, list) and all(isinstance(c, int) for c in status)):
        error = "expect_status must be a number or a list of numbers"
    elif not isinstance(raw.get("headers") or {}, dict) or not isinstance(raw.get("expect_json") or {}, dict):
        error = "headers and expect_json must be mappings"
    elif unknown:
        error = f"Unknown option: {', '.join(sorted(unknown))}"
    else:
        error = None
    if error:
        return {**api, "config_error": f"Config error: {error}"}

    missing = set()
    filled = _fill_env({"url": url, "headers": raw.get("headers") or {}, "body": raw.get("body")}, missing)
    if missing:
        return {**api, "config_error": f"Config error: missing in .env: {', '.join(sorted(missing))}"}
    return {
        **api,
        "url": filled["url"],
        "headers": {str(k): str(v) for k, v in filled["headers"].items()},
        "body": filled["body"],
        "expect_status": status or [],
        "expect_json": raw.get("expect_json") or {},
        "expect_text": str(raw["expect_text"]) if raw.get("expect_text") is not None else None,
        "slow_ms": int(raw.get("slow_ms", API_SLOW_MS)),
        "timeout": int(raw.get("timeout", TIMEOUT)),
        # Repeating POST/PUT/... could create data twice, so only safe methods retry by default
        "retry": bool(raw.get("retry", method in {"GET", "HEAD", "OPTIONS"})),
        "follow_redirects": bool(raw.get("follow_redirects", True)),
    }


def load_apis():
    """Read config/apis.yml: a list of APIs to call (see the examples in that file)."""
    path = Path(APIS_FILE)
    if not path.exists():
        return []
    data, error = _read_yaml_list(path)
    if error:
        return [{"name": path.name, "display_url": "", "config_error": error}]
    return [_api_entry(i, raw) for i, raw in enumerate(data, 1)]
