"""Single-company career sites with public JSON search: TikTok, Amazon, Apple.

Each keeps to entry-level searches (or, for Apple, its Students team) and
lets the local classifier decide what's intern / new grad. Like the other
scrapers, fetch_company_jobs(company, settings) -> (postings, complete); the
config file for each holds one entry, so these slot into main.py's ATS loop.

Not covered here, and why (so they stay on the curated lists):
  ByteDance  search API requires a signature computed by its own JavaScript
  Tesla      bot protection answers "Access Denied" to non-browser clients
  Google     no public JSON search
"""

from __future__ import annotations

import time
from datetime import datetime

import requests

from . import classify, enrich, http_pool
from .posting import JobPosting

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126 Safari/537.36")
_SESSION = http_pool.make_session({"User-Agent": _UA, "Accept": "application/json"})


def _date(text: str, fmt: str) -> str:
    try:
        return datetime.strptime((text or "").strip(), fmt).date().isoformat()
    except ValueError:
        return ""


def _fields(title: str, description: str, hint: str = "") -> dict:
    fields = enrich.described_fields(title, description)
    if hint:
        fields["role_hint"] = hint
    return fields


# -- TikTok ---------------------------------------------------------------------

_TIKTOK_API = "https://api.lifeattiktok.com/api/v1/public/supplier/search/job/posts"
_TIKTOK_TERMS = ("intern", "graduate", "new grad", "phd", "university")


def _tiktok_location(city: dict | None) -> str:
    names = []
    while city:
        name = city.get("en_name") or city.get("i18n_name") or city.get("name")
        if name and name not in names:
            names.append(name)
        city = city.get("parent")
    return ", ".join(names)


def fetch_tiktok(company: dict, settings: dict) -> tuple[list[JobPosting], bool]:
    timeout = settings.get("request_timeout", 30)
    seen: dict[str, JobPosting] = {}
    complete = True
    for term in company.get("search_terms", _TIKTOK_TERMS):
        for offset in range(0, 5000, 100):
            try:
                resp = _SESSION.post(_TIKTOK_API, timeout=timeout,
                                     json={"keyword": term, "limit": 100, "offset": offset},
                                     headers={"Content-Type": "application/json",
                                              "website-path": "tiktok"})
                resp.raise_for_status()
                data = resp.json().get("data") or {}
            except (requests.RequestException, ValueError) as exc:
                print(f"  ! TikTok term={term!r} offset={offset}: {exc}")
                complete = False
                break
            jobs = data.get("job_post_list") or []
            for j in jobs:
                jid = str(j.get("id") or "")
                if not jid or f"tt_{jid}" in seen:
                    continue
                title = (j.get("title") or "").strip()
                description = f"{j.get('description') or ''}\n{j.get('requirement') or ''}"
                kind = ((j.get("recruit_type") or {}).get("en_name") or "").lower()
                seen[f"tt_{jid}"] = JobPosting(
                    job_id=f"tt_{jid}", company=company.get("name", "TikTok"), title=title,
                    apply_url=f"https://lifeattiktok.com/search/{jid}",
                    location=_tiktok_location(j.get("city_info")), posted_on="",
                    source="tiktok", **_fields(title, description,
                                               "intern" if kind == "intern" else ""))
            if len(jobs) < 100 or offset + 100 >= (data.get("count") or 0):
                break
            time.sleep(settings.get("delay_between_requests", 0.5))
    return list(seen.values()), complete


# -- Amazon ---------------------------------------------------------------------

_AMAZON_API = "https://www.amazon.jobs/en/search.json"
_AMAZON_TERMS = ("intern", "internship", "co-op", "new grad", "university graduate")


def fetch_amazon(company: dict, settings: dict) -> tuple[list[JobPosting], bool]:
    timeout = settings.get("request_timeout", 30)
    seen: dict[str, JobPosting] = {}
    complete = True
    for term in company.get("search_terms", _AMAZON_TERMS):
        for offset in range(0, 2000, 100):
            try:
                resp = _SESSION.get(_AMAZON_API, timeout=timeout, params={
                    "base_query": term, "result_limit": 100, "offset": offset,
                    "normalized_country_code[]": "USA", "sort": "recent"})
                resp.raise_for_status()
                data = resp.json()
            except (requests.RequestException, ValueError) as exc:
                print(f"  ! Amazon term={term!r} offset={offset}: {exc}")
                complete = False
                break
            jobs = data.get("jobs") or []
            for j in jobs:
                jid = str(j.get("id_icims") or "")
                if not jid or f"amzn_{jid}" in seen:
                    continue
                title = (j.get("title") or "").strip()
                description = "\n".join(j.get(k) or "" for k in (
                    "description", "basic_qualifications", "preferred_qualifications"))
                seen[f"amzn_{jid}"] = JobPosting(
                    job_id=f"amzn_{jid}", company=company.get("name", "Amazon"), title=title,
                    apply_url=f"https://www.amazon.jobs{j.get('job_path') or ''}",
                    location=j.get("normalized_location") or "",
                    posted_on=_date(j.get("posted_date"), "%B %d, %Y"),
                    source="amazon", **_fields(title, description))
            if len(jobs) < 100 or offset + 100 >= (data.get("hits") or 0):
                break
            time.sleep(settings.get("delay_between_requests", 0.5))
    return list(seen.values()), complete


