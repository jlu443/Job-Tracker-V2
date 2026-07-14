"""Parse job descriptions into relevance flags for announceable jobs.

Only new intern/new_grad postings get enriched (a handful per hourly run), so
the per-job detail request each ATS needs is affordable. Aggregator postings
(Indeed etc.) can't be re-fetched individually — JobSpy captures their
descriptions at scrape time and they arrive on the job dict instead.

Flags set on each job dict (and persisted to the DB by db.update_enrichment):
    sponsorship: 'no' | 'yes' | ''   visa sponsorship explicitly ruled out / offered
    clearance:   'yes' | ''          security clearance required or mentioned
    grad_year:   '2026' | '2026, 2027' | ''   graduation window mentioned
"""

from __future__ import annotations

import html
import re
import time
from urllib.parse import urlparse

import requests

from . import http_pool

_HEADERS = {"User-Agent": "Mozilla/5.0 (job-tracker)", "Accept": "application/json"}
_SESSION = http_pool.make_session(_HEADERS)
_TIMEOUT = 30
_DELAY = 0.2  # politeness between per-job detail requests

_TAG = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return _TAG.sub(" ", html.unescape(text or ""))


# --- per-source description fetchers ----------------------------------------
# Each takes the job dict and returns plain-ish text ("" when unavailable).

def _fetch_workday(job: dict) -> str:
    # apply_url:  https://{tenant}.{wd}.myworkdayjobs.com/{site}/job/...
    # detail API: https://{host}/wday/cxs/{tenant}/{site}/job/...
    u = urlparse(job["apply_url"])
    tenant = (u.hostname or "").split(".")[0]
    site, _, rest = u.path.lstrip("/").partition("/")
    if not (tenant and site and rest):
        return ""
    url = f"https://{u.hostname}/wday/cxs/{tenant}/{site}/{rest}"
    data = _SESSION.get(url, timeout=_TIMEOUT).json()
    return _strip_html((data.get("jobPostingInfo") or {}).get("jobDescription", ""))


_GH_URL = re.compile(
    r"(?:job-boards|boards)(?:\.eu)?\.greenhouse\.io/([A-Za-z0-9_-]+)/jobs/(\d+)")


def _fetch_greenhouse(job: dict) -> str:
    m = _GH_URL.search(job["apply_url"])
    if not m:  # company-hosted URL; board token not recoverable
        return ""
    token, gh_id = m.groups()
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{gh_id}"
    return _strip_html(_SESSION.get(url, timeout=_TIMEOUT).json().get("content", ""))


def _fetch_lever(job: dict) -> str:
    # apply_url: https://jobs.lever.co/{slug}/{uuid}. descriptionPlain alone
    # misses the requirement bullets, which live in lists[].content.
    parts = urlparse(job["apply_url"]).path.strip("/").split("/")
    if len(parts) < 2:
        return ""
    url = f"https://api.lever.co/v0/postings/{parts[0]}/{parts[1]}"
    d = _SESSION.get(url, timeout=_TIMEOUT).json()
    pieces = [d.get("descriptionPlain", ""), d.get("additionalPlain", "")]
    pieces += [_strip_html(l.get("content", "")) for l in d.get("lists") or []]
    return "\n".join(p for p in pieces if p)


# One board fetch covers every enriched job at that company.
_ashby_boards: dict[str, list] = {}


def _fetch_ashby(job: dict) -> str:
    # The board payload includes descriptionPlain per job.
    slug = urlparse(job["apply_url"]).path.strip("/").split("/")[0]
    if not slug:
        return ""
    if slug not in _ashby_boards:
        url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
        _ashby_boards[slug] = _SESSION.get(url, timeout=_TIMEOUT).json().get("jobs", [])
    ash_id = job["job_id"].removeprefix("ash_")
    for j in _ashby_boards[slug]:
        if j.get("id") == ash_id:
            return j.get("descriptionPlain") or _strip_html(j.get("descriptionHtml", ""))
    return ""


_FETCHERS = {
    "workday": _fetch_workday,
    "greenhouse": _fetch_greenhouse,
    "lever": _fetch_lever,
    "ashby": _fetch_ashby,
}


