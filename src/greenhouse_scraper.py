"""Fetch job postings from Greenhouse's public jobs board API.

Greenhouse exposes a free, unauthenticated JSON endpoint per company:

    GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs

Returns all current openings in one call (no pagination needed).
"""

from __future__ import annotations

import time

import requests

from . import http_pool
from .posting import JobPosting

_HEADERS = {"User-Agent": "Mozilla/5.0 (job-tracker)"}
_SESSION = http_pool.make_session(_HEADERS)




def fetch_company_jobs(company: dict, settings: dict) -> tuple[list[JobPosting], bool]:
    token = company["token"]
    name = company.get("name", token)
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
    timeout = settings.get("request_timeout", 30)

    try:
        resp = _SESSION.get(url, timeout=timeout)
        if resp.status_code == 404:
            print(f"  ! {name}: token '{token}' not found (404)")
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
        location = (job.get("location") or {}).get("name", "").strip()
        # updated_at moves on every edit; first_published is the real post date.
        raw_date = job.get("first_published") or ""
        posted = raw_date[:10]
        out.append(JobPosting(
            job_id=f"gh_{jid}",
            company=name,
            title=(job.get("title") or "").strip(),
            apply_url=job.get("absolute_url", ""),
            location=location,
            posted_on=posted,
            source="greenhouse",
        ))

    time.sleep(settings.get("delay_between_requests", 0.5))
    return out, True
