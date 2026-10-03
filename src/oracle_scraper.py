"""Fetch job postings from Oracle Recruiting Cloud (Oracle HCM) career sites.

Career sites at https://{host}.fa.{region}.oraclecloud.com/hcmUI/CandidateExperience/
are backed by a public REST endpoint:

    GET https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions
        ?onlyData=true&expand=requisitionList
        &finder=findReqs;siteNumber={site},keyword="{term}",limit=..,offset=..,sortBy=RELEVANCY

Keyword search is fuzzy ("intern" also hits "Internal Audit") but relevance-
ranked, so paging stops once a page has run out of entry-level titles.
"""

from __future__ import annotations

import logging
import time
from urllib.parse import quote

import requests

from . import classify, http_pool
from .posting import JobPosting

log = logging.getLogger(__name__)

_SESSION = http_pool.make_session({"User-Agent": "Mozilla/5.0 (job-tracker)",
                                   "Accept": "application/json"})
_PAGE = 25
# Single words: quoted multi-word phrases ("new grad") match almost nothing.
_TERMS = ("intern", "internship", "co-op", "graduate", "university", "entry")


def job_id_for(host: str, req_id: str) -> str:
    return f"orc_{host.split('.')[0].lower()}_{req_id}"


def apply_url(host: str, site: str, req_id: str) -> str:
    return f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{req_id}"


def _search_url(host: str, site: str, term: str, offset: int) -> str:
    finder = (f"findReqs;siteNumber={site},keyword=\"{term}\",limit={_PAGE},"
              f"offset={offset},sortBy=RELEVANCY")
    return (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
            f"?onlyData=true&expand=requisitionList&finder={quote(finder, safe=';=,')}")


def fetch_company_jobs(company: dict, settings: dict) -> tuple[list[JobPosting], bool]:
    host, site = company["host"], company["site"]
    name = company.get("name", host)
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
                resp = _SESSION.get(_search_url(host, site, term, offset), timeout=timeout)
                if resp.status_code == 404:
                    log.warning(f"  ! {name}: site '{site}' not found (404)")
                    return [], True
                resp.raise_for_status()
                items = resp.json().get("items") or [{}]
            except (requests.RequestException, ValueError) as exc:
                log.warning(f"  ! {name} term={term!r} offset={offset}: {exc}")
                complete = False
                break
            reqs = items[0].get("requisitionList") or []
            total = items[0].get("TotalJobsCount") or 0
            for r in reqs:
                rid = str(r.get("Id") or "")
                if not rid:
                    continue
                jid = job_id_for(host, rid)
                if jid in seen:
                    continue
                seen[jid] = JobPosting(
                    job_id=jid, company=name, title=r.get("Title") or "",
                    apply_url=apply_url(host, site, rid),
                    location=r.get("PrimaryLocation") or "",
                    posted_on=(r.get("PostedDate") or "")[:10], source="oracle")
            relevant = sum(classify.is_entry_level(r.get("Title") or "") for r in reqs)
            offset += _PAGE
            if not reqs or offset >= total or relevant < min_relevant:
                break
            time.sleep(delay)
    return list(seen.values()), complete
