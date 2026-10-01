"""Fetch job postings from iCIMS career portals ({portal}.icims.com).

iCIMS has no public JSON API, but every portal serves a stable server-
rendered listing (the page its iframe embed uses):

    GET https://{host}/jobs/search?ss=1&searchKeyword={term}&in_iframe=1&pr={page}

Each row carries the job id (in the link), title, location
("US-VA-Herndon") and an exact post timestamp. Results are date-ordered,
not relevance-ranked, so paging stops on a page with no entry-level titles.

All portals sit behind one iCIMS edge that answers bursts from an IP with a
"Human Verification" page (HTTP 405) for a minute or two. Requests are
therefore spaced globally, and a challenge triggers one back-off and retry,
then skips iCIMS for the rest of the run (boards stay incomplete, so nothing
is wrongly marked removed).
"""

from __future__ import annotations

import html
import re
import threading
import time
from datetime import datetime

import requests

from . import classify, dates, http_pool
from .posting import JobPosting

# An honest client id. iCIMS's edge challenges a full Chrome user-agent
# coming from a non-browser client, but serves this one normally.
_SESSION = http_pool.make_session({"User-Agent": "Mozilla/5.0 (job-tracker)"})
# "graduate" is useless here: it matches every description with a degree
# requirement.
_TERMS = ("intern", "entry level", "new grad")
_MIN_INTERVAL = 0.4        # seconds between any two iCIMS requests
_CHALLENGE_BACKOFF = 60

_rate_lock = threading.Lock()
_last_request = [0.0]
_blocked = threading.Event()


class _Challenged(Exception):
    pass


def _get(host: str, params: dict, timeout: int, path: str = "/jobs/search") -> requests.Response:
    """Rate-limited GET; one back-off on a bot challenge, then give up."""
    for attempt in (1, 2):
        if _blocked.is_set():
            raise _Challenged()
        with _rate_lock:
            wait = _last_request[0] + _MIN_INTERVAL - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            _last_request[0] = time.monotonic()
        resp = _SESSION.get(f"https://{host}{path}", params=params, timeout=timeout)
        if resp.status_code == 405 and "Human Verification" in resp.text:
            if attempt == 1:
                print(f"  ! iCIMS bot challenge at {host}; backing off {_CHALLENGE_BACKOFF}s")
                time.sleep(_CHALLENGE_BACKOFF)
                continue
            _blocked.set()
            print("  ! iCIMS still challenging; skipping remaining iCIMS portals this run")
            raise _Challenged()
        return resp
    raise _Challenged()

# Each listing row is its own container; the post date sits *before* the
# title link, so rows must be parsed as units, not by reading after a title.
_ROW_SPLIT = re.compile(r'<(?:li|div) class="row"[^>]*>')
_ANCHOR = re.compile(r'href="https?://[^"/]+/jobs/(\d+)/[^"]*"[^>]*class="iCIMS_Anchor"')
_TITLE = re.compile(r'<h3[^>]*>(.*?)</h3>', re.S)
# "Job Location(s)" label, then the value — directly in a <span>, or in a
# <dd> after the label's <dt>, depending on the portal's template.
_LOCATION = re.compile(r'Locations?\s*</span>\s*(?:</dt>\s*<dd[^>]*>)?\s*<span[^>]*>\s*([^<]+?)\s*</span>')
_POSTED = re.compile(r'title="(\d{1,2}/\d{1,2}/\d{4})')
_TAG = re.compile(r"<[^>]+>")


def fetch_description(host: str, job_id: str, timeout: int = 30) -> str:
    """Plain text of one job page (for enrichment), through the rate limiter."""
    try:
        resp = _get(host, {"in_iframe": 1}, timeout, path=f"/jobs/{job_id}/job")
    except _Challenged:
        return ""
    resp.raise_for_status()
    body = resp.text
    start = body.find("iCIMS_JobContent")
    return html.unescape(_TAG.sub(" ", body[start:] if start >= 0 else body))


def job_id_for(host: str, job_id: str) -> str:
    return f"icims_{host.split('.')[0].lower()}_{job_id}"


