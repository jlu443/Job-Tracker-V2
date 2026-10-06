"""Fetch job postings from Workday's CXS jobs API.

Workday career sites are JS-rendered, but the page itself calls a JSON endpoint:

    POST https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs

We call that endpoint directly. No browser, no DOM scraping.
"""

from __future__ import annotations

import dataclasses
import logging
import re
import time

import requests

from . import classify, dates, http_pool
from .posting import JobPosting

log = logging.getLogger(__name__)

_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (job-tracker)",
}
_SESSION = http_pool.make_session(_HEADERS)


def _base_url(tenant: str, wd: str) -> str:
    return f"https://{tenant}.{wd}.myworkdayjobs.com"


def _jobs_endpoint(tenant: str, wd: str, site: str) -> str:
    return f"{_base_url(tenant, wd)}/wday/cxs/{tenant}/{site}/jobs"


def job_id_for(tenant: str, external_path: str) -> str | None:
    """Requisition ids (R12345, JR2016444) are only unique within a tenant."""
    tail = external_path.rsplit("_", 1)[-1].strip() if "_" in external_path else ""
    return f"wd_{tenant.lower()}_{tail}" if tail else None


def _posting(p: dict, tenant: str, name: str, base: str, site: str, anchor) -> JobPosting | None:
    ext = p.get("externalPath", "")
    job_id = job_id_for(tenant, ext)
    if not job_id:
        return None
    return JobPosting(
        job_id=job_id,
        company=name,
        title=(p.get("title") or "").strip(),
        apply_url=f"{base}/{site}{ext}",
        location=(p.get("locationsText") or "").strip(),
        posted_on=dates.relative_to_iso(p.get("postedOn") or "", anchor),
        source="workday",
    )


def _page(endpoint: str, term: str, offset: int, settings: dict, name: str,
          facets: dict | None = None) -> dict | None:
    payload = {"appliedFacets": facets or {}, "limit": settings["page_limit"],
               "offset": offset, "searchText": term}
    # Workday rate-limits per client IP across all tenants (HTTP 429). Back
    # off and retry instead of writing the board off as incomplete.
    for attempt in range(4):
        try:
            resp = _SESSION.post(endpoint, json=payload, timeout=settings["request_timeout"])
            if resp.status_code == 429 and attempt < 3:
                retry_after = resp.headers.get("Retry-After", "")
                time.sleep(float(retry_after) if retry_after.isdigit() else 2 * 2 ** attempt)
                continue
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            log.warning(f"  ! {name} term={term!r} offset={offset}: {exc}")
            return None
    return None


def _recent(company: dict, settings: dict, seen: dict) -> tuple[bool, bool]:
    """Newest postings of any title, until a page holds nothing from the last
    window_days. With no search text Workday lists newest first (a few boards
    pin featured jobs on top, hence "a page with nothing recent", not "the
    first old job"). Every title is classified locally afterwards, so this
    also finds roles the keyword sweep's terms miss ("Software Engineer 1").

    Returns (ok, dated): dated=False when the board hides post dates.
    """
    tenant, wd, site = company["tenant"], company["wd"], company["site"]
    name = company.get("name", tenant)
    cfg = (settings.get("recency_check") or {}).get("workday") or {}
    window, max_pages = cfg.get("window_days", 2), cfg.get("max_pages", 8)
    endpoint, base, anchor = _jobs_endpoint(tenant, wd, site), _base_url(tenant, wd), dates.today()
    for page_no in range(max_pages):
        data = _page(endpoint, "", page_no * settings["page_limit"], settings, name)
        if data is None:
            return False, True
        postings = data.get("jobPostings") or []
        if not postings:
            return True, True
        ages = [dates.age_days(dates.relative_to_iso(p.get("postedOn") or "", anchor))
                for p in postings]
        if page_no == 0 and all(a is None for a in ages):
            return True, False
        for p in postings:
            posting = _posting(p, tenant, name, base, site, anchor)
            if posting and posting.job_id not in seen:
                seen[posting.job_id] = posting
        if not any(a is not None and a <= window for a in ages):
            return True, True
        time.sleep(settings["delay_between_requests"])
    return True, True


# Search filters ("facets") that mark early-career jobs. 50 of 80 sampled
# boards had one (2026-10-06): worker type "Intern (Fixed Term)", job family
# "Internship" / "Early Careers". Listing a filter value is exact, where
# keyword search isn't: Sysco's "intern" search returns 705 jobs, unranked,
# with its 26 internships spread to position 677. Worker-type facets name
# the person ("Student", "Trainee"); job families also name staff teams
# ("Student Finance", "Internal Audit"), so they need a stricter match.
_WORKER_FACET = re.compile(r"workersubtype|jobtype|worker_?type", re.I)
_ENTRY_WORKER = re.compile(r"\b(?:interns?|internships?|co-?ops?|students?|apprentice\w*|"
                           r"trainees?|graduates?|new\s+grad\w*|early\s+careers?)\b", re.I)
_ENTRY_FAMILY = re.compile(r"\b(?:interns?|internships?|co-?ops?|early\s+careers?|"
                           r"campus\s+programs?|students?\s+(?:and|&)\s+graduates|"
                           r"graduate\s+programs?|new\s+grad\w*)\b", re.I)
