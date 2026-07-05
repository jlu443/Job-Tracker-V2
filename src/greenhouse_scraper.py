"""Fetch job postings from Greenhouse's public jobs board API.

Greenhouse exposes a free, unauthenticated JSON endpoint per company:

    GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs

Returns all current openings in one call (no pagination needed).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import requests

from . import http_pool

_HEADERS = {"User-Agent": "Mozilla/5.0 (job-tracker)"}
_SESSION = http_pool.make_session(_HEADERS)


@dataclass(frozen=True)
class JobPosting:
    job_id: str
    company: str
    title: str
    apply_url: str
    location: str
    posted_on: str
    source: str = field(default="greenhouse")


def fetch_company_jobs(company: dict, settings: dict) -> list[JobPosting]:
    token = company["token"]
    name = company.get("name", token)
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
    timeout = settings.get("request_timeout", 30)

    try:
        resp = _SESSION.get(url, timeout=timeout)
        if resp.status_code == 404:
            print(f"  ! {name}: token '{token}' not found (404)")
            return []
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"  ! {name}: {exc}")
        return []

    out = []
    for job in data.get("jobs", []):
        jid = job.get("id")
        if not jid:
            continue
        location = (job.get("location") or {}).get("name", "").strip()
        raw_date = job.get("updated_at", "")
        posted = raw_date[:10] if raw_date else ""
        out.append(JobPosting(
            job_id=f"gh_{jid}",
            company=name,
            title=(job.get("title") or "").strip(),
            apply_url=job.get("absolute_url", ""),
            location=location,
            posted_on=posted,
        ))

    time.sleep(settings.get("delay_between_requests", 0.5))
    return out
