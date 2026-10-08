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


_TITLE_YEAR = re.compile(r"(?<!\d)(20[2-4]\d)(?!\d)")


def season_year(title: str) -> int | None:
    """The latest year a title names ("Summer 2027 Intern" -> 2027)."""
    years = [int(y) for y in _TITLE_YEAR.findall(title or "")]
    return max(years) if years else None


def upcoming_season_years(anchor: date | None = None) -> tuple[int, ...]:
    """Years whose internships / grad programs are still ahead: next year
    and the one after, plus this year until its summer is over."""
    d = anchor or today()
    return ((d.year,) if d.month <= 8 else ()) + (d.year + 1, d.year + 2)


def names_upcoming_season(title: str, anchor: date | None = None) -> bool:
    """"Software Engineer Intern (Summer 2027)" in October 2026: recruiting
    for a season that hasn't happened yet, however long ago it was posted."""
    return season_year(title) in upcoming_season_years(anchor)


def names_past_season(title: str, anchor: date | None = None) -> bool:
    """"New Grad 2025: Software Engineer" in October 2026: a season that's over."""
    year = season_year(title)
    return year is not None and year < min(upcoming_season_years(anchor))


# Exempt sources keep a listing past the age limit for up to this long; a
# curated list's year-old "active" listings were never updated (460 on
# 2026-10-08, e.g. Roblox "New Grad 2025: Software Engineer").
EXEMPT_MAX_DAYS = 365


def outlives_age_limit(source: str, title: str, posted_on: str,
                       exempt_sources: frozenset) -> bool:
    """A listing kept past max_listing_age_days while still open: one naming
    an upcoming season ("Summer 2027"), or from a source whose listings run
    for months, unless it names a season that's over or is over a year old."""
    if names_upcoming_season(title):
        return True
    if source not in exempt_sources or names_past_season(title):
        return False
    age = age_days(posted_on)
    return age is None or age <= EXEMPT_MAX_DAYS
