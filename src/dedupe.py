"""Cross-source deduplication.

The same role often appears both on a company's own ATS board and on an
aggregator (Indeed etc.) with unrelated job ids, so it would be stored and
announced twice. Two layers deal with that:

  * dedupe_postings() collapses cross-source duplicates within one run.
    main.py scrapes first-party ATS boards before the aggregators, so the
    first-party copy wins. Same-source twins are kept — two identical titles
    on one company board are usually genuinely separate requisitions.
  * fuzzy_key() is also matched against rows already active in the DB to
    suppress announcing a posting that is a re-listing of a known job under
    a new id/source (e.g. first seen on Indeed, later on the ATS board).
"""

from __future__ import annotations

import re

from . import eightfold_scraper, successfactors_scraper

# Trailing legal suffixes stripped from company names before comparison.
_SUFFIXES = {"inc", "llc", "ltd", "corp", "co", "corporation", "incorporated",
             "company", "plc", "gmbh", "limited"}

_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")


def _tokens(s: str) -> list[str]:
    return _NON_ALNUM.sub(" ", s.lower()).split()


def _norm_company(s: str) -> str:
    tokens = _tokens(s)
    while tokens and tokens[-1] in _SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def _norm_title(s: str) -> str:
    # Token-sorted so "Intern, Software (Summer 2026)" matches
    # "Software Intern - Summer 2026" across sources.
    return " ".join(sorted(_tokens(s)))


def _norm_location(s: str) -> str:
    # City only: "San Francisco, CA" and "San Francisco, California, US" match.
    return " ".join(_tokens((s or "").split(",")[0]))


def fuzzy_key(company: str, title: str, location: str) -> str | None:
    """Stable identity for 'same role, different listing'. None if unkeyable."""
    c, t = _norm_company(company or ""), _norm_title(title or "")
    if not c or not t:
        return None
    return f"{c}|{t}|{_norm_location(location or '')}"


def role_key(job_key: str) -> str:
    """fuzzy_key minus the city: LinkedIn and Workday list one role once per
    location ("5 Locations" vs "Hillsboro, OR"), but to a job seeker that's
    one opening to hear about."""
    return job_key.rsplit("|", 1)[0] if job_key else ""


_RESHARE = re.compile(r"^(?P<title>.+?)\s*(?:\((?:open|closed)\))?\s+at\s+(?P<company>[^()]+?)\s*$",
                      re.IGNORECASE)


def unwrap_reshare(company: str, title: str) -> tuple[str, str]:
    """'GE Vernova Co-op (Open) at GE Vernova', posted by a university's
    LinkedIn page, is GE Vernova's job. Returns the real (company, title)."""
    m = _RESHARE.match(title or "")
    if not m or _norm_company(m["company"]) == _norm_company(company or ""):
        return company, title
    return m["company"].strip(), m["title"].strip()


_URL_IDS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"greenhouse\.io/[\w-]+/jobs/(\d+)"), "gh_{0}"),
    (re.compile(r"[?&]gh_jid=(\d+)"), "gh_{0}"),
    (re.compile(r"greenhouse\.io/embed/job_app\?(?:[^#]*&)?token=(\d+)"), "gh_{0}"),
    (re.compile(r"jobs\.(?:eu\.)?lever\.co/[\w.-]+/([0-9a-f-]{36})"), "lv_{0}"),
    (re.compile(r"jobs\.ashbyhq\.com/[^/]+/([0-9a-f-]{36})"), "ash_{0}"),
    (re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/[^/]+/(\d{6,})"), "sr_{0}"),
    # Workable isn't scraped any more (it blocks CI IPs), but curated-list rows
    # already carry these ids; changing them would re-announce those jobs.
    (re.compile(r"apply\.workable\.com/[\w-]+/j/([0-9A-F]{6,})", re.I), "wk_{0}"),
    (re.compile(r"linkedin\.com/jobs/view/(?:[\w-]*-)?(\d{8,})"), "li_{0}"),
    (re.compile(r"lifeattiktok\.com/(?:[\w-]+/)*(?:search|position)/(\d{12,})"), "tt_{0}"),
    (re.compile(r"(?:jobs\.bytedance\.com/(?:[\w-]+/)?position|joinbytedance\.com/search)/"
                r"(\d{12,})"), "bd_{0}"),
    (re.compile(r"amazon\.jobs/(?:[\w-]+/)?jobs/(\d+)"), "amzn_{0}"),
    (re.compile(r"jobs\.apple\.com/[\w-]+/details/(\d+)"), "apple_{0}"),
    (re.compile(r"ats\.rippling\.com/[\w-]+/jobs/([0-9a-f-]{36})"), "rip_{0}"),
]
# Jibe career sites (careers.X.com): curated lists cite them with ?icims=1.
_JIBE = re.compile(r"https?://((?:[\w-]+\.)+[a-z]{2,})/jobs/(\d+)/?\?(?:[^#]*&)?icims=1", re.I)
_WORKDAY = re.compile(
    # Tail after the *last* underscore, as scraper.job_id_for() takes it.
    r"https?://([\w-]+)\.wd\d+\.myworkdayjobs\.com/(?:[\w-]+/)*job/[^?#]*_([A-Za-z0-9-]+)"
    r"(?:/apply(?:/[\w-]*)?)?/?(?:[?#]|$)")


