"""Entry point: scrape every configured company, classify, persist, announce.

    python -m src.main          # from the repo root
"""

from __future__ import annotations

import os
import sys

import yaml

from . import (classify, db, greenhouse_scraper, jobspy_scraper,
               lever_scraper, notify, scraper, sheets)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DB_PATH        = os.path.join(_ROOT, "data", "jobs.db")
_COMPANIES      = os.path.join(_ROOT, "config", "companies.yaml")
_GH_COMPANIES   = os.path.join(_ROOT, "config", "greenhouse.yaml")
_LV_COMPANIES   = os.path.join(_ROOT, "config", "lever.yaml")
_SETTINGS       = os.path.join(_ROOT, "config", "settings.yaml")


def _load_yaml(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def main() -> int:
    settings    = _load_yaml(_SETTINGS)
    wd_cos      = _load_yaml(_COMPANIES).get("companies", [])
    gh_cos      = _load_yaml(_GH_COMPANIES).get("companies", [])
    lv_cos      = _load_yaml(_LV_COMPANIES).get("companies", [])

    if not wd_cos and not gh_cos and not lv_cos:
        print("No companies configured in any config file.")
        return 1

    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = db.connect(_DB_PATH)

    all_postings: list = []

    # --- Workday ---
    print(f"=== Workday ({len(wd_cos)} companies) ===")
    for company in wd_cos:
        name = company.get("name", company["tenant"])
        print(f"Scraping {name} ...")
        postings = scraper.fetch_company_jobs(company, settings)
        print(f"  {len(postings)} postings")
        all_postings.extend(postings)

    # --- Greenhouse ---
    print(f"\n=== Greenhouse ({len(gh_cos)} companies) ===")
    for company in gh_cos:
        name = company.get("name", company["token"])
        print(f"Scraping {name} ...")
        postings = greenhouse_scraper.fetch_company_jobs(company, settings)
        print(f"  {len(postings)} postings")
        all_postings.extend(postings)

    # --- Lever ---
    print(f"\n=== Lever ({len(lv_cos)} companies) ===")
    for company in lv_cos:
        name = company.get("name", company["slug"])
        print(f"Scraping {name} ...")
        postings = lever_scraper.fetch_company_jobs(company, settings)
        print(f"  {len(postings)} postings")
        all_postings.extend(postings)

    # --- External job boards (Indeed, Glassdoor, ZipRecruiter) ---
    print("\n=== External job boards ===")
    all_postings.extend(jobspy_scraper.fetch_jobs(settings))

    print(f"\nTotal postings this run: {len(all_postings)}")

    result = db.sync(
        conn,
        all_postings,
        role_for=lambda p: classify.classify(p.title, settings),
    )

    print(f"New: {len(result.new_jobs)}  Updated: {result.updated}  "
          f"Removed: {result.removed}")

    notify.post_new_jobs(result.new_jobs)
    sheets.post_new_jobs(result.new_jobs)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
