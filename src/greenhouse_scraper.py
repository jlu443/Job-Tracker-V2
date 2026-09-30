"""Fetch job postings from Greenhouse's public jobs board API.

Greenhouse exposes a free, unauthenticated JSON endpoint per company:

    GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs

Returns all current openings in one call (no pagination needed).
"""

from __future__ import annotations

import html
import re
import time

import requests

from . import classify, http_pool
from .posting import JobPosting

_HEADERS = {"User-Agent": "Mozilla/5.0 (job-tracker)"}
_SESSION = http_pool.make_session(_HEADERS)
_TAG = re.compile(r"<[^>]+>")


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
        title = (job.get("title") or "").strip()
        description = _TAG.sub(" ", html.unescape(job.get("content") or ""))
        out.append(JobPosting(
            job_id=f"gh_{jid}",
            company=name,
            title=title,
            apply_url=job.get("absolute_url", ""),
            location=location,
            posted_on=posted,
            source="greenhouse",
            role_hint=classify.role_hint_from_description(title, description),
        ))

    time.sleep(settings.get("delay_between_requests", 0.5))
    return out, True
