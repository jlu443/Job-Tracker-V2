"""Fetch job postings from Lever's public postings API.

Lever exposes a free, unauthenticated JSON endpoint per company:

    GET https://api.lever.co/v0/postings/{slug}?mode=json

Returns all current postings in one call (no pagination needed).
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone

import requests

from . import enrich, http_pool
from .posting import JobPosting

_HEADERS = {"User-Agent": "Mozilla/5.0 (job-tracker)"}
_SESSION = http_pool.make_session(_HEADERS)


def fetch_company_jobs(company: dict, settings: dict) -> tuple[list[JobPosting], bool]:
    slug = company["slug"]
    name = company.get("name", slug)
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    timeout = settings.get("request_timeout", 30)

    try:
        resp = _SESSION.get(url, timeout=timeout)
        if resp.status_code == 404:
            print(f"  ! {name}: slug '{slug}' not found (404)")
            return [], True
        resp.raise_for_status()
        postings = resp.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"  ! {name}: {exc}")
        return [], False

    out = []
    for p in postings:
        uid = (p.get("id") or "").strip()
        if not uid:
            continue
        categories = p.get("categories") or {}
        all_locs = categories.get("allLocations") or []
        location = ((all_locs[0] if all_locs else categories.get("location")) or "").strip()
        posted = ""
        created_ms = p.get("createdAt")
        if created_ms:
            try:
                dt = datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc)
                posted = dt.strftime("%Y-%m-%d")
            except (OSError, ValueError):
                pass
        title = (p.get("text") or "").strip()
        # Requirement bullets live in lists[], not descriptionPlain.
        description = " ".join([p.get("descriptionPlain") or ""] + [
            re.sub(r"<[^>]+>", " ", item.get("content") or "") for item in p.get("lists") or []])
        out.append(JobPosting(
            job_id=f"lv_{uid}",
            company=name,
            title=title,
            apply_url=p.get("hostedUrl", ""),
            location=location,
            posted_on=posted,
            source="lever",
            **enrich.described_fields(title, description),
        ))

    time.sleep(settings.get("delay_between_requests", 0.5))
    return out, True
