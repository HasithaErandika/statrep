"""Jinja filters shared by the web UI and the email."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import config


def ago(dt):
    """'5 min ago', '3 days ago', ..."""
    if not dt:
        return ""
    secs = (datetime.now(timezone.utc) - dt).total_seconds()
    for limit, size, unit in ((3600, 60, "min"), (86400, 3600, "hour"), (86400 * 30, 86400, "day"),
                              (86400 * 365, 86400 * 30, "month")):
        if secs < limit:
            n = max(1, int(secs // size))
            return f"{n} {unit}{'s' if n > 1 and unit != 'min' else ''} ago"
    n = int(secs // (86400 * 365))
    return f"{n} year{'s' if n > 1 else ''} ago"


def fmtdate(dt):
    """Local date, with time unless the source only gave a date."""
    if not dt:
        return ""
    if dt.hour == dt.minute == dt.second == 0:
        return dt.strftime("%d %b %Y")
    return dt.astimezone(ZoneInfo(config.TZ)).strftime("%d %b %Y, %H:%M")


def register(env):
    env.filters["ago"] = ago
    env.filters["fmtdate"] = fmtdate
