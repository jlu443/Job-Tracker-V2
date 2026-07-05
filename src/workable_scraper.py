"""Fetch job postings from Workable's public jobs API.

Workable career pages call an unauthenticated JSON endpoint per account:

    POST https://apply.workable.com/api/v3/accounts/{slug}/jobs

Paginated via an opaque nextPage token; total gives the stop condition.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import requests

from . import http_pool

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (job-tracker)",
    "Content-Type": "application/json",
}
_SESSION = http_pool.make_session(_HEADERS)
_MAX_PAGES = 200   # v3 returns 10 per page


@dataclass(frozen=True)
class JobPosting:
    job_id: str
    company: str
    title: str
    apply_url: str
    location: str
    posted_on: str
    source: str = field(default="workable")


def _format_location(job: dict) -> str:
    loc = job.get("location") or {}
    display = loc.get("display")
    if display:
        return display.strip()
    parts = [loc.get("city"), loc.get("region"), loc.get("country")]
    return ", ".join(p for p in parts if p)


def fetch_company_jobs(company: dict, settings: dict) -> list[JobPosting]:
    slug = company["slug"]
    name = company.get("name", slug)
    url = f"https://apply.workable.com/api/v3/accounts/{slug}/jobs"
    timeout = settings.get("request_timeout", 30)
    delay = settings.get("delay_between_requests", 0.5)

    out, token = [], None
    for _ in range(_MAX_PAGES):
        payload = {"query": ""}
        if token:
            payload["token"] = token
        try:
            resp = _SESSION.post(url, json=payload, timeout=timeout)
            if resp.status_code == 404:
                print(f"  ! {name}: account '{slug}' not found (404)")
                return out
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            print(f"  ! {name}: {exc}")
            return out

        results = data.get("results", [])
        for job in results:
            shortcode = job.get("shortcode")
            if not shortcode:
                continue
            raw_date = job.get("published") or ""
            out.append(JobPosting(
                job_id=f"wk_{shortcode}",
                company=name,
                title=(job.get("title") or "").strip(),
                apply_url=f"https://apply.workable.com/{slug}/j/{shortcode}/",
                location=_format_location(job),
                posted_on=raw_date[:10],
            ))

        token = data.get("nextPage")
        if not results or not token or len(out) >= data.get("total", 0):
            break
        time.sleep(delay)

    time.sleep(delay)
    return out
