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
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from .ats_specs import (ATS_SPECS, HEADERS, INITIAL_BACKOFF, MAX_RETRIES, ATSSpec,
                        dedupe_candidates, extract_all, load_existing, request_json, save_config)
from .config import CONFIG_DIR, ROOT

log = logging.getLogger(__name__)

_SEEDS = os.path.join(CONFIG_DIR, "seeds.txt")
_CHECKPOINT = os.path.join(ROOT, ".discover_checkpoint.json")

_VALIDATE_WORKERS = 8


# ─────────────────────────────────────────────────────────────────────────────
# Shared HTTP helpers (retry on 429/5xx/network, give up fast on 4xx)
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# ATS registry: URL patterns, Common Crawl queries, and validators per ATS
# ─────────────────────────────────────────────────────────────────────────────


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
    return extract_all(text, specs)


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
    texts: list[str] = []
    for url in _GITHUB_SOURCES:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            if resp.status_code == 404:
                continue        # repo/season doesn't exist yet — fine
            resp.raise_for_status()
        except requests.RequestException as exc:
            log.info(f"  GitHub source failed ({url}): {exc}")
            continue
        log.info(f"  GitHub: fetched {'/'.join(url.split('/')[3:5])} ({url.rsplit('/', 1)[-1]})")
        _merge_found(found, extract_all(resp.text, specs))
        texts.append(resp.text)
    if any(s.name == "greenhouse" for s in specs):
        _merge_found(found, {"greenhouse": resolve_hidden_greenhouse("\n".join(texts))})
    return found


# Greenhouse jobs linked without their board's name: on the company's own
# site (careers.aqr.com/jobs?gh_jid=123) or as an embed (boards.greenhouse.io/
# embed/job_app?token=123). The page itself names the board ("...?for=aqr").
_GH_HIDDEN = re.compile(
    r"https?://[^\s\"'<>\\]+?(?:[?&]gh_jid=|greenhouse\.io/embed/job_app\?(?:[^\s\"'<>]*&)?"
    r"token=)(\d+)", re.I)
_GH_TOKEN_IN_PAGE = re.compile(
    r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_(?:board|app)(?:/js)?\?"
    r"(?:[^\"'<>\s]*&)?for=)?([a-z0-9_-]+)|boards-api\.greenhouse\.io/v1/boards/([a-z0-9_-]+)"
    r"|[?&]for=([a-z0-9_-]+)", re.I)
_GH_NOT_TOKENS = {"embed", "v1", "boards", "job_app", "job_board", "js"}


def _embed_token(job_id: str) -> dict | None:
    """Greenhouse's own embed page for a job id redirects to
    ...?for={token}&token={id} while the job is open (404 once closed).
    Works when the company's page is rendered by JavaScript
    (careers.withwaymo.com) and names no board in its HTML."""
    try:
        resp = requests.get(f"https://boards.greenhouse.io/embed/job_app?token={job_id}",
                            headers=HEADERS, timeout=25, allow_redirects=True)
    except requests.RequestException:
        return None
    m = re.search(r"[?&]for=([A-Za-z0-9_-]+)", resp.url)
    if not m:
        return None
    token = m.group(1).lower()
    board = request_json("GET", f"https://boards-api.greenhouse.io/v1/boards/{token}")
    return {"token": token, "name": ((board or {}).get("name") or "").strip() or token}


def _resolve_one(url: str, job_id: str) -> dict | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=25, allow_redirects=True)
    except requests.RequestException:
        return None
    tokens = {t.lower() for m in _GH_TOKEN_IN_PAGE.findall(resp.url + " " + resp.text)
              for t in m if t and t.lower() not in _GH_NOT_TOKENS}
    for token in sorted(tokens):
        data = request_json("GET", f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs")
        # Only a board that actually lists this job counts.
        if data and any(str(j.get("id")) == job_id for j in data.get("jobs", [])):
            return {"token": token, "name": token}
    return None


def _resolve_site(links: list[tuple[str, str]], tries: int = 5) -> dict | None:
    """One company site's board: the embed redirect for up to `tries` of its
    job ids, newest (highest) first, since curated lists keep closed jobs;
    then the company page itself."""
    for _, job_id in sorted(links, key=lambda t: -int(t[1]))[:tries]:
        found = _embed_token(job_id)
        if found:
            return found
    return _resolve_one(*links[0])


def resolve_hidden_greenhouse(text: str, max_pages: int = 400) -> list[dict]:
    """Board tokens for Greenhouse jobs linked without one, per company site
    (embeds, which name no site, count one per job)."""
    sites: dict[str, list[tuple[str, str]]] = {}
    for m in _GH_HIDDEN.finditer(text):
        url, job_id = m.group(0), m.group(1)
        site = url if "embed/job_app" in url else url.split("/")[2].lower()
        sites.setdefault(site, []).append((url, job_id))
    todo = list(sites.values())[:max_pages]
    log.info(f"  Greenhouse: resolving {len(todo)} boards hidden behind company pages / embeds ...")
    with ThreadPoolExecutor(max_workers=_VALIDATE_WORKERS) as pool:
        found = [r for r in pool.map(_resolve_site, todo) if r]
    log.info(f"  Greenhouse: resolved {len({f['token'] for f in found})} board tokens")
    return found


# ─────────────────────────────────────────────────────────────────────────────
# Source: JobSpy — apply URLs on Indeed/Glassdoor postings point at ATS boards
# ─────────────────────────────────────────────────────────────────────────────

def harvest_from_jobspy(specs: list[ATSSpec] = ATS_SPECS) -> dict[str, list[dict]]:
    try:
        from jobspy import scrape_jobs
    except ImportError:
        log.info("  jobspy not installed — skipping JobSpy discovery.")
        return {}

    search_terms = [
        "software engineer intern",
        "software engineering internship",
        "new grad software engineer",
        "entry level software engineer",
    ]
    found: dict[str, list[dict]] = {}
    for term in search_terms:
        log.info(f"  [jobspy discovery] '{term}' ...")
        try:
            df = scrape_jobs(site_name=["indeed", "glassdoor", "zip_recruiter"],
                             search_term=term, location="United States",
                             results_wanted=100, hours_old=168,
                             country_indeed="USA", verbose=0)
        except Exception as exc:
            log.info(f"  [jobspy discovery] failed for {term!r}: {exc}")
            continue
        if df is None or df.empty:
            continue
        urls = []
        for col in ("job_url_direct", "job_url", "apply_url"):
            if col in df.columns:
                urls.extend(str(u) for u in df[col].dropna())
        _merge_found(found, extract_all("\n".join(urls), specs))
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
                            headers=HEADERS, timeout=30)
        resp.raise_for_status()
        return [c["cdx-api"] for c in resp.json()][:n]
    except (requests.RequestException, ValueError, KeyError) as exc:
        log.info(f"  collinfo.json unavailable ({exc}); using fallback index list.")
        return _CC_FALLBACK_INDEXES[:n]


