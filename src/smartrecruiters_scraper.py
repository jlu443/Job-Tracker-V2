"""Fetch job postings from SmartRecruiters' public postings API.

SmartRecruiters exposes a free, unauthenticated JSON endpoint per company:

    GET https://api.smartrecruiters.com/v1/companies/{company}/postings

Paginated via limit/offset; totalFound gives the stop condition.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import requests

_HEADERS = {"User-Agent": "Mozilla/5.0 (job-tracker)"}
_PAGE_SIZE = 100
_MAX_PAGES = 50   # safety cap: 5000 postings per company


@dataclass(frozen=True)
class JobPosting:
    job_id: str
    company: str
    title: str
    apply_url: str
    location: str
    posted_on: str
    source: str = field(default="smartrecruiters")


def _format_location(loc: dict) -> str:
    parts = [loc.get("city"), loc.get("region"), (loc.get("country") or "").upper()]
    return ", ".join(p for p in parts if p)


def fetch_company_jobs(company: dict, settings: dict) -> list[JobPosting]:
    company_id = company["company"]
    name = company.get("name", company_id)
    base = f"https://api.smartrecruiters.com/v1/companies/{company_id}/postings"
    timeout = settings.get("request_timeout", 30)
    delay = settings.get("delay_between_requests", 0.5)

    out, offset = [], 0
    for _ in range(_MAX_PAGES):
        try:
            resp = requests.get(base, params={"limit": _PAGE_SIZE, "offset": offset},
                                headers=_HEADERS, timeout=timeout)
            if resp.status_code == 404:
                print(f"  ! {name}: company '{company_id}' not found (404)")
                return out
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            print(f"  ! {name}: {exc}")
            return out

        content = data.get("content", [])
        for p in content:
            pid = p.get("id")
            if not pid:
                continue
            raw_date = p.get("releasedDate") or ""
            out.append(JobPosting(
                job_id=f"sr_{pid}",
                company=name,
                title=(p.get("name") or "").strip(),
                apply_url=f"https://jobs.smartrecruiters.com/{company_id}/{pid}",
                location=_format_location(p.get("location") or {}),
                posted_on=raw_date[:10],
            ))

        offset += len(content)
        if not content or offset >= data.get("totalFound", 0):
            break
        time.sleep(delay)

    time.sleep(delay)
    return out
