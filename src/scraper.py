"""Fetch job postings from Workday's CXS jobs API.

Workday career sites are JS-rendered, but the page itself calls a JSON endpoint:

    POST https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs

We call that endpoint directly. No browser, no DOM scraping.
"""

from __future__ import annotations

import time

import requests

from . import classify, dates, http_pool
from .posting import JobPosting

_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (job-tracker)",
}
_SESSION = http_pool.make_session(_HEADERS)


def _base_url(tenant: str, wd: str) -> str:
    return f"https://{tenant}.{wd}.myworkdayjobs.com"


def _jobs_endpoint(tenant: str, wd: str, site: str) -> str:
    return f"{_base_url(tenant, wd)}/wday/cxs/{tenant}/{site}/jobs"


def job_id_for(tenant: str, external_path: str) -> str | None:
    """Requisition ids (R12345, JR2016444) are only unique within a tenant."""
    tail = external_path.rsplit("_", 1)[-1].strip() if "_" in external_path else ""
    return f"wd_{tenant.lower()}_{tail}" if tail else None


def fetch_company_jobs(company: dict, settings: dict) -> tuple[list[JobPosting], bool]:
    """All postings for one company across every configured search term.

    Returns (postings, complete). complete=False means some request failed, so
    absence from this result says nothing about whether a job was taken down.
    """
    tenant, wd, site = company["tenant"], company["wd"], company["site"]
    name = company.get("name", tenant)
    next_year = str(dates.today().year + 1)
    terms = [t.replace("{next_year}", next_year)
             for t in company.get("search_terms", settings["search_terms"])]

    endpoint = _jobs_endpoint(tenant, wd, site)
    base = _base_url(tenant, wd)
    limit = settings["page_limit"]
    timeout = settings["request_timeout"]
    delay = settings["delay_between_requests"]
    # Search is fuzzy ("intern" also hits "internal", "new grad" hits any
    # "new") but relevance-ranked: entry-level titles cluster on the first
    # pages. Stop once a page has fewer than this many of them.
    min_relevant = settings.get("workday_min_relevant_per_page", 2)
    anchor = dates.today()

    seen: dict[str, JobPosting] = {}
    complete = True

    for term in terms:
        offset, total = 0, None
        for _ in range(settings["max_pages_per_term"]):
            payload = {"appliedFacets": {}, "limit": limit, "offset": offset,
                       "searchText": term}
            try:
                resp = _SESSION.post(endpoint, json=payload, timeout=timeout)
                resp.raise_for_status()
                data = resp.json()
            except (requests.RequestException, ValueError) as exc:
                print(f"  ! {name} term={term!r} offset={offset}: {exc}")
                complete = False
                break

            postings = data.get("jobPostings", [])
            if not postings:
                break
            # Workday only reports `total` on the first page (0 afterwards).
            if total is None:
                total = data.get("total") or 0

            relevant = sum(classify.is_entry_level(p.get("title", "")) for p in postings)

            for p in postings:
                ext = p.get("externalPath", "")
                job_id = job_id_for(tenant, ext)
                if not job_id or job_id in seen:
                    continue
                seen[job_id] = JobPosting(
                    job_id=job_id,
                    company=name,
                    title=p.get("title", "").strip(),
                    apply_url=f"{base}/{site}{ext}",
                    location=p.get("locationsText", "").strip(),
                    posted_on=dates.relative_to_iso(p.get("postedOn", ""), anchor),
                    source="workday",
                )

            offset += limit
            if offset >= total or relevant < min_relevant:
                break
            time.sleep(delay)

    return list(seen.values()), complete
