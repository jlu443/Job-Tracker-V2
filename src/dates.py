"""Normalize every source's posted-date format to an ISO date (YYYY-MM-DD)."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

_RELATIVE = re.compile(r"(\d+)\+?\s*(day|week|month|hour|minute)s?", re.I)
_ISO_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}")
_UNIT_DAYS = {"minute": 0, "hour": 0, "day": 1, "week": 7, "month": 30}


def today() -> date:
    return datetime.now(timezone.utc).date()


def relative_to_iso(text: str, anchor: date | None = None) -> str:
    """'Posted 3 Days Ago' / 'Posted Today' / '2 weeks ago' → ISO date.

    Open-ended values like 'Posted 30+ Days Ago' resolve to their bound (30
    days back), so the result is "at least this old" — enough for staleness
    checks. Returns '' when the text isn't recognizable.
    """
    anchor = anchor or today()
    t = (text or "").strip().lower()
    if not t:
        return ""
    if _ISO_PREFIX.match(t):
        return t[:10]
    if "today" in t or "just" in t:
        return anchor.isoformat()
    if "yesterday" in t:
        return (anchor - timedelta(days=1)).isoformat()
    m = _RELATIVE.search(t)
    if not m:
        return ""
    days = int(m.group(1)) * _UNIT_DAYS[m.group(2).lower()]
    return (anchor - timedelta(days=days)).isoformat()


def epoch_to_iso(seconds) -> str:
    try:
        return datetime.fromtimestamp(float(seconds), tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def age_days(iso: str, anchor: date | None = None) -> int | None:
    if not iso or not _ISO_PREFIX.match(iso):
        return None
    return ((anchor or today()) - date.fromisoformat(iso[:10])).days
