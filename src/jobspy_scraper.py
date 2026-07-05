"""Scrape job boards (LinkedIn, Indeed, Glassdoor, ZipRecruiter) via JobSpy.

Supplements the Workday scraper with broader coverage. Each posting is
normalized into the same JobPosting shape used by the Workday scraper so the
rest of the pipeline (classify, db.sync, notify) is unchanged.

Note: LinkedIn blocks requests from datacenter IPs (GitHub Actions runners).
Indeed, Glassdoor, and ZipRecruiter work fine in CI.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass

# Import here so the rest of the app works without jobspy installed.
try:
    from jobspy import scrape_jobs as _scrape
    _JOBSPY_AVAILABLE = True
except ImportError:
    _JOBSPY_AVAILABLE = False


@dataclass(frozen=True)
class JobPosting:
    job_id: str
    company: str
    title: str
    apply_url: str
    location: str
    posted_on: str
    source: str


def _make_id(source: str, url: str) -> str:
    """Stable ID from source + URL since external boards have no universal ID."""
    return f"{source}_{hashlib.sha1(url.encode()).hexdigest()[:12]}"


def fetch_jobs(settings: dict) -> list[JobPosting]:
    """Search all configured job boards and return normalized postings."""
    if not _JOBSPY_AVAILABLE:
        print("  jobspy not installed — skipping external job boards.")
        return []

    board_settings = settings.get("jobspy", {})
    if not board_settings.get("enabled", True):
        return []

    sites = board_settings.get("sites", ["indeed"])
    search_terms = board_settings.get("search_terms", settings.get("search_terms", []))
    location = board_settings.get("location", "United States")
    results_per_term = board_settings.get("results_per_term", 50)
    hours_old = board_settings.get("hours_old", 72)

    # Residential proxy — required for Glassdoor/ZipRecruiter from datacenter IPs.
    # Format: http://user:pass@host:port  or  socks5://user:pass@host:port
    proxy = os.environ.get("JOBSPY_PROXY")
    proxies = {"http": proxy, "https": proxy} if proxy else None
    if proxy:
        print(f"  [jobspy] Using proxy: {proxy.split('@')[-1]}")  # hide credentials

    seen: dict[str, JobPosting] = {}

    for term in search_terms:
        print(f"  [jobspy] Searching '{term}' on {sites} ...")
        try:
            df = _scrape(
                site_name=sites,
                search_term=term,
                location=location,
                results_wanted=results_per_term,
                hours_old=hours_old,
                country_indeed="USA",
                proxies=proxies,
                verbose=0,
            )
        except Exception as exc:
            print(f"  [jobspy] term={term!r} failed: {exc}")
            continue

        if df is None or df.empty:
            continue

        for _, row in df.iterrows():
            url = str(row.get("job_url") or row.get("apply_url") or "").strip()
            if not url:
                continue
            source = str(row.get("site", "jobspy")).strip()
            job_id = _make_id(source, url)
            if job_id in seen:
                continue

            company = str(row.get("company") or "").strip()
            title = str(row.get("title") or "").strip()
            location_str = str(row.get("location") or "").strip()
            posted = str(row.get("date_posted") or "").strip()

            seen[job_id] = JobPosting(
                job_id=job_id,
                company=company,
                title=title,
                apply_url=url,
                location=location_str,
                posted_on=posted,
                source=source,
            )

    print(f"  [jobspy] {len(seen)} unique postings across all boards.")
    return list(seen.values())