# Workday's alternate host puts the tenant in the path.
_WORKDAY_SITE = re.compile(
    r"https?://wd\d+\.myworkdaysite\.com/(?:[\w-]+/)?recruiting/([\w-]+)/[\w-]+/job/"
    r"[^?#]*_([A-Za-z0-9-]+)(?:/apply(?:/[\w-]*)?)?/?(?:[?#]|$)")
_ORACLE = re.compile(
    r"https?://([a-z0-9-]+)\.fa(?:\.[a-z0-9-]+)?\.oraclecloud\.com/hcmUI/CandidateExperience/"
    r"[\w-]+/sites/[\w-]+/(?:job|requisitions/preview)/(\d+)", re.I)
_ICIMS = re.compile(r"https?://([a-z0-9-]+)\.icims\.com/jobs/(\d+)", re.I)


def canonical_job_id(url: str) -> str | None:
    """The job_id our own scraper would assign to the posting behind `url`.

    Lets an aggregator or curated-list copy of a job collapse onto the
    first-party row exactly, instead of relying on fuzzy title matching.
    """
    if not url:
        return None
    m = _WORKDAY.search(url) or _WORKDAY_SITE.search(url)
    if m:
        return f"wd_{m.group(1).lower()}_{m.group(2)}"
    m = _ORACLE.search(url)
    if m:
        return f"orc_{m.group(1).lower()}_{m.group(2)}"
    ef = eightfold_scraper.canonical_id(url) or successfactors_scraper.canonical_id(url)
    if ef:
        return ef
    m = _ICIMS.search(url)
    if m:
        return f"icims_{m.group(1).lower()}_{m.group(2)}"
    m = _JIBE.search(url)
    if m:
        return f"jibe_{m.group(1).lower()}_{m.group(2)}"
    for pattern, fmt in _URL_IDS:
        m = pattern.search(url)
        if m:
            return fmt.format(m.group(1))
    return None


def identity(p) -> str:
    """Exact identity of a posting: the first-party id when we can derive it."""
    return (canonical_job_id(getattr(p, "direct_url", ""))
            or canonical_job_id(p.apply_url) or p.job_id)


def dedupe_postings(postings: list, known_ids: set[str] = frozenset()) -> tuple[list, int]:
    """Collapse postings that are the same job.

    Two layers, first-listed posting wins (main.py orders first-party ATS
    boards before curated lists before aggregators):
      1. exact — the same first-party id, derived from the apply URL. An
         aggregator copy of a job already stored under its ATS id
         (`known_ids`) is dropped too: the ATS row already tracks it.
      2. fuzzy — same company/title/city from a *different* source. Same-source
         twins are kept; they're usually genuinely separate requisitions.

    Returns (kept postings in original order, number dropped).
    """
    kept: list = []
    seen_identity: set[str] = set()
    first_source: dict[str, str] = {}
    dropped = 0
    for p in postings:
        ident = identity(p)
        if ident in seen_identity or (ident != p.job_id and ident in known_ids):
            dropped += 1
            continue
        seen_identity.add(ident)

        key = fuzzy_key(p.company, p.title, p.location)
        if key is not None:
            source = getattr(p, "source", "workday")
            prior = first_source.get(key)
            if prior is not None and prior != source:
                dropped += 1
                continue
            first_source.setdefault(key, source)
        kept.append(p)
    return kept, dropped


def collapse_roles(jobs: list[dict], max_locations: int = 4) -> list[dict]:
    """One row per role: copies of the same job (company + title) listed per
    city or per source collapse onto the first one, which callers sort to be
    the newest. Locations are merged, capped with "+N more"."""
    kept: dict[str, dict] = {}
    places: dict[str, list[str]] = {}
    for j in jobs:
        key = role_key(j.get("job_key") or "") or j["job_id"]
        if key not in kept:
            kept[key] = dict(j)
            places[key] = []
        for loc in (j.get("location") or "").split("; "):
            if loc and loc not in places[key]:
                places[key].append(loc)
    for key, job in kept.items():
        locs = places[key]
        extra = len(locs) - max_locations
        job["location"] = "; ".join(locs[:max_locations]) + (f" +{extra} more" if extra > 0 else "")
    return list(kept.values())

