"""Fetch job postings from Lever's public postings API.

Lever exposes a free, unauthenticated JSON endpoint per company:

    GET https://api.lever.co/v0/postings/{slug}?mode=json

Returns all current postings in one call (no pagination needed). Boards on
Lever's EU instance (jobs.eu.lever.co, `region: eu` in lever.yaml) are
served by api.eu.lever.co instead.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone

import requests

from . import classify, enrich, http_pool
from .posting import JobPosting

log = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "Mozilla/5.0 (job-tracker)"}
_SESSION = http_pool.make_session(_HEADERS)


def api_url(company: dict) -> str:
    host = "api.eu.lever.co" if company.get("region") == "eu" else "api.lever.co"
    return f"https://{host}/v0/postings/{company['slug']}?mode=json"


def fetch_company_jobs(company: dict, settings: dict) -> tuple[list[JobPosting], bool]:
    slug = company["slug"]
    name = company.get("name", slug)
    url = api_url(company)
    timeout = settings.get("request_timeout", 30)

    try:
        resp = _SESSION.get(url, timeout=timeout)
        if resp.status_code == 404:
            log.warning(f"  ! {name}: slug '{slug}' not found (404)")
            return [], True
        resp.raise_for_status()
        postings = resp.json()
    except (requests.RequestException, ValueError) as exc:
        log.warning(f"  ! {name}: {exc}")
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
        fields = enrich.described_fields(title, description)
        # "Internship", "Early Career Talent": the board's own level label.
        fields["role_hint"] = (classify.hint_from_level(categories.get("commitment") or "")
                               or fields["role_hint"])
        salary = p.get("salaryRange") or {}
        if salary.get("min") and (salary.get("currency") or "USD") == "USD":
            unit = "per hour" if "hour" in (salary.get("interval") or "") else ""
            fields["pay"] = (enrich.parse_pay(f"${salary['min']} - ${salary.get('max') or salary['min']} {unit}")
                             or fields.get("pay", ""))
        out.append(JobPosting(
            job_id=f"lv_{uid}",
            company=name,
            title=title,
            apply_url=p.get("hostedUrl", ""),
            location=location,
            posted_on=posted,
            source="lever",
            **fields,
        ))

    time.sleep(settings.get("delay_between_requests", 0.5))
    return out, True
