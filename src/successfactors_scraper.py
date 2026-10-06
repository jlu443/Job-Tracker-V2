"""Fetch job postings from SAP SuccessFactors career sites (L3Harris, Qorvo, ...).

These sites (jobs.l3harris.com, careers.qorvo.com) serve server-rendered
search results at

    GET https://{host}/search/?q={term}&startrow={offset}

in one of two layouts: a classic table (<tr class="data-row">, 25 per page,
"Results 1 – 25 of <b>209</b>") or a tile grid (<li class="job-tile ...">,
100 per page, aria-rowcount="949"). Both carry the title, the job id in a
/job/{slug}/{id}/ link, and the location; neither shows a post date. Job
pages carry the date (itemprop="datePosted") and the description, which
enrichment reads (see enrich.py), as it does for iCIMS.

The JSON search service (/services/recruiting/v1/jobs) answers 401 even with
the page's session and CSRF token, and the RSS feed ignores the keyword and
dates every item "today", so neither is used (checked 2026-10-03).

Search matches descriptions and isn't relevance-ranked (entry-level titles
were spread evenly across L3Harris's pages), and a multi-word term matches
either word ("early career" found 2,669 of Cintas's 2,826 jobs; quoting
the phrase returns nothing on tile sites). So a site whose whole listing
fits in `successfactors_full_sweep_max_pages` is read in full (q=""): that
took fewer requests across all 76 sites than the keyword terms, which hit
the page cap on 11 of them every run and so never counted as complete
(measured 2026-10-06). Larger sites fall back to the keyword terms.
"""

from __future__ import annotations

import html
import logging
import os
import re
import time
from datetime import datetime
from functools import lru_cache

import requests

from . import http_pool
from .config import CONFIG_DIR, load_yaml
from .posting import JobPosting

log = logging.getLogger(__name__)

_SESSION = http_pool.make_session({"User-Agent": "Mozilla/5.0 (job-tracker)"})
# Measured 2026-10-03 on L3Harris, Westinghouse and Supermicro: each of these
# finds entry-level titles the others don't; "graduate", "internship" and
# "university" found none the others missed ("graduate" matches every
# degree requirement).
_TERMS = ("intern", "co-op", "entry level", "new grad", "early career", "student")

_CHUNK = re.compile(r'<tr class="data-row|<li class="job-tile')
# Groups: job path (the full /job/{slug}/{id}/ is needed; /job/{id}/ errors),
# job id, title. Multi-brand sites prefix the path with the brand
# (Mohawk: /DalTile/job/...).
_PATH = r'((?:/[^"/]+)?/job/[^"/]+/(\d{6,})/)'
_LINK = re.compile(r'<a\b[^>]*?href="' + _PATH + r'"[^>]*>\s*([^<]+?)\s*</a>')
_LINK_CLASSED = re.compile(r'<a\b(?=[^>]*class="jobTitle-link)[^>]*?href="' + _PATH
                           + r'"[^>]*>\s*([^<]+?)\s*</a>')
_LOC_TABLE = re.compile(r'<span class="jobLocation">\s*([^<]+?)\s*</span>')
_LOC_TILE = re.compile(r'id="job-\d+-desktop-section-location-value"[^>]*>\s*([^<]+?)\s*<')
_TOTAL = re.compile(r'aria-rowcount="(\d+)"|of\s*<b>\s*([\d,]+)\s*</b>')
_DATE = re.compile(r'itemprop="datePosted"[^>]*content="([^"]+)"')
_DESC = re.compile(r'class="jobdescription"[^>]*>(.*?)</span>\s*</div>', re.S)
_TAG = re.compile(r"<[^>]+>")
_JOB_URL = re.compile(r"https?://([^/]+)(?:/[^/?#]+)?/job/[^/?#]+/(\d{6,})/?")


def site_key(host: str) -> str:
    """'jobs.l3harris.com' -> 'l3harris'; 'mhicareers.com' -> 'mhicareers'."""
    labels = host.lower().split(".")
    if len(labels) >= 3 and labels[-2] in ("jobs2web", "successfactors"):
        return labels[0]            # shared SAP domains: assaabloy.jobs2web.com
    return labels[-2] if len(labels) >= 2 else labels[0]


def job_id_for(host: str, req_id: str) -> str:
    return f"sf_{site_key(host)}_{req_id}"