def _one_location(loc: str) -> str:
    parts = [p.strip() for p in loc.split("-", 2)]
    if len(parts) == 3 and re.fullmatch(r"[A-Z]{2}", parts[0]):
        country, region, city = parts
        return ", ".join(p for p in (city, region, country) if p)
    return loc.strip()


def normalize_location(loc: str) -> str:
    """'US-VA-Herndon' → 'Herndon, VA, US'; 'A | B' lists → 'A'; 'B'."""
    return "; ".join(_one_location(p) for p in loc.split("|") if p.strip())


def parse_listing(page: str, host: str, name: str) -> list[JobPosting]:
    out = []
    for row in _ROW_SPLIT.split(page):
        anchor = _ANCHOR.search(row)
        title = _TITLE.search(row)
        if not anchor or not title:
            continue
        loc = _LOCATION.search(row)
        posted = _POSTED.search(row)
        iso = ""
        if posted:
            try:
                iso = datetime.strptime(posted.group(1), "%m/%d/%Y").date().isoformat()
            except ValueError:
                pass
        jid = anchor.group(1)
        out.append(JobPosting(
            job_id=job_id_for(host, jid), company=name,
            title=html.unescape(_TAG.sub("", title.group(1))),
            apply_url=f"https://{host}/jobs/{jid}/job",
            location=normalize_location(html.unescape(loc.group(1))) if loc else "",
            posted_on=iso, source="icims"))
    return out


def _crawl(host: str, name: str, keyword: str, max_pages: int, timeout: int,
           seen: dict, stop) -> str:
    """Page one listing ('' = all jobs, newest first) until stop(rows).
    Returns 'ok', 'missing' (404), 'failed' or 'challenged'."""
    previous: list[str] = []
    params = {"ss": 1, "in_iframe": 1}
    if keyword:
        params["searchKeyword"] = keyword
    for page_no in range(max_pages):
        try:
            resp = _get(host, {**params, "pr": page_no}, timeout)
            if resp.status_code == 404:
                print(f"  ! {name}: portal not found (404)")
                return "missing"
            resp.raise_for_status()
        except _Challenged:
            return "challenged"
        except requests.RequestException as exc:
            print(f"  ! {name} keyword={keyword!r} page={page_no}: {exc}")
            return "failed"
        rows = parse_listing(resp.text, host, name)
        ids = [p.job_id for p in rows]
        if not rows or ids == previous:     # past the last page, iCIMS repeats it
            return "ok"
        previous = ids
        for p in rows:
            seen.setdefault(p.job_id, p)
        if stop(rows):
            return "ok"
    return "ok"


def fetch_company_jobs(company: dict, settings: dict) -> tuple[list[JobPosting], bool | None]:
    """One iCIMS portal. company["_mode"] (set by main.py): 'recent' = newest
    listings until a page has nothing from the last window_days (cheap, every
    run); 'sweep' = the keyword searches (complete; default); 'recent+sweep'.
    Returns (postings, complete) with complete None for recent-only."""
    host = company["host"]
    name = company.get("name", host)
    timeout = settings.get("request_timeout", 30)
    mode = company.get("_mode", "sweep")
    seen: dict[str, JobPosting] = {}

    if mode.startswith("recent"):
        cfg = (settings.get("recency_check") or {}).get("icims") or {}
        window = cfg.get("window_days", 2)
        recent = lambda rows: not any(
            (a := dates.age_days(r.posted_on)) is not None and a <= window for r in rows)
        status = _crawl(host, name, "", cfg.get("max_pages", 5), timeout, seen, recent)
        if status == "missing":
            return [], True
        if status != "ok" or not mode.endswith("sweep"):
            return list(seen.values()), (None if status == "ok" else False)

    max_pages = min(settings.get("max_pages_per_term", 25), 10)
    not_entry = lambda rows: not any(classify.is_entry_level(p.title) for p in rows)
    complete = True
    for term in company.get("search_terms", _TERMS):
        status = _crawl(host, name, term, max_pages, timeout, seen, not_entry)
        if status == "missing":
            return [], True
        if status == "challenged":
            return list(seen.values()), False
        if status == "failed":
            complete = False
    return list(seen.values()), complete