def _cc_get(index_url: str, params: dict) -> requests.Response | None:
    """GET with retries. Returns the response, or None if the pattern has no
    captures (404). Raises on repeated transient failure."""
    backoff = INITIAL_BACKOFF
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(index_url, params=params, headers=HEADERS,
                                timeout=90)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            if attempt == MAX_RETRIES - 1:
                raise
            log.info(f"    request failed ({exc}); retry in {backoff}s")
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
        log.info(f"Warning: couldn't save checkpoint ({exc})")


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
        log.info(f"Resuming CC harvest: {len(done)} index/pattern pairs already done.")

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
                    log.info(f"  [{index_name}] {pattern}: page count failed "
                          f"({exc}); skipping.")
                    continue
                fetch = min(num_pages, max_pages)
                log.info(f"  [{index_name}] {pattern} — {num_pages} pages"
                      + (f", capped at {max_pages}" if num_pages > max_pages else ""))
                for page in range(fetch):
                    try:
                        urls = _fetch_cc_page(index_url, pattern, page)
                    except requests.RequestException as exc:
                        log.info(f"    page {page} permanently failed ({exc}); "
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


def validate_and_merge(spec: ATSSpec, candidates: list[dict]) -> int:
    """Validate new candidates concurrently; merge survivors into the config."""
    existing, existing_keys = load_existing(spec)
    todo = [c for c in dedupe_candidates(spec, candidates) if spec.key(c) not in existing_keys]
    if not todo:
        log.info(f"  [{spec.name}] nothing new to validate "
              f"({len(existing)} already configured).")
        return 0

    log.info(f"  [{spec.name}] validating {len(todo)} new candidates ...")
    added = 0
    with ThreadPoolExecutor(max_workers=_VALIDATE_WORKERS) as pool:
        for cand, ok in zip(todo, pool.map(spec.validate, todo)):
            if not ok:
                continue
            existing.append(cand)
            existing_keys.add(spec.key(cand))
            added += 1
            log.info(f"    + [{spec.name}] {cand.get('name')}")
    save_config(spec, existing)
    log.info(f"  [{spec.name}] added {added}, total {len(existing)}.")
    return added


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    from .config import setup_logging
    setup_logging()
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
            log.info(f"Unknown ATS name(s): {', '.join(sorted(unknown))}. "
                  f"Known: {', '.join(s.name for s in ATS_SPECS)}")
            return 1
        specs = [s for s in ATS_SPECS if s.name in wanted]

    found: dict[str, list[dict]] = {s.name: [] for s in specs}

    log.info("Harvesting from seeds.txt ...")
    _merge_found(found, harvest_from_seeds(specs))

    if not args.seeds_only and not args.no_github:
        log.info("Harvesting from GitHub job lists ...")
        _merge_found(found, harvest_from_github(specs))

    if not args.seeds_only and not args.no_jobspy:
        log.info("Harvesting ATS URLs from job boards via JobSpy ...")
        _merge_found(found, harvest_from_jobspy(specs))

    if not args.seeds_only and not args.no_cc:
        log.info("Harvesting from Common Crawl ...")
        _merge_found(found, harvest_from_common_crawl(
            specs, max_pages=args.cc_max_pages, num_indexes=args.cc_indexes))

    log.info("\nRaw candidates per ATS:")
    for spec in specs:
        uniq = len(dedupe_candidates(spec, found.get(spec.name, [])))
        log.info(f"  {spec.name:<16} {len(found.get(spec.name, [])):>7} raw "
              f"/ {uniq} unique")

    log.info("\nValidating and merging into config files ...")
    totals = {}
    for spec in specs:
        totals[spec.name] = validate_and_merge(spec, found.get(spec.name, []))

    _clear_checkpoint()
    summary = "  ".join(f"{k} +{v}" for k, v in totals.items())
    log.info(f"\nDone. {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
