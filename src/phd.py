"""PhD & research-track internships for the Sheet's "PhD & Research" tab.

A separate component on top of the main pipeline's data. It never changes
what's stored or announced; it reads jobs.db.

Track (stored per job as research_track):
  phd          aimed at PhD students: says PhD / doctoral in the title, or the
               description asks for current PhD students / candidates
  research_ms  research-track but open to MS students: a research-type title
               (Research Intern, Applied Scientist, Research Engineer, ...) or
               a description asking for MS or PhD students

The tab lists open US ones newest-posted first, like every other tab.
"""

from __future__ import annotations

import re
import sqlite3

from . import dedupe, geo, profile

_PHD_TITLE = re.compile(r"\bph\.?\s?d\b|\bdoctoral\b|\bdoctorate\b", re.I)
_PHD_DESC = re.compile(
    r"\b(?:current(?:ly)?|enrolled|pursuing|candidate|student)s?\b[^.]{0,60}"
    r"\b(?:ph\.?\s?d|doctoral|doctorate)\b|\bph\.?\s?d\.?\s+(?:student|candidate)s?\b", re.I)
_RESEARCH_TITLE = re.compile(
    r"\bresearch(?:er)?\b|\bapplied\s+scientist\b|\bresearch\s+(?:scientist|engineer)\b|"
    r"\bscientist\b|\br\s*&\s*d\b", re.I)
_MS_OR_PHD = re.compile(
    r"\b(?:m\.?s\.?|master'?s|msc)\b\s*(?:/|or|and|,)\s*(?:ph\.?\s?d)\b|"
    r"\bph\.?\s?d\.?\s*(?:/|or|and|,)\s*(?:m\.?s\.?|master'?s|msc)\b|"
    r"\bgraduate students?\b", re.I)


def track(title: str, description: str = "") -> str:
    """'phd' | 'research_ms' | '' for an internship's title and description."""
    if _PHD_TITLE.search(title or ""):
        return "phd"
    desc = description or ""
    if _PHD_DESC.search(desc) and not _MS_OR_PHD.search(desc):
        return "phd"
    if _RESEARCH_TITLE.search(title or "") or _MS_OR_PHD.search(desc):
        return "research_ms"
    return ""


def posted_sort_key(job: dict) -> tuple[str, str]:
    """Newest-posted first when sorted in reverse; jobs without a post date
    fall back to when we first saw them."""
    return ((job.get("posted_on") or (job.get("first_seen") or "")[:10]),
            job.get("first_seen") or "")


def open_internships(conn: sqlite3.Connection, settings: dict) -> list[dict]:
    """Open PhD/research-track internships in the US, newest-posted first."""
    categories = set(settings.get("announce_categories") or ())
    conn.row_factory = sqlite3.Row
    jobs = [dict(r) for r in conn.execute(
        "SELECT * FROM jobs WHERE status = 'active' AND role_type = 'intern' "
        "AND research_track IN ('phd', 'research_ms')")]
    out = []
    for j in jobs:
        if not geo.is_us(j.get("location") or "") or geo.title_names_foreign_place(j["title"]):
            continue
        if categories and j.get("category") not in categories:
            continue
        if not profile.fits(j, settings):
            continue
        out.append(j)
    out.sort(key=posted_sort_key, reverse=True)
    return dedupe.collapse_roles(out)
