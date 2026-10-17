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
# Curated lists also link a company's own front end (2026-10-16: 15 Qualcomm,
# 6 Microsoft listings didn't map to the ids we scrape):
#   careers.qualcomm.com/careers/job/446704474013
#   apply.careers.microsoft.com/careers?query=intern&...&pid=1970393556922931
_FRONT_URL = re.compile(r"https?://(?:careers\.([a-z0-9-]+)\.com|explore\.jobs\.([a-z0-9-]+)\.net)"
                        r"/careers/job/(\d+)", re.I)
_PID_URL = re.compile(r"https?://apply\.careers\.([a-z0-9-]+)\.com/careers\?[^#]*\bpid=(\d+)", re.I)


def job_id_for(tenant: str, position_id) -> str:
    return f"ef_{tenant.lower()}_{position_id}"


def canonical_id(url: str) -> str | None:
    m = _URL.search(url or "")
    if m:
        return job_id_for(m.group(1) or m.group(2), m.group(3))
    m = _FRONT_URL.search(url or "")
    if m:
        return job_id_for(m.group(1) or m.group(2), m.group(3))
    m = _PID_URL.search(url or "")
    return job_id_for(m.group(1), m.group(2)) if m else None


def _date(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _search(host: str, timeout: int, params: dict, api: str = "pcsx") -> requests.Response:
    """One search request; Eightfold answers bursts with 429, so wait it out
    (Retry-After, else 10/20/40s) a few times before giving up. Older sites
    (Netflix: "PCSX is not enabled", 2026-10-17) answer the v2 API instead:
    /api/apply/v2/jobs, positions at the top level, t_create for the date."""
    path = "/api/apply/v2/jobs" if api == "v2" else "/api/pcsx/search"
    if api == "v2":
        params = {**params, "num": _PAGE}
    for attempt in range(4):
        resp = _SESSION.get(f"https://{host}{path}", timeout=timeout, params=params)
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
                api = company.get("api", "pcsx")
                resp = _search(host, timeout, {"domain": domain, "query": term,
                                               "location": "United States", "start": offset}, api)
                resp.raise_for_status()
                data = resp.json() if api == "v2" else resp.json().get("data") or {}
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
                    apply_url=p.get("canonicalPositionUrl")
                    or f"https://{host}{p.get('positionUrl') or f'/careers/job/{pid}'}",
                    location="; ".join(p.get("standardizedLocations") or p.get("locations") or []),
                    posted_on=_date(p.get("postedTs") or p.get("t_create")), source="eightfold")
            relevant = sum(classify.is_entry_level(p.get("name") or "") for p in positions)
            offset += _PAGE
            if not positions or offset >= (data.get("count") or 0) or relevant < min_relevant:
                break
            time.sleep(delay)
    return list(seen.values()), complete


def fetch_description(job: dict, timeout: int = 30) -> str:
    """Plain-ish text of one position (HTML stripped by the caller)."""
    v2 = re.search(r"https?://(explore\.jobs\.([a-z0-9-]+)\.net)/careers/job/(\d+)",
                   job.get("apply_url", ""), re.I)
    if v2:      # v2-API sites (Netflix): the job's own v2 record
        resp = _SESSION.get(f"https://{v2.group(1)}/api/apply/v2/jobs/{v2.group(3)}",
                            timeout=timeout, params={"domain": f"{v2.group(2).lower()}.com"})
        resp.raise_for_status()
        return resp.json().get("job_description") or ""
    m = _URL.search(job.get("apply_url", ""))
    if not m:
        return ""
    tenant, pid = (m.group(1) or m.group(2)).lower(), m.group(3)
    host = m.group(0).split("/")[2]
    resp = _SESSION.get(f"https://{host}/api/pcsx/position_details", timeout=timeout,
                        params={"domain": f"{tenant}.com", "position_id": pid, "hl": "en"})
    resp.raise_for_status()
    return (resp.json().get("data") or {}).get("jobDescription") or ""
