"""New-grad jobs with plain titles, found by reading their descriptions.

About half of curated new-grad jobs have a title that says nothing about
level ("Software Engineer", "Data Analyst"); only the description does
("new graduates", "0-2 years"). Greenhouse, Lever and Ashby hand their
descriptions over with the listing (enrich.described_fields), but Workday,
Oracle, iCIMS, SmartRecruiters, SuccessFactors and Eightfold need one
request per job, so their plain-titled jobs were never stored: our own
scrapers found ~60% of the ones Simplify lists (2026-10-10), and none that
it doesn't.

Each run reads up to a capped number of new, US, tech, plain-titled postings
from those sources, newest first. A description that marks the job new-grad
becomes its role_hint; every read id is remembered in `described` so it's
read once.
"""

from __future__ import annotations

import dataclasses
import logging
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from . import classify, dates, enrich, geo

log = logging.getLogger(__name__)

SOURCES = {"workday", "oracle", "icims", "smartrecruiters", "successfactors", "eightfold",
           "bamboohr", "jazzhr"}


def wanted(p, known: set[str], read: set[str], max_age_days: int | None = None) -> bool:
    """Whether a posting is worth a description read: new, not read before,
    on a listing-only source, US, tech, with a title that doesn't say level."""
    if (p.source not in SOURCES or p.job_id in known or p.job_id in read
            or p.role_hint or classify.classify_by_keyword(p.title) is not None
            or classify.categorize(p.title) == "other"
            or not geo.is_us_job(p.title, p.location)):
        return False
    age = dates.age_days(p.posted_on)
    return max_age_days is None or age is None or age <= max_age_days


def read_ids(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT job_id FROM described")}


def candidates(postings: list, known: set[str], read: set[str],
               max_age_days: int | None = None) -> list:
    """Postings worth a description read, newest posted first."""
    out = [p for p in postings if wanted(p, known, read, max_age_days)]
    out.sort(key=lambda p: p.posted_on or "", reverse=True)
    return out


def _read(p) -> tuple[str, bool, str]:
    """(description, definite, country): definite = read, or the page is
    gone; country as the job page names it (Workday), else ''."""
    job = {"job_id": p.job_id, "source": p.source, "apply_url": p.apply_url}
    text = enrich._description_for(job)
    time.sleep(enrich._DELAY)
    return text, bool(text) or bool(job.get("closed")), job.get("country", "")


def fresh_enough(job: dict, capped: bool, fresh_days: int = 3) -> bool:
    """Announce a job found this way only when it's new: posted in the last
    few days, or undated (iCIMS, SuccessFactors) once the backlog is read."""
    age = dates.age_days(job.get("posted_on") or "")
    return age <= fresh_days if age is not None else not capped


def find_new_grads(conn: sqlite3.Connection, postings: list, known: set[str],
                   limit: int = 500, max_age_days: int | None = None
                   ) -> tuple[list, set[str], bool]:
    """Return (postings, ids now hinted new_grad, whether the cap was hit).
    Postings the descriptions mark new-grad come back with role_hint set and
    their description attached, so enrichment doesn't fetch it again."""
    read = read_ids(conn)
    todo = candidates(postings, known, read, max_age_days)
    capped = len(todo) > limit
    todo = todo[:limit]
    if not todo:
        return postings, set(), False
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_read, todo))
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.executemany("INSERT OR REPLACE INTO described (job_id, checked_on) VALUES (?, ?)",
                     [(p.job_id, now) for p, (_, definite, _) in zip(todo, results) if definite])
    conn.commit()
    # The page's country rules out a bare-city location abroad; the attached
    # description means enrichment won't fetch the page again to learn it.
    hinted = {p.job_id: text for p, (text, _, country) in zip(todo, results)
              if classify.new_grad_from_description(text)
              and geo.is_us_job(p.title, p.location, country)}
    log.info(f"Plain titles: read {sum(bool(r[0]) for r in results)} of {len(todo)} descriptions"
             f"{' (capped)' if capped else ''}; {len(hinted)} are new-grad")
    if hinted:
        postings = [dataclasses.replace(p, role_hint="new_grad", description=hinted[p.job_id])
                    if p.job_id in hinted else p for p in postings]
    return postings, set(hinted), capped


def prune(conn: sqlite3.Connection, keep_days: int = 60) -> None:
    """Forget reads older than the listing window; such postings are gone or
    past the age limit anyway."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat(timespec="seconds")
    conn.execute("DELETE FROM described WHERE checked_on < ?", (cutoff,))
    conn.commit()