# Mixed or campus-job values: Coca-Cola's "Interim & Interns" (temp staff
# too), a university's "Student Employee" (campus jobs, not internships).
_MIXED_VALUE = re.compile(r"\binterim\b", re.I)
_CAMPUS_JOB_VALUE = re.compile(r"\bstudent\s+(?:employee|worker|assistant|staff)s?\b", re.I)
_INTERN_WORD = re.compile(r"\b(?:interns?|internships?|co-?ops?)\b", re.I)
_NEW_GRAD_VALUE = re.compile(r"\b(?:graduates?|new\s+grad\w*|early\s+careers?)\b", re.I)
_INTERN_VALUE = re.compile(r"\b(?:interns?|internships?|co-?ops?|students?|apprentice\w*|"
                           r"trainees?)\b", re.I)


def entry_facets(facets: list) -> dict[str, list[tuple[str, str]]]:
    """facetParameter -> [(value id, role hint)] for early-career values."""
    out: dict[str, list[tuple[str, str]]] = {}

    def walk(facet: dict) -> None:
        param = facet.get("facetParameter") or ""
        if "location" in param.lower():
            return
        rx = _ENTRY_WORKER if _WORKER_FACET.search(param) else _ENTRY_FAMILY
        for v in facet.get("values") or []:
            if v.get("values"):              # a group of facets
                walk(v)
            elif v.get("id") and rx.search(d := v.get("descriptor") or "") \
                    and not _MIXED_VALUE.search(d) \
                    and not (_CAMPUS_JOB_VALUE.search(d) and not _INTERN_WORD.search(d)):
                hint = "intern" if _INTERN_VALUE.search(d) or not _NEW_GRAD_VALUE.search(d) \
                    else "new_grad"
                out.setdefault(param, []).append((v["id"], hint))

    for f in facets or []:
        walk(f)
    return out


def _facet_pass(company: dict, settings: dict, seen: dict) -> bool:
    """Every job under the board's early-career filter values, labeled with
    the value's role (a plain "Software Engineer" filed as an internship)."""
    tenant, wd, site = company["tenant"], company["wd"], company["site"]
    name = company.get("name", tenant)
    endpoint, base, anchor = _jobs_endpoint(tenant, wd, site), _base_url(tenant, wd), dates.today()
    first = _page(endpoint, "", 0, settings, name)
    if first is None:
        return False
    for param, values in entry_facets(first.get("facets") or []).items():
        for value_id, hint in values:
            offset, total = 0, None
            for _ in range(settings["max_pages_per_term"]):
                data = _page(endpoint, "", offset, settings, name, {param: [value_id]})
                if data is None:
                    return False
                postings = data.get("jobPostings") or []
                if total is None:
                    total = data.get("total") or 0
                for p in postings:
                    posting = _posting(p, tenant, name, base, site, anchor)
                    if posting:
                        kept = seen.get(posting.job_id, posting)
                        if not kept.role_hint:
                            kept = dataclasses.replace(kept, role_hint=hint)
                        seen[posting.job_id] = kept
                offset += settings["page_limit"]
                if not postings or offset >= total:
                    break
                time.sleep(settings["delay_between_requests"])
            else:
                return False
    return True


def _sweep(company: dict, settings: dict, seen: dict) -> bool:
    """Every configured search term, paged while results stay entry-level.
    The complete pass: absence from it means a job was taken down."""
    tenant, wd, site = company["tenant"], company["wd"], company["site"]
    name = company.get("name", tenant)
    next_year = str(dates.today().year + 1)
    terms = [t.replace("{next_year}", next_year)
             for t in company.get("search_terms", settings["search_terms"])]
    endpoint, base, anchor = _jobs_endpoint(tenant, wd, site), _base_url(tenant, wd), dates.today()
    # Search is fuzzy ("intern" also hits "internal", "new grad" hits any
    # "new") but relevance-ranked: entry-level titles cluster on the first
    # pages. Stop once a page has fewer than this many of them.
    min_relevant = settings.get("workday_min_relevant_per_page", 2)
    complete = _facet_pass(company, settings, seen)
    for term in terms:
        offset, total = 0, None
        for _ in range(settings["max_pages_per_term"]):
            data = _page(endpoint, term, offset, settings, name)
            if data is None:
                complete = False
                break
            postings = data.get("jobPostings", [])
            if not postings:
                break
            # Workday only reports `total` on the first page (0 afterwards).
            if total is None:
                total = data.get("total") or 0
            relevant = sum(classify.is_entry_level(p.get("title", "")) for p in postings)
            for p in postings:
                posting = _posting(p, tenant, name, base, site, anchor)
                if posting and posting.job_id not in seen:
                    seen[posting.job_id] = posting
            offset += settings["page_limit"]
            if offset >= total or relevant < min_relevant:
                break
            time.sleep(settings["delay_between_requests"])
        else:
            # Page cap reached with entry-level results still coming (CVS:
            # 4,000+ "intern" results): the rest weren't seen, so absence
            # proves nothing. Otherwise they'd all be marked closed.
            complete = False
    return complete


def fetch_company_jobs(company: dict, settings: dict) -> tuple[list[JobPosting], bool | None]:
    """One Workday board. company["_mode"] (set by main.py) picks the work:

      recent        newest postings only (cheap; every run)
      recent+sweep  both; the sweep makes the result complete
      sweep         keyword sweep only (the default)

    Returns (postings, complete): True = complete, so absent jobs were taken
    down; False = a request failed; None = recent-only by design (partial).
    """
    mode = company.get("_mode", "sweep")
    seen: dict[str, JobPosting] = {}
    ok = True
    if mode.startswith("recent"):
        ok, dated = _recent(company, settings, seen)
        if not dated:            # board hides post dates: recency can't work
            mode = "sweep"
    if mode.endswith("sweep"):
        complete = _sweep(company, settings, seen) and ok
        return list(seen.values()), complete
    return list(seen.values()), (None if ok else False)