# -- Apple ----------------------------------------------------------------------

_APPLE = "https://jobs.apple.com"
# Apple's "Students" team holds internships and university-graduate roles
# whose titles often don't say so ("SoC Design Verification Engineer"), plus
# Apple Store student roles ("US-Technical Expert"). The INTRN sub-team
# filter alone is unreliable (25 results one hour, 0 the next), so both
# queries run and results are merged.
_APPLE_QUERIES = (
    {"teams": [{"team": "teamsAndSubTeams-STDNT", "subTeam": "subTeam-INTRN"}]},
    {"teams": [{"team": "teamsAndSubTeams-STDNT"}]},
)


def _apple_hint(title: str) -> str:
    """Students-team role: an internship if the title says so; otherwise a
    university-graduate role when it's a tech job (not retail "US-...")."""
    if classify.is_entry_level(title):
        return ""          # the title already decides
    if title.upper().startswith("US-") or classify.categorize(title) == "other":
        return ""
    return "new_grad"


def _apple_location(locations: list) -> str:
    out = []
    for loc in locations or []:
        parts = [loc.get("city") or loc.get("name") or "", loc.get("stateProvince") or "",
                 loc.get("countryName") or ""]
        text = ", ".join(p for p in parts if p)
        if text and text not in out:
            out.append(text)
    return "; ".join(out)


def fetch_apple(company: dict, settings: dict) -> tuple[list[JobPosting], bool | None]:
    timeout = settings.get("request_timeout", 30)
    session = requests.Session()
    session.headers.update({"User-Agent": _UA, "Accept": "application/json"})
    try:
        token = session.get(f"{_APPLE}/api/v1/CSRFToken", timeout=timeout) \
            .headers.get("x-apple-csrf-token")
    except requests.RequestException as exc:
        print(f"  ! Apple: {exc}")
        return [], False
    headers = {"x-apple-csrf-token": token or "", "Content-Type": "application/json",
               "Origin": _APPLE, "Referer": f"{_APPLE}/en-us/search"}
    seen: dict[str, JobPosting] = {}
    complete = True
    for filters in _APPLE_QUERIES:
        for page in range(1, 30):
            body = {"query": "", "page": page, "locale": "en-us", "sort": "newest",
                    "filters": {"range": {"standardWeeklyHours": {"start": None, "end": None}},
                                "locations": ["postLocation-USA"], **filters},
                    "format": {"longDate": "MMMM D, YYYY", "mediumDate": "MMM D, YYYY"}}
            try:
                resp = session.post(f"{_APPLE}/api/v1/search", json=body, headers=headers,
                                    timeout=timeout)
                resp.raise_for_status()
                res = resp.json().get("res") or {}
            except (requests.RequestException, ValueError) as exc:
                print(f"  ! Apple page={page}: {exc}")
                complete = False
                break
            results = res.get("searchResults") or []
            for j in results:
                pid = str(j.get("positionId") or "")
                if not pid or f"apple_{pid}" in seen:
                    continue
                title = (j.get("postingTitle") or "").strip()
                seen[f"apple_{pid}"] = JobPosting(
                    job_id=f"apple_{pid}", company=company.get("name", "Apple"), title=title,
                    apply_url=f"{_APPLE}/en-us/details/{pid}/{j.get('transformedPostingTitle') or ''}",
                    location=_apple_location(j.get("locations")),
                    posted_on=_date(j.get("postingDate"), "%b %d, %Y"),
                    source="apple", **_fields(title, j.get("jobSummary") or "",
                                              _apple_hint(title)))
            # Page until a short page: totalRecords isn't always reliable.
            if len(results) < 20:
                break
            time.sleep(settings.get("delay_between_requests", 0.5))
    # Apple's search returns different result counts for the same query from
    # one call to the next (73, then 44), so missing from one run proves
    # nothing: report partial (None) and let the 60-day purge retire jobs.
    return list(seen.values()), (None if complete else False)