@lru_cache(maxsize=1)
def _configured_hosts() -> frozenset[str]:
    path = os.path.join(CONFIG_DIR, "successfactors.yaml")
    return frozenset(c["host"].lower() for c in load_yaml(path).get("companies", []) or [])


def canonical_id(url: str) -> str | None:
    """Only for configured hosts: /job/{slug}/{digits}/ alone is too generic."""
    m = _JOB_URL.search(url or "")
    if m and m.group(1).lower() in _configured_hosts():
        return job_id_for(m.group(1), m.group(2))
    return None


def parse_search(page: str) -> tuple[list[tuple[str, str, str, str]], int]:
    """([(job id, job path, title, location)], total results) from one page."""
    rows, seen = [], set()
    chunks = _CHUNK.split(page)[1:]
    for chunk in chunks:
        m = _LINK_CLASSED.search(chunk) or _LINK.search(chunk)
        if not m or m.group(2) in seen:
            continue
        seen.add(m.group(2))
        loc = _LOC_TABLE.search(chunk) or _LOC_TILE.search(chunk)
        rows.append((m.group(2), m.group(1), html.unescape(m.group(3)).strip(),
                     html.unescape(loc.group(1)).strip() if loc else ""))
    t = _TOTAL.search(page)
    total = int((t.group(1) or t.group(2)).replace(",", "")) if t else len(rows)
    return rows, total


def _search(host: str, name: str, term: str, max_pages: int, seen: dict,
            settings: dict) -> bool:
    """Page through one search into `seen`; False if it failed or was cut off."""
    timeout = settings.get("request_timeout", 30)
    delay = settings.get("delay_between_requests", 0.5)
    offset = 0
    for _ in range(max_pages):
        try:
            resp = _SESSION.get(f"https://{host}/search/", timeout=timeout,
                                params={"q": term, "startrow": offset})
            resp.raise_for_status()
        except requests.RequestException as exc:
            log.warning(f"  ! {name} term={term!r} startrow={offset}: {exc}")
            return False
        rows, total = parse_search(resp.text)
        added = 0
        for req_id, path, title, location in rows:
            jid = job_id_for(host, req_id)
            if jid not in seen:
                added += 1
                seen[jid] = JobPosting(
                    job_id=jid, company=name, title=title,
                    apply_url=f"https://{host}{path}",
                    location=location, posted_on="", source="successfactors")
        offset += len(rows)
        # Some sites repeat their last page for any startrow past the end.
        if not rows or offset >= total or (offset > len(rows) and not added):
            return True
        time.sleep(delay)
    return False        # hit max_pages: there were more results


def listing_pages(host: str, settings: dict) -> int | None:
    """Pages in the site's full listing, or None if it can't be read."""
    try:
        resp = _SESSION.get(f"https://{host}/search/", params={"q": "", "startrow": 0},
                            timeout=settings.get("request_timeout", 30))
        resp.raise_for_status()
    except requests.RequestException:
        return None
    rows, total = parse_search(resp.text)
    return -(-total // len(rows)) if rows else None


def fetch_company_jobs(company: dict, settings: dict) -> tuple[list[JobPosting], bool]:
    host = company["host"]
    name = company.get("name", host)
    seen: dict[str, JobPosting] = {}
    pages = None if company.get("search_terms") else listing_pages(host, settings)
    if pages is not None and pages <= settings.get("successfactors_full_sweep_max_pages", 300):
        complete = _search(host, name, "", pages + 1, seen, settings)
    else:
        max_pages = settings.get("max_pages_per_term", 25)
        complete = True
        for term in company.get("search_terms", _TERMS):
            complete &= _search(host, name, term, max_pages, seen, settings)
    return list(seen.values()), complete


def fetch_detail(url: str, timeout: int = 30) -> tuple[str, str]:
    """(description text, ISO post date or '') from a job page."""
    resp = _SESSION.get(url, timeout=timeout)
    resp.raise_for_status()
    page = resp.text
    desc = _DESC.search(page)
    text = html.unescape(_TAG.sub(" ", desc.group(1))) if desc else ""
    posted = ""
    d = _DATE.search(page)
    if d:
        try:
            posted = datetime.strptime(d.group(1), "%a %b %d %H:%M:%S %Z %Y").date().isoformat()
        except ValueError:
            pass
    return text, posted
