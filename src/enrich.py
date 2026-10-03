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
import logging
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

from . import classify, http_pool, icims_scraper, phd

log = logging.getLogger(__name__)

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


_LI_DESC = re.compile(r'show-more-less-html__markup[^>]*>(.*?)</div>', re.S)
_LI_APPLICANTS = re.compile(
    r'num-applicants__(?:caption|figure)[^>]*>\s*(.*?)\s*<', re.S)
_LI_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                             "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"}


# LinkedIn's guest API answers bursts with 429. Requests are spaced across
# the enrichment thread pool, and the first 429 stops LinkedIn for the run.
_LI_INTERVAL = 2.0
_li_lock = threading.Lock()
_li_last = [0.0]
_li_blocked = threading.Event()


def _fetch_linkedin(job: dict) -> str:
    # Public (logged-out) job page; also yields the applicant count, a useful
    # competitiveness signal that only LinkedIn exposes.
    li_id = job["job_id"].removeprefix("li_")
    if _li_blocked.is_set():
        return ""                     # unread; the backlog retries next run
    with _li_lock:                    # one request at a time, spaced out
        wait = _li_last[0] + _LI_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _li_last[0] = time.monotonic()
    resp = requests.get(
        f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{li_id}",
        headers=_LI_HEADERS, timeout=_TIMEOUT)
    if resp.status_code == 429:
        if not _li_blocked.is_set():
            _li_blocked.set()
            log.warning("  ! LinkedIn rate limit: skipping its descriptions for the rest of this run")
        return ""
    resp.raise_for_status()
    page = resp.text
    m = _LI_APPLICANTS.search(page)
    if m:
        job["applicants"] = " ".join(m.group(1).split())
    m = _LI_DESC.search(page)
    return _strip_html(m.group(1)) if m else ""


_ORACLE_URL = re.compile(r"https://([^/]+)/hcmUI/CandidateExperience/[\w-]+/sites/([\w-]+)/job/(\d+)")


def _fetch_oracle(job: dict) -> str:
    m = _ORACLE_URL.search(job["apply_url"])
    if not m:
        return ""
    host, site, req_id = m.groups()
    url = (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
           f"?expand=all&onlyData=true&finder=ById;Id=%22{req_id}%22,siteNumber={site}")
    items = _SESSION.get(url, timeout=_TIMEOUT).json().get("items") or [{}]
    d = items[0]
    return "\n".join(_strip_html(d.get(k) or "") for k in (
        "ExternalDescriptionStr", "ExternalResponsibilitiesStr",
        "ExternalQualificationsStr", "CorporateDescriptionStr"))


def _fetch_icims(job: dict) -> str:
    m = re.search(r"https://([^/]+)/jobs/(\d+)", job["apply_url"])
    return icims_scraper.fetch_description(*m.groups()) if m else ""


def _fetch_smartrecruiters(job: dict) -> str:
    m = re.search(r"smartrecruiters\.com/([^/]+)/(\d+)", job["apply_url"])
    if not m:
        return ""
    company, pid = m.groups()
    d = _SESSION.get(f"https://api.smartrecruiters.com/v1/companies/{company}/postings/{pid}",
                     timeout=_TIMEOUT).json()
    sections = ((d.get("jobAd") or {}).get("sections") or {}).values()
    return "\n".join(_strip_html(s.get("text") or "") for s in sections if isinstance(s, dict))


_FETCHERS = {
    "workday": _fetch_workday,
    "greenhouse": _fetch_greenhouse,
    "lever": _fetch_lever,
    "ashby": _fetch_ashby,
    "linkedin": _fetch_linkedin,
    "oracle": _fetch_oracle,
    "icims": _fetch_icims,
    "smartrecruiters": _fetch_smartrecruiters,
}
_ID_PREFIX_SOURCE = {"wd": "workday", "gh": "greenhouse", "lv": "lever",
                     "ash": "ashby", "li": "linkedin", "orc": "oracle", "icims": "icims",
                     "sr": "smartrecruiters"}


def fetchable(job: dict) -> bool:
    """Whether some fetcher can read this job's description."""
    source = _ID_PREFIX_SOURCE.get(job["job_id"].split("_", 1)[0], job.get("source", ""))
    return source in _FETCHERS


