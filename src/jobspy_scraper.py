"""Scrape external job boards (Indeed, LinkedIn, Glassdoor, ZipRecruiter) via JobSpy.

Supplements the first-party ATS scrapers with broader coverage. Each posting
is normalized into the same JobPosting shape so the rest of the pipeline
(classify, db.sync, notify) is unchanged. JobSpy's dataframe includes each
posting's description for free; it's kept for the enrichment pass because
aggregator postings can't be re-fetched individually later.

Sites are scraped one at a time so a blocked/rate-limited site can't take the
others' results down with it, and so the proxy is only used where required:

  * Glassdoor/ZipRecruiter block datacenter IPs. In CI they're skipped
    unless JOBSPY_PROXY (a residential proxy) is set; on a local machine
    (residential IP) they're attempted directly.
  * LinkedIn's guest API tolerates low volume from anywhere and rate-limits
    rather than hard-blocks, so it uses the proxy when one is configured and
    otherwise tries directly; a 429 just yields fewer results.
  * Indeed works from anywhere and never goes through the proxy — that would
    just burn proxy bandwidth.
"""

from __future__ import annotations

import hashlib
import logging
import os

from . import dedupe, enrich, repost
from .posting import JobPosting

log = logging.getLogger(__name__)

# Import here so the rest of the app works without jobspy installed.
try:
    from jobspy import scrape_jobs as _scrape
    _JOBSPY_AVAILABLE = True
except ImportError:
    _JOBSPY_AVAILABLE = False


# Sites that block requests from datacenter IPs (GitHub Actions runners).
_NEEDS_PROXY = {"glassdoor", "zip_recruiter"}
_PROXY_IF_AVAILABLE = {"linkedin"}


def _make_id(source: str, url: str) -> str:
    """The board's own id when the URL carries one (LinkedIn), else a URL hash."""
    li_id = repost.linkedin_numeric_id(url)
    if source == "linkedin" and li_id:
        return f"li_{li_id}"
    return f"{source}_{hashlib.sha1(url.encode()).hexdigest()[:12]}"


def _cell(row, key: str) -> str:
    """Dataframe cell as a clean string ('' for NaN/NaT/None)."""
    val = row.get(key)
    if val is None:
        return ""
    s = str(val).strip()
    return "" if s.lower() in ("nan", "nat", "none") else s


def fetch_jobs(settings: dict) -> list[JobPosting]:
    """Search all configured job boards and return normalized postings."""
    if not _JOBSPY_AVAILABLE:
        log.info("  jobspy not installed — skipping external job boards.")
        return []

    board_settings = settings.get("jobspy", {})
    if not board_settings.get("enabled", True):
        return []

    sites = board_settings.get("sites", ["indeed"])
    search_terms = board_settings.get("search_terms", settings.get("search_terms", []))
    location = board_settings.get("location", "United States")
    results_per_term = board_settings.get("results_per_term", 50)
    hours_old = board_settings.get("hours_old", 72)

    # Residential proxy for the sites that block datacenter IPs.
    # Format: http://user:pass@host:port  or  socks5://user:pass@host:port
    proxy = os.environ.get("JOBSPY_PROXY")
    in_ci = bool(os.environ.get("GITHUB_ACTIONS"))
    if proxy:
        log.info(f"  [jobspy] proxy configured: {proxy.split('@')[-1]}")  # hide credentials

    seen: dict[str, JobPosting] = {}

    for site in sites:
        # jobspy expects proxies as list[str], e.g. ["user:pass@host:port"].
        proxies = None
        if site in _PROXY_IF_AVAILABLE and proxy:
            proxies = [proxy]
        elif site in _NEEDS_PROXY:
            if proxy:
                proxies = [proxy]
            elif in_ci:
                log.info(f"  [jobspy] {site}: JOBSPY_PROXY not set — skipping "
                      "(datacenter IPs are blocked)")
                continue
            # Local run without a proxy: residential IP, try directly.

        for term in search_terms:
            log.info(f"  [jobspy] {site}: searching {term!r} ...")
            try:
                df = _scrape(
                    site_name=[site],
                    search_term=term,
                    location=location,
                    results_wanted=results_per_term,
                    hours_old=hours_old,
                    country_indeed="USA",
                    proxies=proxies,
                    verbose=0,
                )
            except Exception as exc:
                log.info(f"  [jobspy] {site} term={term!r} failed: {exc}")
                continue

            if df is None or df.empty:
                continue

            for _, row in df.iterrows():
                url = _cell(row, "job_url") or _cell(row, "apply_url")
                if not url:
                    continue
                source = _cell(row, "site") or site
                job_id = _make_id(source, url)
                if job_id in seen:
                    continue

                company, title = _cell(row, "company"), _cell(row, "title")
                if source == "linkedin":
                    # Universities/recruiters re-share others' jobs on LinkedIn.
                    company, title = dedupe.unwrap_reshare(company, title)
                description = _cell(row, "description")
                flags = enrich.scrape_time_flags(title, description)
                seen[job_id] = JobPosting(
                    job_id=job_id,
                    company=company,
                    title=title,
                    apply_url=url,
                    location=_cell(row, "location"),
                    posted_on=_cell(row, "date_posted")[:10],
                    source=source,
                    description=description,
                    direct_url=_cell(row, "job_url_direct"),
                    **flags,
                )

    log.info(f"  [jobspy] {len(seen)} unique postings across all boards.")
    return list(seen.values())
