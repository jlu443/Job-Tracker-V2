"""Fetch job postings from Lever's public postings API.

Lever exposes a free, unauthenticated JSON endpoint per company:

    GET https://api.lever.co/v0/postings/{slug}?mode=json

Returns all current postings in one call (no pagination needed).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

_HEADERS = {"User-Agent": "Mozilla/5.0 (job-tracker)"}


@dataclass(frozen=True)
class JobPosting:
    job_id: str
    company: str
    title: str
    apply_url: str
    location: str
    posted_on: str
    source: str = field(default="lever")


def fetch_company_jobs(company: dict, settings: dict) -> list[JobPosting]:
    slug = company["slug"]
    name = company.get("name", slug)
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    timeout = settings.get("request_timeout", 30)

    try:
        resp = requests.get(url, headers=_HEADERS, timeout=timeout)
        if resp.status_code == 404:
            print(f"  ! {name}: slug '{slug}' not found (404)")
            return []
        resp.raise_for_status()
        postings = resp.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"  ! {name}: {exc}")
        return []

    out = []
    for p in postings:
        uid = p.get("id", "").strip()
        if not uid:
            continue
        categories = p.get("categories") or {}
        all_locs = categories.get("allLocations") or []
        location = (all_locs[0] if all_locs else categories.get("location", "")).strip()
        posted = ""
        created_ms = p.get("createdAt")
        if created_ms:
            try:
                dt = datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc)
                posted = dt.strftime("%Y-%m-%d")
            except (OSError, ValueError):
                pass
        out.append(JobPosting(
            job_id=f"lv_{uid}",
            company=name,
            title=(p.get("text") or "").strip(),
            apply_url=p.get("hostedUrl", ""),
            location=location,
            posted_on=posted,
        ))

    time.sleep(settings.get("delay_between_requests", 0.5))
    return out