def _description_for(job: dict) -> str:
    if job.get("description"):  # captured at scrape time (JobSpy sources)
        return job["description"]
    fetcher = _FETCHERS.get(job.get("source", ""))
    if fetcher is None:  # smartrecruiters/workable etc. — flags stay unknown
        return ""
    try:
        return fetcher(job)
    except (requests.RequestException, ValueError, KeyError, AttributeError) as exc:
        print(f"  ! enrich fetch failed for {job['job_id']}: {exc}")
        return ""


# --- flag parsing ------------------------------------------------------------
# Patterns run against lowercased, whitespace-collapsed text; [^.]{0,N} keeps
# a match within roughly one sentence.

_NO_SPONSOR = [re.compile(p) for p in (
    r"(?:not|unable|cannot|can ?not|won'?t|will not|do(?:es)? not)"
    r"(?: \w+){0,3} sponsor",
    r"no (?:visa |work |immigration )?sponsorship",
    r"sponsorship (?:is )?(?:not (?:available|offered|provided)|unavailable)",
    r"without (?:visa |employer |the need for )?sponsorship",
    r"not (?:offer|provide|be able)[^.]{0,30}sponsor",
    r"(?:u\.?s\.?|united states) citizen(?:ship)?(?: is)? required",
    r"must be (?:a )?(?:u\.?s\.?|united states) citizen",
)]

_YES_SPONSOR = [re.compile(p) for p in (
    r"(?:visa |h-?1b |immigration )?sponsorship (?:is )?available",
    r"will (?:consider )?sponsor",
    r"(?:offer|provide)s? (?:visa |immigration |work )?sponsorship",
    r"open to (?:visa )?sponsorship",
)]

_NO_CLEARANCE = [re.compile(p) for p in (
    r"(?:no|not)[^.]{0,40}clearance",
    r"clearance[^.]{0,20}not required",
)]

_CLEARANCE = [re.compile(p) for p in (
    r"security clearance",
    r"ts\W{0,2}sci",
    r"top secret",
    r"secret clearance",
    r"public trust",
    r"clearance (?:is )?required",
    r"able to obtain[^.]{0,30}clearance",
)]

_GRAD_WORDS = re.compile(r"graduat\w*|class of|degree completion")
_YEAR = re.compile(r"\b(20\d{2})\b")


def _grad_years(t: str) -> str:
    """All plausible years from sentences that mention graduation."""
    years: set[str] = set()
    for sentence in t.split("."):
        if _GRAD_WORDS.search(sentence):
            years.update(y for y in _YEAR.findall(sentence)
                         if 2024 <= int(y) <= 2032)
    return ", ".join(sorted(years))


def parse_flags(text: str) -> dict:
    flags = {"sponsorship": "", "clearance": "", "grad_year": ""}
    if not text:
        return flags
    # Newlines become sentence boundaries so bullet-list items don't bleed
    # into each other; then collapse whitespace for the space-based patterns.
    t = re.sub(r"\s*\n+\s*", ". ", text.lower())
    t = " ".join(t.split())

    if any(p.search(t) for p in _NO_SPONSOR):
        flags["sponsorship"] = "no"
    elif any(p.search(t) for p in _YES_SPONSOR):
        flags["sponsorship"] = "yes"

    if (not any(p.search(t) for p in _NO_CLEARANCE)
            and any(p.search(t) for p in _CLEARANCE)):
        flags["clearance"] = "yes"

    flags["grad_year"] = _grad_years(t)
    return flags


def enrich_jobs(jobs: list[dict]) -> None:
    """Fetch + parse each job's description; mutates the dicts in place."""
    if not jobs:
        return
    print(f"Enriching {len(jobs)} announceable postings ...")
    counts = {"sponsorship": 0, "clearance": 0, "grad_year": 0}
    for job in jobs:
        flags = parse_flags(_description_for(job))
        job.update(flags)
        for key, value in flags.items():
            if value:
                counts[key] += 1
        time.sleep(_DELAY)
    print(f"  flags set — sponsorship: {counts['sponsorship']}, "
          f"clearance: {counts['clearance']}, grad year: {counts['grad_year']}")
