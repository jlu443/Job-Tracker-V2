"""Fetch postings from Jobvite career sites (jobs.jobvite.com/{slug}).

No public JSON API; each site's list page is a server-rendered table:

    GET https://jobs.jobvite.com/{slug}/jobs?p={page}

    <td class="jv-job-list-name"><a href="/{slug}/job/{id}">Title</a></td>
    <td class="jv-job-list-location"> Plano, Texas </td>   (or "2 Locations")

The job page carries the description, which enrichment reads. 28 curated
intern/new-grad listings were on Jobvite sites (MTA, Tyler Technologies,
SRC, Nutanix, ...), 2026-10-17.
"""

from __future__ import annotations

import html
import logging
import re
import time

import requests

from . import http_pool
from .posting import JobPosting

log = logging.getLogger(__name__)

_SESSION = http_pool.make_session({"User-Agent": "Mozilla/5.0 (job-tracker)"})
_URL = re.compile(r"jobs\.jobvite\.com/([\w-]+)/job/([A-Za-z0-9]{6,})", re.I)
_ROW = re.compile(r'<td class="jv-job-list-name">\s*<a href="/([\w-]+)/job/([A-Za-z0-9]+)"[^>]*>(.*?)</a>'
                  r'.*?<td class="jv-job-list-location">(.*?)</td>', re.S)
_DESCRIPTION = re.compile(r'class="jv-job-detail-description"[^>]*>(.*?)</div>\s*(?:<div|</div>)',
                          re.S)


def job_id_for(slug: str, job_id: str) -> str:
    return f"jv_{slug.lower()}_{job_id}"


def canonical_id(url: str) -> str | None:
    m = _URL.search(url or "")
    return job_id_for(m.group(1), m.group(2)) if m else None


def _text(fragment: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment or "")).split())


def parse_list(page: str, name: str) -> list[JobPosting]:
    return [JobPosting(job_id=job_id_for(slug, jid), company=name, title=_text(title),
                       apply_url=f"https://jobs.jobvite.com/{slug}/job/{jid}",
                       location=_text(place), posted_on="", source="jobvite")
            for slug, jid, title, place in _ROW.findall(page) if _text(title)]


def fetch_company_jobs(company: dict, settings: dict) -> tuple[list[JobPosting], bool]:
    slug = company["slug"]
    name = company.get("name", slug)
    timeout = settings.get("request_timeout", 30)
    seen: dict[str, JobPosting] = {}
    for page in range(0, 30):
        try:
            resp = _SESSION.get(f"https://jobs.jobvite.com/{slug}/jobs", timeout=timeout,
                                params={"p": page} if page else None)
            if resp.status_code == 404:
                log.warning(f"  ! {name}: no Jobvite site '{slug}' (404)")
                return [], True
            resp.raise_for_status()
        except requests.RequestException as exc:
            log.warning(f"  ! {name} page {page}: {exc}")
            return list(seen.values()), False
        new = [j for j in parse_list(resp.text, name) if j.job_id not in seen]
        seen.update((j.job_id, j) for j in new)
        if not new or f"p={page + 1}" not in resp.text:
            break
        time.sleep(settings.get("delay_between_requests", 0.5))
    return list(seen.values()), True


def fetch_description(url: str, timeout: int = 30) -> str:
    resp = _SESSION.get(url, timeout=timeout)
    resp.raise_for_status()
    m = _DESCRIPTION.search(resp.text)
    return _text(m.group(1)) if m else ""
