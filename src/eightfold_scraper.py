"""Fetch job postings from Eightfold career sites (Microsoft, Qualcomm, ...).

Sites at https://{tenant}.eightfold.ai/careers (or a company host such as
apply.careers.microsoft.com) are backed by a public JSON search:

    GET https://{host}/api/pcsx/search?domain={domain}&query={term}
        &location=United States&start={offset}

Ten positions per page, relevance-ranked, with a total count; paging stops
once a page runs out of entry-level titles (as for Oracle). Descriptions
come from /api/pcsx/position_details (see enrich.py).
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone

import requests

from . import classify, http_pool
from .posting import JobPosting

log = logging.getLogger(__name__)

_SESSION = http_pool.make_session({"User-Agent": "Mozilla/5.0 (job-tracker)",
                                   "Accept": "application/json"})
_PAGE = 10
# Measured 2026-10-03 on Microsoft and Qualcomm: these return entry-level
# titles; "new grad" and "graduate" mostly return unrelated roles.
_TERMS = ("intern", "university", "early career", "entry level")

# apply.careers.microsoft.com/careers/job/1970393556953113
# qualcomm.eightfold.ai/careers/job/446716226621
_URL = re.compile(r"https?://(?:([a-z0-9-]+)\.eightfold\.ai|apply\.careers\.([a-z0-9-]+)\.com)"
                  r"/careers/job/(\d+)", re.I)


def job_id_for(tenant: str, position_id) -> str:
    return f"ef_{tenant.lower()}_{position_id}"


def canonical_id(url: str) -> str | None:
    m = _URL.search(url or "")
    return job_id_for(m.group(1) or m.group(2), m.group(3)) if m else None


def _date(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _search(host: str, timeout: int, params: dict) -> requests.Response:
    """One search request; Eightfold answers bursts with 429, so wait it out
    (Retry-After, else 10/20/40s) a few times before giving up."""
    for attempt in range(4):
        resp = _SESSION.get(f"https://{host}/api/pcsx/search", timeout=timeout, params=params)
        if resp.status_code != 429 or attempt == 3:
            return resp
        wait = resp.headers.get("Retry-After", "")
        time.sleep(min(int(wait), 120) if wait.isdigit() else 10 * 2 ** attempt)
    return resp


def fetch_company_jobs(company: dict, settings: dict) -> tuple[list[JobPosting], bool]:
    host, domain, tenant = company["host"], company["domain"], company["tenant"]
    name = company.get("name", tenant)
    timeout = settings.get("request_timeout", 30)
    delay = settings.get("delay_between_requests", 0.5)
    min_relevant = settings.get("workday_min_relevant_per_page", 2)
    max_pages = settings.get("max_pages_per_term", 25)

    seen: dict[str, JobPosting] = {}
    complete = True
    for term in company.get("search_terms", _TERMS):
        offset = 0
        for _ in range(max_pages):
            try:
                resp = _search(host, timeout, {"domain": domain, "query": term,
                                               "location": "United States", "start": offset})
                resp.raise_for_status()
                data = resp.json().get("data") or {}
            except (requests.RequestException, ValueError) as exc:
                log.warning(f"  ! {name} term={term!r} offset={offset}: {exc}")
                complete = False
                break
            positions = data.get("positions") or []
            for p in positions:
                pid = p.get("id")
                if not pid:
                    continue
                jid = job_id_for(tenant, pid)
                if jid in seen:
                    continue
                seen[jid] = JobPosting(
                    job_id=jid, company=name, title=(p.get("name") or "").strip(),
                    apply_url=f"https://{host}{p.get('positionUrl') or f'/careers/job/{pid}'}",
                    location="; ".join(p.get("standardizedLocations") or p.get("locations") or []),
                    posted_on=_date(p.get("postedTs")), source="eightfold")
            relevant = sum(classify.is_entry_level(p.get("name") or "") for p in positions)
            offset += _PAGE
            if not positions or offset >= (data.get("count") or 0) or relevant < min_relevant:
                break
            time.sleep(delay)
    return list(seen.values()), complete


def fetch_description(job: dict, timeout: int = 30) -> str:
    """Plain-ish text of one position (HTML stripped by the caller)."""
    m = _URL.search(job.get("apply_url", ""))
    if not m:
        return ""
    tenant, pid = (m.group(1) or m.group(2)).lower(), m.group(3)
    host = m.group(0).split("/")[2]
    resp = _SESSION.get(f"https://{host}/api/pcsx/position_details", timeout=timeout,
                        params={"domain": f"{tenant}.com", "position_id": pid, "hl": "en"})
    resp.raise_for_status()
    return (resp.json().get("data") or {}).get("jobDescription") or ""
