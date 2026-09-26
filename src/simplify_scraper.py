"""Ingest community-curated intern / new-grad lists (SimplifyJobs format).

These GitHub repos publish a machine-readable listings.json of hand-vetted
early-career postings — the highest-precision source available, and one that
covers ATSes we don't scrape (Oracle HCM, iCIMS, custom career sites).

A listing whose URL points at a supported ATS gets that ATS's job id
(gh_123, wd_nvidia_JR1, ...), so it collapses onto the first-party row
instead of becoming a duplicate; everything else is keyed sim_<listing id>.
"""

from __future__ import annotations

import requests

from . import dates, dedupe, http_pool
from .posting import JobPosting

_SESSION = http_pool.make_session({"User-Agent": "Mozilla/5.0 (job-tracker)"})
_RAW = "https://raw.githubusercontent.com/{repo}/dev/.github/scripts/listings.json"

_CATEGORY = {
    "software": "software", "software engineering": "software",
    "ai/ml/data": "data_ml", "data science, ai & machine learning": "data_ml",
    "hardware": "hardware", "hardware engineering": "hardware",
    "quant": "quant", "quantitative finance": "quant",
    "product": "product", "product management": "product",
}
_SPONSORSHIP = {
    "Offers Sponsorship": "yes",
    "Does Not Offer Sponsorship": "no",
    "U.S. Citizenship is Required": "no",
}


def _to_posting(item: dict, role: str) -> JobPosting | None:
    url = (item.get("url") or "").strip()
    if not url or not item.get("title"):
        return None
    job_id = dedupe.canonical_job_id(url) or f"sim_{item.get('id')}"
    return JobPosting(
        job_id=job_id,
        company=(item.get("company_name") or "").strip(),
        title=item["title"].strip(),
        apply_url=url,
        location="; ".join(item.get("locations") or []),
        posted_on=dates.epoch_to_iso(item.get("date_posted")),
        source="simplify",
        category=_CATEGORY.get((item.get("category") or "").lower(), ""),
        sponsorship=_SPONSORSHIP.get(item.get("sponsorship") or "", ""),
        role_hint=role,
    )


def fetch_jobs(settings: dict) -> tuple[list[JobPosting], bool]:
    """All active listings across the configured repos.

    Returns (postings, complete); complete only when every repo loaded, since
    that's what makes "missing from the list" mean "closed".
    """
    cfg = settings.get("curated_lists", {})
    if not cfg.get("enabled", True):
        return [], False
    timeout = settings.get("request_timeout", 30) * 2

    out: dict[str, JobPosting] = {}
    complete = True
    for entry in cfg.get("repos", []):
        repo, role = entry["repo"], entry["role"]
        try:
            resp = _SESSION.get(_RAW.format(repo=repo), timeout=timeout)
            resp.raise_for_status()
            items = resp.json()
        except (requests.RequestException, ValueError) as exc:
            print(f"  ! {repo}: {exc}")
            complete = False
            continue
        n = 0
        for item in items:
            if not item.get("active") or not item.get("is_visible", True):
                continue
            p = _to_posting(item, role)
            if p and p.job_id not in out:
                out[p.job_id] = p
                n += 1
        print(f"  {repo}: {n} active listings")
    return list(out.values()), complete
