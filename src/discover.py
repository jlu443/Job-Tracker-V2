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

    python -m src.discover                 # Common Crawl + seeds
    python -m src.discover --seeds-only    # skip Common Crawl
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time

import requests
import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_COMPANIES = os.path.join(_ROOT, "config", "companies.yaml")
_SEEDS = os.path.join(_ROOT, "config", "seeds.txt")

_HEADERS = {"User-Agent": "Mozilla/5.0 (job-tracker-discover)"}

# Captures tenant, wd, and site from a careers URL.
#   https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite
_URL_RE = re.compile(
    r"https?://(?P<tenant>[a-z0-9-]+)\.(?P<wd>wd\d+)\.myworkdayjobs\.com"
    r"/(?:[a-z]{2}-[A-Z]{2}/)?(?P<site>[A-Za-z0-9_-]+)",
    re.IGNORECASE,
)

# Common Crawl index — newest index id changes over time; this endpoint
# redirects/serves the collection list. We query one recent index.
_CC_INDEX = "https://index.commoncrawl.org/CC-MAIN-2024-51-index"


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


def harvest_from_common_crawl() -> list[dict]:
    """Query the Common Crawl index for *.myworkdayjobs.com URLs."""
    params = {"url": "*.myworkdayjobs.com/*", "output": "json"}
    try:
        resp = requests.get(_CC_INDEX, params=params, headers=_HEADERS,
                            timeout=120, stream=True)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"Common Crawl query failed ({exc}). Falling back to seeds only.")
        return []

    out = []
    for line in resp.iter_lines():
        if not line:
            continue
        try:
            import json
            rec = json.loads(line)
        except ValueError:
            continue
        parsed = _parse_url(rec.get("url", ""))
        if parsed:
            out.append(parsed)
    return out


def _validate(cand: dict, timeout: int = 30) -> bool:
    endpoint = (f"https://{cand['tenant']}.{cand['wd']}.myworkdayjobs.com"
                f"/wday/cxs/{cand['tenant']}/{cand['site']}/jobs")
    payload = {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""}
    try:
        resp = requests.post(endpoint, json=payload, headers={
            **_HEADERS, "Content-Type": "application/json"}, timeout=timeout)
        if resp.status_code != 200:
            return False
        return bool(resp.json().get("jobPostings"))
    except (requests.RequestException, ValueError):
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
                    help="skip the Common Crawl harvest")
    args = ap.parse_args()

    candidates = harvest_from_seeds()
    if not args.seeds_only:
        print("Harvesting candidates from Common Crawl ...")
        candidates += harvest_from_common_crawl()

    candidates = _dedupe(candidates)
    print(f"{len(candidates)} unique candidate tenants. Validating ...")

    existing = yaml.safe_load(open(_COMPANIES, encoding="utf-8")) or {}
    existing_companies = existing.get("companies", [])
    existing_keys = {(c["tenant"], c["wd"], c["site"]) for c in existing_companies}

    added = 0
    for c in candidates:
        key = (c["tenant"], c["wd"], c["site"])
        if key in existing_keys:
            continue
        if _validate(c):
            c["name"] = c["tenant"]  # default; edit by hand for a nicer label
            existing_companies.append(c)
            existing_keys.add(key)
            added += 1
            print(f"  + {c['tenant']} / {c['site']}")
        time.sleep(0.3)

    existing["companies"] = existing_companies
    with open(_COMPANIES, "w", encoding="utf-8") as fh:
        yaml.safe_dump(existing, fh, sort_keys=False, allow_unicode=True)

    print(f"\nAdded {added} new validated companies. "
          f"Total: {len(existing_companies)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
