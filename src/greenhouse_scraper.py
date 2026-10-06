"""Fetch job postings from Greenhouse's public jobs board API.

Greenhouse exposes a free, unauthenticated JSON endpoint per company:

    GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs

Returns all current openings in one call (no pagination needed).
"""

from __future__ import annotations

import html
import logging
import re
import time

import requests

from . import dates, enrich, http_pool
from .posting import JobPosting

log = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "Mozilla/5.0 (job-tracker)"}
_SESSION = http_pool.make_session(_HEADERS)
_TAG = re.compile(r"<[^>]+>")


def reused_posting_date(title: str, first_published: str, updated: str) -> str:
    """Companies re-use one posting each season and just rename it: Glean's
    "Software Engineer, Intern (Summer 2027)" was first published 2025-09-03,
    Perpay's in 2023. When first publication predates both the named
    season's recruiting (the year before it) and this year, the last update
    is the real post date. ("Class of 2029", first published in August 2026,
    is simply early.)"""
    year = dates.season_year(title)
    before = f"{min(year - 1, dates.today().year)}-01-01" if year else ""
    if updated and first_published and first_published < before:
        return updated
    return first_published


def fetch_company_jobs(company: dict, settings: dict) -> tuple[list[JobPosting], bool]:
    token = company["token"]
    name = company.get("name", token)
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
    timeout = settings.get("request_timeout", 30)

    try:
        # content=true adds each job's description: it's how plain-titled
        # new-grad roles ("Software Engineer") are recognized.
        resp = _SESSION.get(url, params={"content": "true"}, timeout=timeout)
        if resp.status_code == 404:
            log.warning(f"  ! {name}: token '{token}' not found (404)")
            return [], True
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        log.warning(f"  ! {name}: {exc}")
        return [], False

    out = []
    for job in data.get("jobs", []):
        jid = job.get("id")
        if not jid:
            continue
        location = ((job.get("location") or {}).get("name") or "").strip()
        # updated_at moves on every edit; first_published is the real post date.
        raw_date = job.get("first_published") or ""
        posted = raw_date[:10]
        title = (job.get("title") or "").strip()
        posted = reused_posting_date(title, posted, (job.get("updated_at") or "")[:10])
        description = _TAG.sub(" ", html.unescape(job.get("content") or ""))
        out.append(JobPosting(
            job_id=f"gh_{jid}",
            company=name,
            title=title,
            apply_url=job.get("absolute_url", ""),
            location=location,
            posted_on=posted,
            source="greenhouse",
            **enrich.described_fields(title, description),
        ))

    time.sleep(settings.get("delay_between_requests", 0.5))
    return out, True
