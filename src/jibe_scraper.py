"""Fetch postings from Jibe career sites (iCIMS "Attract"; careers.X.com).

Many large employers (AMD, Johns Hopkins APL, Garmin, ...) run their own
careers domain on Jibe, whose pages call a public JSON endpoint:

    GET https://{host}/api/jobs?keywords={term}&page={n}&limit=100

Job links look like https://{host}/jobs/{id}?icims=1, which is also how
curated lists cite them (and how discovery finds new hosts).
"""

from __future__ import annotations

import logging
import time

import requests

from . import enrich, http_pool
from .posting import JobPosting

log = logging.getLogger(__name__)

_SESSION = http_pool.make_session({"User-Agent": "Mozilla/5.0 (job-tracker)",
                                   "Accept": "application/json"})
_TERMS = ("intern", "co-op", "new grad", "university", "entry level")


def job_id_for(host: str, job_id: str) -> str:
    return f"jibe_{host.lower()}_{job_id}"


def fetch_company_jobs(company: dict, settings: dict) -> tuple[list[JobPosting], bool]:
    host = company["host"]
    name = company.get("name", host)
    timeout = settings.get("request_timeout", 30)
    seen: dict[str, JobPosting] = {}
    complete = True
    for term in company.get("search_terms", _TERMS):
        for page in range(1, 21):
            try:
                resp = _SESSION.get(f"https://{host}/api/jobs", timeout=timeout,
                                    params={"keywords": term, "page": page, "limit": 100})
                if resp.status_code == 404:
                    log.warning(f"  ! {name}: no Jibe API at {host} (404)")
                    return [], True
                resp.raise_for_status()
                data = resp.json()
            except (requests.RequestException, ValueError) as exc:
                log.warning(f"  ! {name} term={term!r} page={page}: {exc}")
                complete = False
                break
            jobs = data.get("jobs") or []
            for item in jobs:
                j = item.get("data") or {}
                slug = str(j.get("slug") or j.get("req_id") or "")
                jid = job_id_for(host, slug)
                if not slug or jid in seen:
                    continue
                title = (j.get("title") or "").strip()
                description = f"{j.get('description') or ''}\n{j.get('qualifications') or ''}"
                seen[jid] = JobPosting(
                    job_id=jid, company=name, title=title,
                    apply_url=f"https://{host}/jobs/{slug}?icims=1",
                    location=j.get("full_location") or "",
                    posted_on=(j.get("posted_date") or "")[:10], source="jibe",
                    # The underlying ATS posting, when Jibe exposes it.
                    direct_url=j.get("apply_url") or "",
                    **enrich.described_fields(title, enrich._strip_html(description)))
            if len(jobs) < 100 or page * 100 >= (data.get("totalCount") or 0):
                break
            time.sleep(settings.get("delay_between_requests", 0.5))
    return list(seen.values()), complete