def _description_for(job: dict) -> str:
    # LinkedIn's scrape-time description is usually empty; its page also
    # carries the applicant count, so always visit it.
    if job.get("description") and job.get("source") != "linkedin":
        return job["description"]
    # Curated-list rows carry their ATS's id, so the ATS fetcher applies.
    source = _ID_PREFIX_SOURCE.get(job["job_id"].split("_", 1)[0], job.get("source", ""))
    fetcher = _FETCHERS.get(source)
    if fetcher is None:  # sources without a detail fetcher — flags stay unknown
        return job.get("description", "")
    try:
        return fetcher(job)
    except (requests.RequestException, ValueError, KeyError, AttributeError) as exc:
        log.warning(f"  ! enrich fetch failed for {job['job_id']}: {exc}")
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
)]

# US citizenship required. Kept apart from "no sponsorship": a green-card
# holder qualifies for the latter but not the former. Citizenship-only roles
# also count as sponsorship "no".
_CITIZEN = [re.compile(p) for p in (
    r"(?:u\.?s\.?|united states) citizen(?:ship)?(?: is)? required",
    r"must be (?:a )?(?:u\.?s\.?|united states) citizen",
    r"(?:requires?|requiring) (?:u\.?s\.?|united states) citizenship",
    r"only (?:u\.?s\.?|united states) citizens",
    # A bare requirement bullet: "Required Qualifications: ... US Citizenship."
    r"(?:^|[.:] )(?:u\.?s\.?|united states) citizenship(?: required| is required)?(?:\.|$)",
)]

# Student work authorization (F-1 CPT / OPT / STEM OPT), stated in the posting.
# There's no public per-employer data for either, so the posting is the only
# direct source. Bare "opt" is English ("opt in"), so only unambiguous forms
# count.
# Bump when parse_flags learns a new flag: open PhD/research rows are re-read.
FLAGS_VERSION = "2"   # 2: opt_cpt

_OPT_TERM = (r"(?:stem opt|opt ?/ ?cpt|cpt ?/ ?opt|opt or cpt|cpt or opt|opt and cpt|"
             r"cpt and opt|\bcpt\b|curricular practical training|optional practical "
             r"training|f-?1 (?:students?|visa|opt|status))")
