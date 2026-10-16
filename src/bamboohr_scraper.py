"""Fetch postings from BambooHR career sites ({slug}.bamboohr.com/careers).

Each site lists its open jobs through a public JSON endpoint:

    GET https://{slug}.bamboohr.com/careers/list
    GET https://{slug}.bamboohr.com/careers/{id}/detail   (description, datePosted)

The list carries title, department, city/state and employment status
("Co-op", "Internship", "Full-Time"); the detail page carries the post date
and description, which enrichment reads (see enrich.py). Small companies use
it: 28 curated intern/new-grad listings on 14 sites were out of reach before
this scraper (2026-10-16).
"""

from __future__ import annotations

import logging
import re

import requests

from . import classify, http_pool
from .posting import JobPosting

log = logging.getLogger(__name__)

_SESSION = http_pool.make_session({"User-Agent": "Mozilla/5.0 (job-tracker)",
                                   "Accept": "application/json"})

# https://acme.bamboohr.com/careers/121, .../careers/121/detail,
# .../jobs/view.php?id=121 (older links)
_URL = re.compile(r"https?://([a-z0-9-]+)\.bamboohr\.com/(?:careers/(\d+)|jobs/view\.php\?id=(\d+))",
                  re.I)


def job_id_for(slug: str, job_id) -> str:
    return f"bhr_{slug.lower()}_{job_id}"


def canonical_id(url: str) -> str | None:
    m = _URL.search(url or "")
    return job_id_for(m.group(1), m.group(2) or m.group(3)) if m else None


def _location(j: dict) -> str:
    loc, ats = j.get("location") or {}, j.get("atsLocation") or {}
    parts = [loc.get("city") or ats.get("city"), loc.get("state") or ats.get("state")
             or ats.get("province"), loc.get("addressCountry") or ats.get("country")]
    text = ", ".join(p for p in parts if p)
    if j.get("isRemote") or str(j.get("locationType")) == "1":
        text = f"Remote; {text}" if text else "Remote"
    return text


def fetch_company_jobs(company: dict, settings: dict) -> tuple[list[JobPosting], bool]:
    slug = company["slug"]
    name = company.get("name", slug)
    try:
        resp = _SESSION.get(f"https://{slug}.bamboohr.com/careers/list",
                            timeout=settings.get("request_timeout", 30))
        if resp.status_code == 404:
            log.warning(f"  ! {name}: no BambooHR careers site '{slug}' (404)")
            return [], True
        resp.raise_for_status()
        items = resp.json().get("result") or []
    except (requests.RequestException, ValueError) as exc:
        log.warning(f"  ! {name}: {exc}")
        return [], False
    out = []
    for j in items:
        jid = j.get("id")
        title = (j.get("jobOpeningName") or "").strip()
        if not jid or not title:
            continue
        # An "Internship" / "Co-op" employment status counts when the title is
        # plain; "Full-Time" says nothing about level.
        status = classify.hint_from_level(j.get("employmentStatusLabel") or "")
        out.append(JobPosting(
            job_id=job_id_for(slug, jid), company=name, title=title,
            apply_url=f"https://{slug}.bamboohr.com/careers/{jid}",
            location=_location(j), posted_on="", source="bamboohr",
            role_hint="intern" if status == "intern" else ""))
    return out, True


def fetch_detail(url: str, timeout: int) -> tuple[str, str]:
    """(description HTML, post date) for a job page URL; ('', '') if unreadable."""
    m = _URL.search(url or "")
    if not m:
        return "", ""
    resp = _SESSION.get(f"https://{m.group(1)}.bamboohr.com/careers/"
                        f"{m.group(2) or m.group(3)}/detail", timeout=timeout)
    resp.raise_for_status()
    job = ((resp.json().get("result") or {}).get("jobOpening") or {})
    return job.get("description") or "", (job.get("datePosted") or "")[:10]
