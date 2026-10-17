"""Fetch postings from Paylocity recruiting sites.

Each company's job list page embeds its jobs as JSON:

    GET https://recruiting.paylocity.com/Recruiting/Jobs/All/{company guid}
    <script> window.pageData = {"Jobs": [{"JobId", "JobTitle", "PublishedDate",
             "JobLocation": {"City", "State", "Country"}, "IsRemote", ...}], ...};

Job ids are global (/Recruiting/Jobs/Details/{JobId}), which is how curated
lists cite them; a job page names its company's guid. The job page carries
the description, which enrichment reads. 25 curated intern/new-grad
listings were on Paylocity, 2026-10-17.
"""

from __future__ import annotations

import html
import json
import logging
import re
import time

import requests

from . import http_pool
from .posting import JobPosting

log = logging.getLogger(__name__)

_SESSION = http_pool.make_session({"User-Agent": "Mozilla/5.0 (job-tracker)"})
_BASE = "https://recruiting.paylocity.com/Recruiting/Jobs"
_URL = re.compile(r"recruiting\.paylocity\.com/Recruiting/Jobs/(?:Details|Apply)/(\d+)", re.I)
_GUID = re.compile(r"/Recruiting/Jobs/All/([0-9a-f-]{36})", re.I)
_PAGE_DATA = re.compile(r"window\.pageData\s*=\s*(\{.*?\});\s*</script>", re.S)


def job_id_for(job_id) -> str:
    return f"pl_{job_id}"


def canonical_id(url: str) -> str | None:
    m = _URL.search(url or "")
    return job_id_for(m.group(1)) if m else None


def company_guid(job_url: str, timeout: int = 30) -> str | None:
    """The company guid a job page links back to (for discovery)."""
    resp = _get(job_url, timeout)
    m = _GUID.search(resp.text) if resp.ok else None
    return m.group(1).lower() if m else None


def _page_data(page: str) -> dict:
    m = _PAGE_DATA.search(page)
    try:
        return json.loads(m.group(1)) if m else {}
    except ValueError:
        return {}


def _location(j: dict) -> str:
    loc = j.get("JobLocation") or {}
    text = ", ".join(p for p in (loc.get("City"), loc.get("State"), loc.get("Country")) if p) \
        or j.get("LocationName") or ""
    return f"Remote; {text}" if j.get("IsRemote") and text else ("Remote" if j.get("IsRemote")
                                                                 else text)


def _get(url: str, timeout: int) -> requests.Response:
    """One shared host that answers bursts with 429 (eight quick requests
    did it, 2026-10-17): wait it out a few times before giving up."""
    for attempt in range(4):
        resp = _SESSION.get(url, timeout=timeout)
        if resp.status_code != 429 or attempt == 3:
            return resp
        wait = resp.headers.get("Retry-After", "")
        time.sleep(min(int(wait), 60) if wait.isdigit() else 5 * 2 ** attempt)
    return resp


def fetch_company_jobs(company: dict, settings: dict) -> tuple[list[JobPosting], bool]:
    guid = company["guid"]
    try:
        resp = _get(f"{_BASE}/All/{guid}", settings.get("request_timeout", 30))
        if resp.status_code == 404:
            log.warning(f"  ! {company.get('name', guid)}: no Paylocity site (404)")
            return [], True
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning(f"  ! {company.get('name', guid)}: {exc}")
        return [], False
    data = _page_data(resp.text)
    if "Jobs" not in data:
        log.warning(f"  ! {company.get('name', guid)}: no job data on the page")
        return [], False
    name = company.get("name") or data.get("ModuleTitle") or guid
    return [JobPosting(job_id=job_id_for(j["JobId"]), company=name,
                       title=" ".join((j.get("JobTitle") or "").split()),
                       apply_url=f"{_BASE}/Details/{j['JobId']}", location=_location(j),
                       posted_on=(j.get("PublishedDate") or "")[:10], source="paylocity")
            for j in data["Jobs"] if j.get("JobId") and j.get("JobTitle")
            and not j.get("IsInternal")], True


def fetch_description(url: str, timeout: int = 30) -> str:
    resp = _get(url, timeout)
    resp.raise_for_status()
    m = re.search(r'class="job-preview-details"[^>]*>(.*?)</div>\s*</div>', resp.text, re.S) \
        or re.search(r"<main\b.*?</main>", resp.text, re.S)
    text = m.group(1) if m and m.lastindex else (m.group(0) if m else "")
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text)).split())
