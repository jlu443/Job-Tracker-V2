"""Fetch job postings from Workday's CXS jobs API.

Workday career sites are JS-rendered, but the page itself calls a JSON endpoint:

    POST https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs

We call that endpoint directly. No browser, no DOM scraping.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import requests

from . import http_pool

_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (job-tracker)",
}
_SESSION = http_pool.make_session(_HEADERS)


@dataclass(frozen=True)
class JobPosting:
    job_id: str          # Workday's stable id, e.g. JR2016444
    company: str
    title: str
    apply_url: str
    location: str
    posted_on: str       # ISO date from Workday's postedOn field, or ""


def _base_url(tenant: str, wd: str) -> str:
    return f"https://{tenant}.{wd}.myworkdayjobs.com"


def _jobs_endpoint(tenant: str, wd: str, site: str) -> str:
    return f"{_base_url(tenant, wd)}/wday/cxs/{tenant}/{site}/jobs"


def _extract_job_id(posting: dict) -> str | None:
    """Workday puts the JR id in bulletFields[0]; fall back to the path tail."""
    bullets = posting.get("bulletFields") or []
    if bullets and bullets[0]:
        return bullets[0].strip()
    path = posting.get("externalPath", "")
    if "_" in path:
        return path.rsplit("_", 1)[-1].strip() or None
    return None


def fetch_company_jobs(company: dict, settings: dict) -> list[JobPosting]:
    """Return all postings for one company across every configured search term.

    Deduped on job_id (the same role can match multiple search terms).
    """
    tenant, wd, site = company["tenant"], company["wd"], company["site"]
    name = company.get("name", tenant)
    terms = company.get("search_terms", settings["search_terms"])

    endpoint = _jobs_endpoint(tenant, wd, site)
    base = _base_url(tenant, wd)
    limit = settings["page_limit"]
    timeout = settings["request_timeout"]
    delay = settings["delay_between_requests"]

    seen: dict[str, JobPosting] = {}

    for term in terms:
        offset = 0
        for _ in range(settings["max_pages_per_term"]):
            payload = {"appliedFacets": {}, "limit": limit, "offset": offset,
                       "searchText": term}
            try:
                resp = _SESSION.post(endpoint, json=payload, timeout=timeout)
                resp.raise_for_status()
                data = resp.json()
            except (requests.RequestException, ValueError) as exc:
                print(f"  ! {name} term={term!r} offset={offset}: {exc}")
                break

            postings = data.get("jobPostings", [])
            if not postings:
                break

            for p in postings:
                job_id = _extract_job_id(p)
                if not job_id or job_id in seen:
                    continue
                ext = p.get("externalPath", "")
                seen[job_id] = JobPosting(
                    job_id=job_id,
                    company=name,
                    title=p.get("title", "").strip(),
                    apply_url=f"{base}/{site}{ext}",
                    location=p.get("locationsText", "").strip(),
                    posted_on=p.get("postedOn", "").strip(),
                )

            offset += limit
            if offset >= data.get("total", 0):
                break
            time.sleep(delay)

    return list(seen.values())
