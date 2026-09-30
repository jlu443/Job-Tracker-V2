"""Fetch job postings from Ashby's public posting API.

Ashby exposes a free, unauthenticated JSON endpoint per company:

    GET https://api.ashbyhq.com/posting-api/job-board/{slug}

Returns all listed openings in one call (no pagination needed).
"""

from __future__ import annotations

import time

import requests

from . import classify, http_pool, phd
from .posting import JobPosting

_HEADERS = {"User-Agent": "Mozilla/5.0 (job-tracker)"}
_SESSION = http_pool.make_session(_HEADERS)


def fetch_company_jobs(company: dict, settings: dict) -> tuple[list[JobPosting], bool]:
    slug = company["slug"]
    name = company.get("name", slug)
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    timeout = settings.get("request_timeout", 30)

    try:
        resp = _SESSION.get(url, timeout=timeout)
        if resp.status_code == 404:
            print(f"  ! {name}: slug '{slug}' not found (404)")
            return [], True
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"  ! {name}: {exc}")
        return [], False

    out = []
    for job in data.get("jobs", []):
        jid = job.get("id")
        if not jid:
            continue
        raw_date = job.get("publishedAt") or ""
        title = (job.get("title") or "").strip()
        description = job.get("descriptionPlain") or ""
        out.append(JobPosting(
            job_id=f"ash_{jid}",
            company=name,
            title=title,
            apply_url=job.get("jobUrl") or f"https://jobs.ashbyhq.com/{slug}/{jid}",
            location=(job.get("location") or "").strip(),
            posted_on=raw_date[:10],
            source="ashby",
            role_hint=classify.role_hint_from_description(title, description),
            research_track=phd.track(title, description),
        ))

    time.sleep(settings.get("delay_between_requests", 0.5))
    return out, True