_OPT_NO = [re.compile(p) for p in (
    r"(?:not|unable|cannot|can ?not|won'?t|will not|do(?:es)? not|ineligible)[^.]{0,60}"
    + _OPT_TERM,
    _OPT_TERM + r"[^.]{0,40}(?:not (?:eligible|accepted|supported|available|considered)"
                r"|ineligible)",
)]
_OPT_YES = [re.compile(p) for p in (
    _OPT_TERM + r"[^.]{0,60}(?:eligible|welcome|accepted|considered|supported|available|"
                r"encouraged to apply)",
    r"(?:accept|consider|welcome|support)\w*[^.]{0,40}" + _OPT_TERM,
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

_MONEY = r"\$\s?(\d[\d,]*(?:\.\d+)?)\s?(k)?"
_UNIT = r"(?:\s*(?:/\s?h(?:ou)?r\b|per hour|an hour|/\s?y(?:ea)?r\b|per year|annually))?"
_PAY_RANGE = re.compile(_MONEY + _UNIT + r"\s*(?:usd\s*)?(?:-|–|—|to)\s*(?:usd\s*)?\$?\s?"
                        r"(\d[\d,]*(?:\.\d+)?)\s?(k)?(?P<after>[^.]{0,40})", re.I)
_NOT_PAY = re.compile(r"^\s*(?:million|billion|mm|bn|m\b|b\b)", re.I)


def _amount(number: str, k: str) -> float:
    return float(number.replace(",", "")) * (1000 if k else 1)


def parse_pay(text: str) -> str:
    """First plausible pay range in the text, normalized: '$45–55/hr',
    '$120k–150k/yr', or one figure when both ends match. '' if none."""
    for m in _PAY_RANGE.finditer(text or ""):
        low, high = _amount(m.group(1), m.group(2)), _amount(m.group(3), m.group(4))
        if high < low or _NOT_PAY.match(m.group("after") or ""):   # "$5 to $10 million"
            continue
        # Annualized figures for hourly roles ("$95,698 USD (Hourly Role)")
        # are still yearly amounts; anything under $500 is an hourly rate.
        hourly = high < 500
        if hourly and 7 <= low <= 500:
            fmt = lambda v: f"{v:.0f}" if v == int(v) else f"{v:.2f}"
            span = fmt(low) if round(low) == round(high) else f"{fmt(low)}–{fmt(high)}"
            return f"${span}/hr"
        if not hourly and 15_000 <= low <= 2_000_000:
            k = lambda v: f"{v / 1000:.0f}k" if v >= 10_000 else f"{v:.0f}"
            span = k(low) if k(low) == k(high) else f"{k(low)}–{k(high)}"
            return f"${span}/yr"
    return ""


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
    """sponsorship: 'yes' | 'no' | ''   citizenship: 'required' | ''
    clearance: 'yes' (required/mentioned) | 'none' (explicitly not required) | ''
    grad_year: '2026' | '2026, 2027' | ''      pay: '$45–55/hr' | '$120k–150k/yr' | ''
    opt_cpt: 'yes' (CPT/OPT/F-1 students accepted) | 'no' (ruled out) | ''"""
    flags = {"sponsorship": "", "citizenship": "", "clearance": "", "grad_year": "",
             "pay": "", "opt_cpt": ""}
    if not text:
        return flags
    # Newlines become sentence boundaries so bullet-list items don't bleed
    # into each other; then collapse whitespace for the space-based patterns.
    t = re.sub(r"\s*\n+\s*", ". ", text.lower())
    t = " ".join(t.split())

    if any(p.search(t) for p in _CITIZEN):
        flags["citizenship"] = "required"
    if flags["citizenship"] or any(p.search(t) for p in _NO_SPONSOR):
        flags["sponsorship"] = "no"
    elif any(p.search(t) for p in _YES_SPONSOR):
        flags["sponsorship"] = "yes"

    if any(p.search(t) for p in _NO_CLEARANCE):
        flags["clearance"] = "none"
    elif any(p.search(t) for p in _CLEARANCE):
        flags["clearance"] = "yes"

    if flags["citizenship"] or any(p.search(t) for p in _OPT_NO):
        flags["opt_cpt"] = "no"
    elif any(p.search(t) for p in _OPT_YES):
        flags["opt_cpt"] = "yes"

    flags["grad_year"] = _grad_years(t)
    flags["pay"] = parse_pay(text)
    return flags


def scrape_time_flags(title: str, description: str, role_hint: str = "",
                      research_track: str = "") -> dict:
    """Flags for a posting whose description came with the listing (Greenhouse,
    Lever, Ashby, aggregators): no extra request. Only for jobs we'd keep, to
    spare the regex work on ~100k senior postings a run. Returns JobPosting
    keyword arguments, or {} when not applicable."""
    if not description or not (role_hint or research_track
                                or classify.is_entry_level(title)):
        return {}
    return {**parse_flags(description), "checked": True}


def described_fields(title: str, description: str) -> dict:
    """Everything a listing's own description tells us, as JobPosting kwargs:
    new-grad hint, PhD/research track, and sponsorship/clearance flags."""
    hint = classify.role_hint_from_description(title, description)
    track = phd.track(title, description)
    return {"role_hint": hint, "research_track": track,
            **scrape_time_flags(title, description, hint, track)}


def _enrich_one(job: dict) -> dict:
    text = _description_for(job)
    flags = parse_flags(text)
    if text:   # read, even if it mentions none of the flags
        job["checked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # Never let "no mention found" erase a value the source supplied.
    job.update({k: v for k, v in flags.items() if v or not job.get(k)})
    # The description can reveal a PhD/research track the title hides.
    track = phd.track(job.get("title", ""), text)
    if track and job.get("research_track") != "phd":
        job["research_track"] = track
    time.sleep(_DELAY)
    return flags


def enrich_jobs(jobs: list[dict], label: str = "announceable postings",
                workers: int = 8) -> None:
    """Fetch + parse each job's description; mutates the dicts in place.
    Jobs span many hosts, so a small thread pool keeps this quick."""
    if not jobs:
        return
    log.info(f"Enriching {len(jobs)} {label} ...")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(_enrich_one, jobs))
    counts = Counter(k for flags in results for k, v in flags.items() if v)
    read = sum(1 for j in jobs if j.get("checked_at"))
    log.info(f"  read {read}/{len(jobs)} descriptions — sponsorship: {counts['sponsorship']}, "
          f"citizenship: {counts['citizenship']}, clearance: {counts['clearance']}, "
          f"grad year: {counts['grad_year']}")
