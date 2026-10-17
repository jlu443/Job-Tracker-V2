"""Fetch postings from Oracle Taleo career sections ({tenant}.taleo.net).

A career section's search page names its portal id; its REST search then
answers JSON:

    GET  https://{host}/careersection/{section}/jobsearch.ftl?lang=en   (portal=NNN)
    POST https://{host}/careersection/rest/jobboard/searchjobs?lang=en&portal={portal}
         {"fieldData": {"fields": {"KEYWORD": term}}, "pageNo": n, ...}
    -> {"requisitionList": [{"contestNo", "column": [title, '["US-MD-City"]', "10/13/2026"]}],
        "pagingData": {"totalCount", "pageSize"}}

Curated lists cite jobdetail.ftl?job={contestNo}. 11 curated intern/new-grad
listings were on Taleo (Textron, Energy Transfer), 2026-10-17.
"""

from __future__ import annotations

import html
import json
import logging
import re
import time
import urllib.parse
from datetime import datetime

import requests

from . import http_pool
from .posting import JobPosting

log = logging.getLogger(__name__)

_SESSION = http_pool.make_session({"User-Agent": "Mozilla/5.0 (job-tracker)"})
_TERMS = ("intern", "co-op", "graduate", "entry level", "new grad")
_URL = re.compile(r"https?://([\w-]+)\.taleo\.net/careersection/[\w-]+/jobdetail\.ftl\?(?:[^#]*&)?job=(\w+)",
                  re.I)
_PORTAL = re.compile(r'portal[=:"\s]+(\d{6,})')


def job_id_for(host: str, contest_no: str) -> str:
    return f"tl_{host.split('.')[0].lower()}_{contest_no}"


def canonical_id(url: str) -> str | None:
    m = _URL.search(url or "")
    return f"tl_{m.group(1).lower()}_{m.group(2)}" if m else None


def _date(text: str) -> str:
    try:
        return datetime.strptime(text.strip(), "%m/%d/%Y").date().isoformat()
    except (ValueError, AttributeError):
        return ""


def _locations(text: str) -> str:
    try:
        places = json.loads(text)
    except (ValueError, TypeError):
        places = [text]
    # "US-Maryland-Hunt Valley" -> "Hunt Valley, Maryland, US"
    return "; ".join(", ".join(reversed(p.split("-"))) if p.count("-") >= 2 else p
                     for p in places if p)


def _body(term: str, page: int) -> dict:
    return {"multilineEnabled": False,
            "sortingSelection": {"sortBySelectionParam": "3", "ascendingSortingOrder": "false"},
            "fieldData": {"fields": {"KEYWORD": term, "LOCATION": "", "CATEGORY": ""},
                          "valid": True},
            "filterSelectionParam": {"searchFilterSelections": []},
            "advancedSearchFiltersSelectionParam": {"searchFilterSelections": []},
            "pageNo": page}


def fetch_company_jobs(company: dict, settings: dict) -> tuple[list[JobPosting], bool]:
    host, section = company["host"], company["section"]
    name = company.get("name", host)
    timeout = settings.get("request_timeout", 30)
    try:
        page = _SESSION.get(f"https://{host}/careersection/{section}/jobsearch.ftl",
                            params={"lang": "en"}, timeout=timeout)
        page.raise_for_status()
    except requests.RequestException as exc:
        log.warning(f"  ! {name}: {exc}")
        return [], False
    portal = _PORTAL.search(page.text)
    if not portal:
        log.warning(f"  ! {name}: no Taleo portal id on the search page")
        return [], False
    seen: dict[str, JobPosting] = {}
    complete = True
    for term in company.get("search_terms", _TERMS):
        for page_no in range(1, 21):
            try:
                resp = _SESSION.post(f"https://{host}/careersection/rest/jobboard/searchjobs",
                                     params={"lang": "en", "portal": portal.group(1)},
                                     json=_body(term, page_no), timeout=timeout,
                                     # Taleo answers 500 without a time zone
                                     headers={"Content-Type": "application/json",
                                              "tz": "GMT-05:00"})
                resp.raise_for_status()
                data = resp.json()
            except (requests.RequestException, ValueError) as exc:
                log.warning(f"  ! {name} term={term!r} page={page_no}: {exc}")
                complete = False
                break
            reqs = data.get("requisitionList") or []
            for r in reqs:
                cols, contest = r.get("column") or [], r.get("contestNo")
                if not contest or not cols:
                    continue
                jid = job_id_for(host, contest)
                seen.setdefault(jid, JobPosting(
                    job_id=jid, company=name, title=" ".join(html.unescape(cols[0]).split()),
                    apply_url=f"https://{host}/careersection/{section}/jobdetail.ftl?job={contest}",
                    location=_locations(cols[1]) if len(cols) > 1 else "",
                    posted_on=_date(cols[2]) if len(cols) > 2 else "", source="taleo"))
            paging = data.get("pagingData") or {}
            if not reqs or page_no * (paging.get("pageSize") or 25) >= (paging.get("totalCount") or 0):
                break
            time.sleep(settings.get("delay_between_requests", 0.5))
    return list(seen.values()), complete


def fetch_description(url: str, timeout: int = 30) -> str:
    resp = _SESSION.get(url, timeout=timeout)
    resp.raise_for_status()
    # The page's data sits URL-encoded in a hidden "initialHistory" field,
    # fields separated by "!|!"; the description is the longest one.
    m = re.search(r'name="initialHistory"[^>]*value="([^"]*)"', resp.text) \
        or re.search(r'value="([^"]*)"[^>]*name="initialHistory"', resp.text)
    if not m:
        return ""
    parts = [urllib.parse.unquote(html.unescape(p)) for p in m.group(1).split("!|!")]
    text = max(parts, key=len, default="")
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text)).split())
