"""Discover company job boards across every supported ATS.

The scrapers can only fetch what discovery finds, so discovery is the coverage
ceiling. The pipeline is source-agnostic and ATS-agnostic:

    sources (broad)               extraction                 validation (exact)
    ---------------               ----------                 ------------------
    config/seeds.txt           →                          →
    GitHub job lists (+JSON)   →   every ATS's URL regex  →   hit the ATS's
    JobSpy posting URLs        →   runs over every byte   →   public API; keep
    Common Crawl URL index     →   of harvested text      →   boards with jobs

Every source feeds every ATS: a Greenhouse link in a GitHub README, an Ashby
URL inside an Indeed posting, and a Workday tenant in Common Crawl are all
caught in the same pass. Validation is cheap (one API call per candidate), so
false positives from the broad harvest cost nothing. Survivors are merged into
per-ATS config files; existing entries are always preserved.

Run occasionally (it's slow); the per-run scraper just reads the config files.

    python -m src.discover                    # all sources, all ATSes
    python -m src.discover --seeds-only       # seeds.txt only (fast smoke test)
    python -m src.discover --no-cc            # skip Common Crawl (the slow one)
    python -m src.discover --ats greenhouse,ashby   # limit to some ATSes
    python -m src.discover --cc-max-pages 100      # cap CC pages per pattern
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable
from urllib.parse import unquote

import requests
import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONFIG_DIR = os.path.join(_ROOT, "config")
_SEEDS = os.path.join(_CONFIG_DIR, "seeds.txt")
_CHECKPOINT = os.path.join(_ROOT, ".discover_checkpoint.json")

_HEADERS = {"User-Agent": "Mozilla/5.0 (job-tracker-discover)"}
_MAX_RETRIES = 4
_INITIAL_BACKOFF = 2
_VALIDATE_WORKERS = 8


# ─────────────────────────────────────────────────────────────────────────────
# Shared HTTP helpers (retry on 429/5xx/network, give up fast on 4xx)
# ─────────────────────────────────────────────────────────────────────────────

def _request_json(method: str, url: str, *, payload: dict | None = None,
                  params: dict | None = None, timeout: int = 25):
    """Return parsed JSON, or None on a definitive miss (4xx) / repeated failure."""
    backoff = _INITIAL_BACKOFF
    headers = dict(_HEADERS)
    if payload is not None:
        headers["Content-Type"] = "application/json"
    for attempt in range(_MAX_RETRIES):
        try:
            resp = requests.request(method, url, json=payload, params=params,
                                    headers=headers, timeout=timeout)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise requests.RequestException(f"HTTP {resp.status_code}")
            if resp.status_code != 200:
                return None
            return resp.json()
        except (requests.RequestException, ValueError):
            if attempt == _MAX_RETRIES - 1:
                return None
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# ATS registry: URL patterns, Common Crawl queries, and validators per ATS
# ─────────────────────────────────────────────────────────────────────────────

_WD_RE = re.compile(
    r"https?://(?P<tenant>[a-z0-9-]+)\.(?P<wd>wd\d+)\.myworkdayjobs\.com"
    r"/(?:[a-z]{2}-[A-Z]{2}/)?(?P<site>[A-Za-z0-9_-]+)",
    re.IGNORECASE,
)
_WD_BAD_SITES = {"wday", "cxs", "assets", "static", "login", "fonts"}

_GH_RES = [
    re.compile(r"boards(?:-api)?\.greenhouse\.io(?:/v1/boards)?/([a-z0-9_-]+)", re.I),
    re.compile(r"job-boards(?:\.eu)?\.greenhouse\.io/([a-z0-9_-]+)", re.I),
    re.compile(r"greenhouse\.io/embed/job_board\?[^\s\"'<>]*?for=([a-z0-9_-]+)", re.I),
]
_GH_BAD = {"v1", "boards", "jobs", "embed", "js", "generic", "internal"}

_LV_RE = re.compile(r"jobs\.lever\.co/([A-Za-z0-9_-]+)", re.I)

_ASHBY_RE = re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)", re.I)
_ASHBY_BAD = {"api"}

_SR_RE = re.compile(
    r"(?:jobs|careers)\.smartrecruiters\.com/(?:oneclick-ui/company/)?([A-Za-z0-9]+)",
    re.I,
)
_SR_BAD = {"sitemap", "favicon"}

_WK_RE = re.compile(r"apply\.workable\.com/([a-z0-9-]+)", re.I)
_WK_BAD = {"api", "j", "jobs", "assets"}


def _extract_workday(text: str) -> list[dict]:
    out = []
    for m in _WD_RE.finditer(text):
        site = m.group("site")
        if site.lower() in _WD_BAD_SITES:
            continue
        out.append({"tenant": m.group("tenant").lower(), "wd": m.group("wd").lower(),
                    "site": site, "name": m.group("tenant").lower()})
    return out


def _extract_greenhouse(text: str) -> list[dict]:
    out = []
    for rx in _GH_RES:
        for m in rx.finditer(text):
            token = m.group(1).lower()
            if token not in _GH_BAD:
                out.append({"token": token, "name": token})
    return out


def _extract_lever(text: str) -> list[dict]:
    return [{"slug": m.group(1).lower(), "name": m.group(1).lower()}
            for m in _LV_RE.finditer(text)]


def _extract_ashby(text: str) -> list[dict]:
    out = []
    for m in _ASHBY_RE.finditer(text):
        slug = m.group(1).rstrip(".")
        if slug.lower() in _ASHBY_BAD:
            continue
        out.append({"slug": slug, "name": unquote(slug)})
    return out


def _extract_smartrecruiters(text: str) -> list[dict]:
    out = []
    for m in _SR_RE.finditer(text):
        company = m.group(1)
        if company.lower() in _SR_BAD:
            continue
        out.append({"company": company, "name": company})
    return out


def _extract_workable(text: str) -> list[dict]:
    out = []
    for m in _WK_RE.finditer(text):
        slug = m.group(1).lower()
        if slug in _WK_BAD:
            continue
        out.append({"slug": slug, "name": slug})
    return out


def _validate_workday(c: dict) -> bool:
    url = (f"https://{c['tenant']}.{c['wd']}.myworkdayjobs.com"
           f"/wday/cxs/{c['tenant']}/{c['site']}/jobs")
    data = _request_json("POST", url, payload={
        "appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""})
    return bool(data and data.get("jobPostings"))


def _validate_greenhouse(c: dict) -> bool:
    url = f"https://boards-api.greenhouse.io/v1/boards/{c['token']}/jobs"
    data = _request_json("GET", url)
    return bool(data and data.get("jobs"))


def _validate_lever(c: dict) -> bool:
    url = f"https://api.lever.co/v0/postings/{c['slug']}?mode=json"
    data = _request_json("GET", url)
    return isinstance(data, list) and len(data) > 0


def _validate_ashby(c: dict) -> bool:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{c['slug']}"
    data = _request_json("GET", url)
    return bool(data and data.get("jobs"))


def _validate_smartrecruiters(c: dict) -> bool:
    url = f"https://api.smartrecruiters.com/v1/companies/{c['company']}/postings"
    data = _request_json("GET", url, params={"limit": 1})
    return bool(data and data.get("totalFound"))


def _validate_workable(c: dict) -> bool:
    url = f"https://apply.workable.com/api/v3/accounts/{c['slug']}/jobs"
    data = _request_json("POST", url, payload={"query": ""})
    return bool(data and data.get("total"))


@dataclass(frozen=True)
class ATSSpec:
    name: str                             # id used in --ats and reporting
    config_file: str                      # yaml file under config/
    extract: Callable[[str], list[dict]]  # text -> candidate dicts
    validate: Callable[[dict], bool]      # one API call: does this board exist?
    key: Callable[[dict], object]         # dedupe / merge identity
    cc_patterns: tuple[str, ...]          # Common Crawl URL-index queries

    @property
    def config_path(self) -> str:
        return os.path.join(_CONFIG_DIR, self.config_file)


ATS_SPECS: list[ATSSpec] = [
    ATSSpec("workday", "companies.yaml", _extract_workday, _validate_workday,
            lambda c: (c["tenant"], c["wd"], c["site"]),
            ("*.myworkdayjobs.com/*",)),
    ATSSpec("greenhouse", "greenhouse.yaml", _extract_greenhouse, _validate_greenhouse,
            lambda c: c["token"],
            ("boards.greenhouse.io/*", "job-boards.greenhouse.io/*")),
    ATSSpec("lever", "lever.yaml", _extract_lever, _validate_lever,
            lambda c: c["slug"],
            ("jobs.lever.co/*",)),
    ATSSpec("ashby", "ashby.yaml", _extract_ashby, _validate_ashby,
            lambda c: c["slug"].lower(),
            ("jobs.ashbyhq.com/*",)),
    ATSSpec("smartrecruiters", "smartrecruiters.yaml", _extract_smartrecruiters,
            _validate_smartrecruiters,
            lambda c: c["company"].lower(),
            ("jobs.smartrecruiters.com/*", "careers.smartrecruiters.com/*")),
    ATSSpec("workable", "workable.yaml", _extract_workable, _validate_workable,
            lambda c: c["slug"],
            ("apply.workable.com/*",)),
]


def _extract_all(text: str, specs: list[ATSSpec]) -> dict[str, list[dict]]:
    return {spec.name: spec.extract(text) for spec in specs}


def _merge_found(dst: dict[str, list[dict]], src: dict[str, list[dict]]) -> None:
    for ats, cands in src.items():
        dst.setdefault(ats, []).extend(cands)


# ─────────────────────────────────────────────────────────────────────────────
# Source: seeds.txt — hand-curated URLs, any supported ATS
# ─────────────────────────────────────────────────────────────────────────────

def harvest_from_seeds(specs: list[ATSSpec] = ATS_SPECS) -> dict[str, list[dict]]:
    if not os.path.exists(_SEEDS):
        return {}
    with open(_SEEDS, encoding="utf-8") as fh:
        text = "\n".join(line for line in fh if not line.lstrip().startswith("#"))
    return _extract_all(text, specs)


# ─────────────────────────────────────────────────────────────────────────────
# Source: GitHub internship/new-grad lists (READMEs + structured listings.json)
# ─────────────────────────────────────────────────────────────────────────────

_GITHUB_SOURCES = [
    # SimplifyJobs ships machine-readable listings.json next to the README —
    # it keeps entries (with apply URLs) that have rotated out of the README.
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/.github/scripts/listings.json",
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json",
    "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json",
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/README.md",
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/README.md",
    "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/README.md",
    "https://raw.githubusercontent.com/Ouckah/Summer2025-Internships/main/README.md",
    "https://raw.githubusercontent.com/ReaVNaiL/New-Grad-2024/main/README.md",
    "https://raw.githubusercontent.com/vanshb03/Summer2026-Internships/dev/README.md",
]


def harvest_from_github(specs: list[ATSSpec] = ATS_SPECS) -> dict[str, list[dict]]:
    found: dict[str, list[dict]] = {}
    for url in _GITHUB_SOURCES:
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=30)
            if resp.status_code == 404:
                continue        # repo/season doesn't exist yet — fine
            resp.raise_for_status()
        except requests.RequestException as exc:
            print(f"  GitHub source failed ({url}): {exc}")
            continue
        print(f"  GitHub: fetched {'/'.join(url.split('/')[3:5])} ({url.rsplit('/', 1)[-1]})")
        _merge_found(found, _extract_all(resp.text, specs))
    return found


# ─────────────────────────────────────────────────────────────────────────────
# Source: JobSpy — apply URLs on Indeed/Glassdoor postings point at ATS boards
# ─────────────────────────────────────────────────────────────────────────────

def harvest_from_jobspy(specs: list[ATSSpec] = ATS_SPECS) -> dict[str, list[dict]]:
    try:
        from jobspy import scrape_jobs
    except ImportError:
        print("  jobspy not installed — skipping JobSpy discovery.")
        return {}

    search_terms = [
        "software engineer intern",
        "software engineering internship",
        "new grad software engineer",
        "entry level software engineer",
    ]
    found: dict[str, list[dict]] = {}
    for term in search_terms:
        print(f"  [jobspy discovery] '{term}' ...")
        try:
            df = scrape_jobs(site_name=["indeed", "glassdoor", "zip_recruiter"],
                             search_term=term, location="United States",
                             results_wanted=100, hours_old=168,
                             country_indeed="USA", verbose=0)
        except Exception as exc:
            print(f"  [jobspy discovery] failed for {term!r}: {exc}")
            continue
        if df is None or df.empty:
            continue
        urls = []
        for col in ("job_url_direct", "job_url", "apply_url"):
            if col in df.columns:
                urls.extend(str(u) for u in df[col].dropna())
        _merge_found(found, _extract_all("\n".join(urls), specs))
    return found


# ─────────────────────────────────────────────────────────────────────────────
# Source: Common Crawl URL index — one query pattern per ATS domain
# ─────────────────────────────────────────────────────────────────────────────

# Fallback if collinfo.json is unreachable.
_CC_FALLBACK_INDEXES = [
    "https://index.commoncrawl.org/CC-MAIN-2025-18-index",
    "https://index.commoncrawl.org/CC-MAIN-2025-13-index",
]


def _cc_newest_indexes(n: int) -> list[str]:
    """Ask Common Crawl for its index list so we always use the newest crawls."""
    try:
        resp = requests.get("https://index.commoncrawl.org/collinfo.json",
                            headers=_HEADERS, timeout=30)
        resp.raise_for_status()
        return [c["cdx-api"] for c in resp.json()][:n]
    except (requests.RequestException, ValueError, KeyError) as exc:
        print(f"  collinfo.json unavailable ({exc}); using fallback index list.")
        return _CC_FALLBACK_INDEXES[:n]


def _cc_get(index_url: str, params: dict) -> requests.Response | None:
    """GET with retries. Returns the response, or None if the pattern has no
    captures (404). Raises on repeated transient failure."""
    backoff = _INITIAL_BACKOFF
    for attempt in range(_MAX_RETRIES):
        try:
            resp = requests.get(index_url, params=params, headers=_HEADERS,
                                timeout=90)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            if attempt == _MAX_RETRIES - 1:
                raise
            print(f"    request failed ({exc}); retry in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
    return None  # unreachable


def _cc_num_pages(index_url: str, pattern: str) -> int:
    resp = _cc_get(index_url, {"url": pattern, "output": "json",
                               "showNumPages": "true"})
    if resp is None:
        return 0
    try:
        return int(resp.json().get("pages", 0))
    except ValueError:
        return 0


def _fetch_cc_page(index_url: str, pattern: str, page: int) -> list[str]:
    """All URLs from one page of a CC index query.

    NOTE: the CDX server paginates with page=N (and reports the page count via
    showNumPages). It silently ignores offset=, so offset-based paging just
    re-reads the first block forever.
    """
    resp = _cc_get(index_url, {"url": pattern, "output": "json", "fl": "url",
                               "page": page})
    if resp is None:
        return []
    urls = []
    for line in resp.text.splitlines():
        if not line.strip():
            continue
        try:
            urls.append(json.loads(line).get("url", ""))
        except ValueError:
            continue
    return urls


def _load_checkpoint() -> dict:
    if not os.path.exists(_CHECKPOINT):
        return {}
    try:
        with open(_CHECKPOINT, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) and data.get("version") == 2 else {}
    except (json.JSONDecodeError, IOError):
        return {}


def _save_checkpoint(done: list[str], found: dict[str, list[dict]]) -> None:
    try:
        with open(_CHECKPOINT, "w", encoding="utf-8") as fh:
            json.dump({"version": 2, "done": done, "found": found}, fh)
    except IOError as exc:
        print(f"Warning: couldn't save checkpoint ({exc})")


def _clear_checkpoint() -> None:
    try:
        if os.path.exists(_CHECKPOINT):
            os.remove(_CHECKPOINT)
    except IOError:
        pass


def harvest_from_common_crawl(specs: list[ATSSpec] = ATS_SPECS,
                              max_pages: int = 40,
                              num_indexes: int = 3) -> dict[str, list[dict]]:
    """Query CC indexes for every ATS's URL pattern. Checkpointed per
    (index, pattern) pair, so Ctrl-C and re-run is always safe."""
    checkpoint = _load_checkpoint()
    done: list[str] = checkpoint.get("done", [])
    found: dict[str, list[dict]] = checkpoint.get("found", {})
    if done:
        print(f"Resuming CC harvest: {len(done)} index/pattern pairs already done.")

    indexes = _cc_newest_indexes(num_indexes)
    for index_url in indexes:
        index_name = index_url.rstrip("/").split("/")[-1]
        for spec in specs:
            for pattern in spec.cc_patterns:
                pair = f"{index_name}|{pattern}"
                if pair in done:
                    continue
                try:
                    num_pages = _cc_num_pages(index_url, pattern)
                except requests.RequestException as exc:
                    print(f"  [{index_name}] {pattern}: page count failed "
                          f"({exc}); skipping.")
                    continue
                fetch = min(num_pages, max_pages)
                print(f"  [{index_name}] {pattern} — {num_pages} pages"
                      + (f", capped at {max_pages}" if num_pages > max_pages else ""))
                for page in range(fetch):
                    try:
                        urls = _fetch_cc_page(index_url, pattern, page)
                    except requests.RequestException as exc:
                        print(f"    page {page} permanently failed ({exc}); "
                              f"moving on.")
                        continue
                    cands = spec.extract("\n".join(urls))
                    found.setdefault(spec.name, []).extend(cands)
                    time.sleep(0.5)
                done.append(pair)
                _save_checkpoint(done, found)
    return found


# ─────────────────────────────────────────────────────────────────────────────
# Validate + merge into per-ATS config files
# ─────────────────────────────────────────────────────────────────────────────

def _dedupe(spec: ATSSpec, cands: list[dict]) -> list[dict]:
    seen, out = set(), []
    for c in cands:
        k = spec.key(c)
        if k not in seen:
            seen.add(k)
            out.append(c)
    return out


def _load_existing(spec: ATSSpec) -> tuple[list[dict], set]:
    if not os.path.exists(spec.config_path):
        return [], set()
    data = yaml.safe_load(open(spec.config_path, encoding="utf-8")) or {}
    companies = data.get("companies", []) or []
    return companies, {spec.key(c) for c in companies}


def _save_config(spec: ATSSpec, companies: list[dict]) -> None:
    with open(spec.config_path, "w", encoding="utf-8") as fh:
        yaml.safe_dump({"companies": companies}, fh, sort_keys=False,
                       allow_unicode=True)


def validate_and_merge(spec: ATSSpec, candidates: list[dict]) -> int:
    """Validate new candidates concurrently; merge survivors into the config."""
    existing, existing_keys = _load_existing(spec)
    todo = [c for c in _dedupe(spec, candidates) if spec.key(c) not in existing_keys]
    if not todo:
        print(f"  [{spec.name}] nothing new to validate "
              f"({len(existing)} already configured).")
        return 0

    print(f"  [{spec.name}] validating {len(todo)} new candidates ...")
    added = 0
    with ThreadPoolExecutor(max_workers=_VALIDATE_WORKERS) as pool:
        for cand, ok in zip(todo, pool.map(spec.validate, todo)):
            if not ok:
                continue
            existing.append(cand)
            existing_keys.add(spec.key(cand))
            added += 1
            print(f"    + [{spec.name}] {cand.get('name')}")
    _save_config(spec, existing)
    print(f"  [{spec.name}] added {added}, total {len(existing)}.")
    return added


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--seeds-only", action="store_true",
                    help="only harvest from config/seeds.txt")
    ap.add_argument("--no-github", action="store_true", help="skip GitHub lists")
    ap.add_argument("--no-jobspy", action="store_true", help="skip JobSpy harvest")
    ap.add_argument("--no-cc", action="store_true", help="skip Common Crawl")
    ap.add_argument("--ats", default="",
                    help="comma-separated ATS subset, e.g. greenhouse,ashby")
    ap.add_argument("--cc-max-pages", type=int, default=40,
                    help="max CDX pages per index/pattern pair (a page is a "
                         "multi-thousand-URL block)")
    ap.add_argument("--cc-indexes", type=int, default=3,
                    help="how many of the newest CC indexes to query")
    args = ap.parse_args()

    specs = ATS_SPECS
    if args.ats:
        wanted = {s.strip().lower() for s in args.ats.split(",") if s.strip()}
        unknown = wanted - {s.name for s in ATS_SPECS}
        if unknown:
            print(f"Unknown ATS name(s): {', '.join(sorted(unknown))}. "
                  f"Known: {', '.join(s.name for s in ATS_SPECS)}")
            return 1
        specs = [s for s in ATS_SPECS if s.name in wanted]

    found: dict[str, list[dict]] = {s.name: [] for s in specs}

    print("Harvesting from seeds.txt ...")
    _merge_found(found, harvest_from_seeds(specs))

    if not args.seeds_only and not args.no_github:
        print("Harvesting from GitHub job lists ...")
        _merge_found(found, harvest_from_github(specs))

    if not args.seeds_only and not args.no_jobspy:
        print("Harvesting ATS URLs from job boards via JobSpy ...")
        _merge_found(found, harvest_from_jobspy(specs))

    if not args.seeds_only and not args.no_cc:
        print("Harvesting from Common Crawl ...")
        _merge_found(found, harvest_from_common_crawl(
            specs, max_pages=args.cc_max_pages, num_indexes=args.cc_indexes))

    print("\nRaw candidates per ATS:")
    for spec in specs:
        uniq = len(_dedupe(spec, found.get(spec.name, [])))
        print(f"  {spec.name:<16} {len(found.get(spec.name, [])):>7} raw "
              f"/ {uniq} unique")

    print("\nValidating and merging into config files ...")
    totals = {}
    for spec in specs:
        totals[spec.name] = validate_and_merge(spec, found.get(spec.name, []))

    _clear_checkpoint()
    summary = "  ".join(f"{k} +{v}" for k, v in totals.items())
    print(f"\nDone. {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
