"""Fetch postings from Rippling ATS job boards (ats.rippling.com/{slug}).

Each board exposes its whole list through a public JSON endpoint:

    GET https://ats.rippling.com/api/v2/board/{slug}/jobs?page={n}&pageSize=100
"""

from __future__ import annotations

import time

import requests

from . import http_pool
from .posting import JobPosting

_SESSION = http_pool.make_session({"User-Agent": "Mozilla/5.0 (job-tracker)",
                                   "Accept": "application/json"})


def _location(locations: list) -> str:
    out = []
    for loc in locations or []:
        text = loc.get("name") or ", ".join(
            p for p in (loc.get("city"), loc.get("stateCode"), loc.get("countryCode")) if p)
        if loc.get("country") and loc.get("country") not in text:
            text = f"{text}, {loc['country']}" if text else loc["country"]
        if text and text not in out:
            out.append(text)
    return "; ".join(out)


def fetch_company_jobs(company: dict, settings: dict) -> tuple[list[JobPosting], bool]:
    slug = company["slug"]
    name = company.get("name", slug)
    timeout = settings.get("request_timeout", 30)
    out: list[JobPosting] = []
    for page in range(0, 50):
        try:
            resp = _SESSION.get(f"https://ats.rippling.com/api/v2/board/{slug}/jobs",
                                params={"page": page, "pageSize": 100}, timeout=timeout)
            if resp.status_code == 404:
                print(f"  ! {name}: board '{slug}' not found (404)")
                return [], True
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            print(f"  ! {name}: {exc}")
            return out, False
        for j in data.get("items") or []:
            uid = j.get("id")
            if not uid:
                continue
            out.append(JobPosting(
                job_id=f"rip_{uid}", company=name, title=(j.get("name") or "").strip(),
                apply_url=j.get("url") or f"https://ats.rippling.com/{slug}/jobs/{uid}",
                location=_location(j.get("locations")), posted_on="", source="rippling"))
        if page + 1 >= (data.get("totalPages") or 0):
            break
        time.sleep(settings.get("delay_between_requests", 0.5))
    return out, True
