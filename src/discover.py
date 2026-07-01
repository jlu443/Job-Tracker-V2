"""Discover Workday tenants and write the validated ones to companies.yaml.

There is no public registry of Workday tenants, so this is a harvest+validate
pipeline:

  1. Harvest candidate careers URLs from the Common Crawl URL index (free, broad)
     and from config/seeds.txt (hand-curated head start).
  2. Parse each into (tenant, wd, site).
  3. Validate by calling the CXS jobs endpoint with limit=1 — keep only the ones
     that return jobs.
  4. Merge survivors into config/companies.yaml (existing entries preserved).

Run occasionally (it's slow); the per-run scraper just reads companies.yaml.

    python -m src.discover                 # GitHub repos + Common Crawl + seeds
    python -m src.discover --seeds-only    # skip Common Crawl and GitHub
    python -m src.discover --no-github     # skip GitHub only
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

import requests
import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_COMPANIES    = os.path.join(_ROOT, "config", "companies.yaml")
_GH_COMPANIES = os.path.join(_ROOT, "config", "greenhouse.yaml")
_LV_COMPANIES = os.path.join(_ROOT, "config", "lever.yaml")
_SEEDS        = os.path.join(_ROOT, "config", "seeds.txt")
_CHECKPOINT   = os.path.join(_ROOT, ".discover_checkpoint.json")

_HEADERS = {"User-Agent": "Mozilla/5.0 (job-tracker-discover)"}
_MAX_RETRIES = 5
_INITIAL_BACKOFF = 2

# Captures tenant, wd, and site from a Workday careers URL.
_URL_RE = re.compile(
    r"https?://(?P<tenant>[a-z0-9-]+)\.(?P<wd>wd\d+)\.myworkdayjobs\.com"
    r"/(?:[a-z]{2}-[A-Z]{2}/)?(?P<site>[A-Za-z0-9_-]+)",
    re.IGNORECASE,
)
# Greenhouse: boards.greenhouse.io/{token} or boards-api.greenhouse.io/v1/boards/{token}
_GH_RE = re.compile(
    r"boards(?:-api)?\.greenhouse\.io(?:/v1/boards)?/([a-z0-9_-]+)",
    re.IGNORECASE,
)
# Lever: jobs.lever.co/{slug}
_LV_RE = re.compile(
    r"jobs\.lever\.co/([a-z0-9_-]+)",
    re.IGNORECASE,
)

# Common Crawl indexes to query — newest first. Each covers ~1-2 months of crawl data.
_CC_INDEXES = [
    "https://index.commoncrawl.org/CC-MAIN-2025-18-index",  # Apr/May 2025
    "https://index.commoncrawl.org/CC-MAIN-2025-13-index",  # Mar 2025
    "https://index.commoncrawl.org/CC-MAIN-2025-08-index",  # Feb 2025
    "https://index.commoncrawl.org/CC-MAIN-2024-51-index",  # Dec 2024
    "https://index.commoncrawl.org/CC-MAIN-2024-46-index",  # Nov 2024
]


def _parse_url(url: str) -> dict | None:
    m = _URL_RE.search(url)
    if not m:
        return None
    site = m.group("site")
    # Skip Workday's own asset/path segments that aren't career sites.
    if site.lower() in {"wday", "cxs", "assets", "static"}:
        return None
    return {"tenant": m.group("tenant").lower(), "wd": m.group("wd").lower(),
            "site": site}


def _load_checkpoint() -> list[dict]:
    """Load candidates from checkpoint if it exists."""
    if not os.path.exists(_CHECKPOINT):
        return []
    try:
        with open(_CHECKPOINT, encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, IOError):
        return []


def _save_checkpoint(candidates: list[dict]) -> None:
    """Save candidates to checkpoint file."""
    try:
        with open(_CHECKPOINT, "w", encoding="utf-8") as fh:
            json.dump(candidates, fh)
    except IOError as e:
        print(f"Warning: couldn't save checkpoint ({e})")


def _clear_checkpoint() -> None:
    """Delete checkpoint file after successful completion."""
    try:
        if os.path.exists(_CHECKPOINT):
            os.remove(_CHECKPOINT)
    except IOError:
        pass


# GitHub repos with curated internship/new-grad Workday links.
# Raw content URLs — these are markdown files with apply links embedded.
_GITHUB_SOURCES = [
    # SimplifyJobs internship lists
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2025-Internships/dev/README.md",
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/README.md",
    "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/README.md",
    # Pittsburg CSC repo - another popular list
    "https://raw.githubusercontent.com/ReaVNaiL/New-Grad-2024/main/README.md",
    "https://raw.githubusercontent.com/Ouckah/Summer2025-Internships/main/README.md",
]


def _fetch_github_text() -> list[str]:
    """Fetch raw text from all GitHub sources. Returns list of page texts."""
    texts = []
    for url in _GITHUB_SOURCES:
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=30)
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            texts.append(resp.text)
            print(f"  GitHub: fetched {url.split('/')[-3]}/{url.split('/')[-2]}")
        except requests.RequestException as exc:
            print(f"  GitHub source failed ({url}): {exc}")
    return texts


def harvest_from_github() -> list[dict]:
    """Extract Workday URLs from curated GitHub repos."""
    out = []
    for text in _fetch_github_text():
        for match in _URL_RE.finditer(text):
            site = match.group("site")
            if site.lower() in {"wday", "cxs", "assets", "static"}:
                continue
            out.append({
                "tenant": match.group("tenant").lower(),
                "wd": match.group("wd").lower(),
                "site": site,
            })
    print(f"  GitHub Workday candidates: {len(out)}")
    return out


def harvest_greenhouse_from_github() -> list[dict]:
    """Extract Greenhouse tokens from curated GitHub repos."""
    seen, out = set(), []
    for text in _fetch_github_text():
        for match in _GH_RE.finditer(text):
            token = match.group(1).lower()
            if token not in seen and token not in {"v1", "boards", "jobs"}:
                seen.add(token)
                out.append({"token": token, "name": token})
    print(f"  GitHub Greenhouse candidates: {len(out)}")
    return out


def harvest_lever_from_github() -> list[dict]:
    """Extract Lever slugs from curated GitHub repos."""
    seen, out = set(), []
    for text in _fetch_github_text():
        for match in _LV_RE.finditer(text):
            slug = match.group(1).lower()
            if slug not in seen:
                seen.add(slug)
                out.append({"slug": slug, "name": slug})
    print(f"  GitHub Lever candidates: {len(out)}")
    return out


def _validate_greenhouse(token: str, timeout: int = 20) -> bool:
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=timeout)
        if resp.status_code != 200:
            return False
        return bool(resp.json().get("jobs"))
    except (requests.RequestException, ValueError):
        return False


def _validate_lever(slug: str, timeout: int = 20) -> bool:
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=timeout)
        if resp.status_code != 200:
            return False
        data = resp.json()
        return isinstance(data, list) and len(data) > 0
    except (requests.RequestException, ValueError):
        return False


def _load_existing_yaml(path: str) -> tuple[list[dict], set]:
    if not os.path.exists(path):
        return [], set()
    existing = yaml.safe_load(open(path, encoding="utf-8")) or {}
    companies = existing.get("companies", [])
    return companies, {c.get("token") or c.get("slug") for c in companies}


def _save_yaml(path: str, companies: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump({"companies": companies}, fh, sort_keys=False, allow_unicode=True)


def harvest_from_jobspy() -> list[dict]:
    """Search Indeed/Glassdoor/ZipRecruiter for intern/new-grad roles and extract
    any Workday apply URLs — these are verified tenant+site combos by definition."""
    try:
        from jobspy import scrape_jobs
    except ImportError:
        print("  jobspy not installed — skipping JobSpy discovery.")
        return []

    search_terms = [
        "software engineer intern",
        "software engineering internship",
        "new grad software engineer",
        "entry level software engineer",
    ]
    sites = ["indeed", "glassdoor", "zip_recruiter"]
    out = []

    for term in search_terms:
        print(f"  [jobspy discovery] '{term}' ...")
        try:
            df = scrape_jobs(
                site_name=sites,
                search_term=term,
                location="United States",
                results_wanted=100,
                hours_old=168,  # last week
                country_indeed="USA",
                verbose=0,
            )
        except Exception as exc:
            print(f"  [jobspy discovery] failed for {term!r}: {exc}")
            continue

        if df is None or df.empty:
            continue

        for _, row in df.iterrows():
            for col in ("job_url_direct", "job_url", "apply_url"):
                url = str(row.get(col) or "")
                if "myworkdayjobs.com" in url:
                    parsed = _parse_url(url)
                    if parsed:
                        out.append(parsed)
                        break

    print(f"  [jobspy discovery] {len(out)} Workday URLs extracted.")
    return out


def harvest_from_seeds() -> list[dict]:
    if not os.path.exists(_SEEDS):
        return []
    out = []
    with open(_SEEDS, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parsed = _parse_url(line)
            if parsed:
                out.append(parsed)
    return out


_CC_PAGE_SIZE = 500


def _fetch_cc_page(index_url: str, offset: int) -> tuple[list[dict], bool]:
    """Fetch one page from a Common Crawl index. Returns (parsed_candidates, has_more)."""
    params = {
        "url": "*.myworkdayjobs.com/*",
        "output": "json",
        "limit": _CC_PAGE_SIZE,
        "offset": offset,
    }
    backoff = _INITIAL_BACKOFF
    for attempt in range(_MAX_RETRIES):
        try:
            resp = requests.get(index_url, params=params, headers=_HEADERS,
                                timeout=60, stream=True)
            resp.raise_for_status()
            lines = []
            for line in resp.iter_lines():
                if line:
                    lines.append(line)
            out = []
            for line in lines:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                parsed = _parse_url(rec.get("url", ""))
                if parsed:
                    out.append(parsed)
            has_more = len(lines) == _CC_PAGE_SIZE
            return out, has_more
        except requests.RequestException as exc:
            if attempt == _MAX_RETRIES - 1:
                raise
            print(f"    Page at offset {offset} failed ({exc}). Retrying in {backoff}s...")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
    return [], False  # unreachable


def _harvest_one_index(index_url: str, start_offset: int, out: list[dict]) -> None:
    """Harvest all pages from one CC index starting at start_offset."""
    offset = start_offset
    index_name = index_url.split("/")[-1]
    while True:
        print(f"  [{index_name}] offset {offset}...")
        has_more = True
        try:
            page_candidates, has_more = _fetch_cc_page(index_url, offset)
            out.extend(page_candidates)
        except requests.RequestException as exc:
            print(f"  [{index_name}] offset {offset} permanently failed ({exc}). Skipping page.")

        offset += _CC_PAGE_SIZE

        if not has_more:
            print(f"  [{index_name}] done. Total candidates so far: {len(out)}")
            return

        time.sleep(0.5)


def harvest_from_common_crawl() -> list[dict]:
    """Query multiple Common Crawl indexes for *.myworkdayjobs.com URLs, paginated."""
    checkpoint = _load_checkpoint()
    if isinstance(checkpoint, dict):
        done_indexes = checkpoint.get("done_indexes", [])
        current_index = checkpoint.get("current_index", 0)
        start_offset = checkpoint.get("offset", 0)
        out = checkpoint.get("candidates", [])
        print(f"Resuming from checkpoint: index {current_index}, offset={start_offset}, {len(out)} candidates so far.")
    else:
        done_indexes, current_index, start_offset, out = [], 0, 0, []

    for i, index_url in enumerate(_CC_INDEXES):
        if i < current_index:
            continue
        if index_url in done_indexes:
            continue
        index_name = index_url.split("/")[-1]
        print(f"\nHarvesting from {index_name} ...")
        offset = start_offset if i == current_index else 0
        _harvest_one_index(index_url, offset, out)
        done_indexes.append(index_url)
        start_offset = 0
        _save_checkpoint({"done_indexes": done_indexes, "current_index": i + 1,
                          "offset": 0, "candidates": out})

    print(f"\nAll indexes harvested. Total raw candidates: {len(out)}")
    return out


def _validate(cand: dict, timeout: int = 30) -> bool:
    endpoint = (f"https://{cand['tenant']}.{cand['wd']}.myworkdayjobs.com"
                f"/wday/cxs/{cand['tenant']}/{cand['site']}/jobs")
    payload = {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""}
    headers = {**_HEADERS, "Content-Type": "application/json"}
    backoff = _INITIAL_BACKOFF
    for attempt in range(_MAX_RETRIES):
        try:
            resp = requests.post(endpoint, json=payload, headers=headers, timeout=timeout)
            # 4xx (except 429) = wrong tenant/site — no point retrying
            if resp.status_code in (400, 404, 410, 422):
                return False
            # 429 or 5xx — transient, retry with backoff
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt < _MAX_RETRIES - 1:
                    retry_after = float(resp.json().get("retry_after", backoff)) \
                        if resp.status_code == 429 else backoff
                    time.sleep(retry_after)
                    backoff = min(backoff * 2, 60)
                    continue
                return False
            if resp.status_code != 200:
                return False
            return bool(resp.json().get("jobPostings"))
        except (requests.RequestException, ValueError):
            if attempt < _MAX_RETRIES - 1:
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            return False
    return False


def _dedupe(cands: list[dict]) -> list[dict]:
    seen, out = set(), []
    for c in cands:
        key = (c["tenant"], c["wd"], c["site"])
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds-only", action="store_true",
                    help="skip Common Crawl, GitHub, and JobSpy harvests")
    ap.add_argument("--no-github", action="store_true",
                    help="skip GitHub harvest")
    ap.add_argument("--no-cc", action="store_true",
                    help="skip Common Crawl harvest")
    ap.add_argument("--no-jobspy", action="store_true",
                    help="skip JobSpy harvest")
    args = ap.parse_args()

    # ── Workday discovery ────────────────────────────────────────────────────
    candidates = harvest_from_seeds()

    if not args.seeds_only and not args.no_github:
        print("Harvesting from GitHub internship/new-grad repos ...")
        candidates += harvest_from_github()

    if not args.seeds_only and not args.no_jobspy:
        print("Harvesting Workday URLs from job boards via JobSpy ...")
        candidates += harvest_from_jobspy()

    if not args.seeds_only and not args.no_cc:
        print("Harvesting candidates from Common Crawl ...")
        candidates += harvest_from_common_crawl()

    candidates = _dedupe(candidates)
    print(f"\n{len(candidates)} unique Workday candidate tenants. Validating ...")

    wd_companies, wd_keys = _load_existing_yaml(_COMPANIES)
    wd_added = 0
    for c in candidates:
        key = (c["tenant"], c["wd"], c["site"])
        if key in wd_keys:
            continue
        if _validate(c):
            c["name"] = c["tenant"]
            wd_companies.append(c)
            wd_keys.add(key)
            wd_added += 1
            print(f"  + [workday] {c['tenant']} / {c['site']}")
        time.sleep(0.3)

    _save_yaml(_COMPANIES, wd_companies)
    print(f"Workday: added {wd_added}, total {len(wd_companies)}.")

    # ── Greenhouse discovery ─────────────────────────────────────────────────
    gh_candidates: list[dict] = []
    if not args.seeds_only and not args.no_github:
        print("\nHarvesting Greenhouse tokens from GitHub repos ...")
        gh_candidates += harvest_greenhouse_from_github()

    gh_companies, gh_keys = _load_existing_yaml(_GH_COMPANIES)
    gh_added = 0
    for c in gh_candidates:
        if c["token"] in gh_keys:
            continue
        if _validate_greenhouse(c["token"]):
            gh_companies.append(c)
            gh_keys.add(c["token"])
            gh_added += 1
            print(f"  + [greenhouse] {c['token']}")
        time.sleep(0.3)

    _save_yaml(_GH_COMPANIES, gh_companies)
    print(f"Greenhouse: added {gh_added}, total {len(gh_companies)}.")

    # ── Lever discovery ──────────────────────────────────────────────────────
    lv_candidates: list[dict] = []
    if not args.seeds_only and not args.no_github:
        print("\nHarvesting Lever slugs from GitHub repos ...")
        lv_candidates += harvest_lever_from_github()

    lv_companies, lv_keys = _load_existing_yaml(_LV_COMPANIES)
    lv_added = 0
    for c in lv_candidates:
        if c["slug"] in lv_keys:
            continue
        if _validate_lever(c["slug"]):
            lv_companies.append(c)
            lv_keys.add(c["slug"])
            lv_added += 1
            print(f"  + [lever] {c['slug']}")
        time.sleep(0.3)

    _save_yaml(_LV_COMPANIES, lv_companies)
    print(f"Lever: added {lv_added}, total {len(lv_companies)}.")

    _clear_checkpoint()
    print(f"\nDone. Workday +{wd_added}  Greenhouse +{gh_added}  Lever +{lv_added}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
