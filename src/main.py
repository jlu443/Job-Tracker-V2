"""Entry point: scrape every configured company, classify, persist, announce.

    python -m src.main          # from the repo root
"""

from __future__ import annotations

import os
import sys

import yaml

from . import classify, db, jobspy_scraper, notify, scraper

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DB_PATH = os.path.join(_ROOT, "data", "jobs.db")
_COMPANIES = os.path.join(_ROOT, "config", "companies.yaml")
_SETTINGS = os.path.join(_ROOT, "config", "settings.yaml")


def _load_yaml(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def main() -> int:
    settings = _load_yaml(_SETTINGS)
    companies = _load_yaml(_COMPANIES).get("companies", [])
    if not companies:
        print("No companies configured. Run discover.py or edit config/companies.yaml.")
        return 1

    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = db.connect(_DB_PATH)

    # --- Workday scrape ---
    all_postings: list = []
    for company in companies:
        name = company.get("name", company["tenant"])
        print(f"Scraping {name} ...")
        postings = scraper.fetch_company_jobs(company, settings)
        print(f"  {len(postings)} postings")
        all_postings.extend(postings)

    print(f"\nWorkday postings this run: {len(all_postings)}")

    # --- External job boards (Indeed, Glassdoor, ZipRecruiter) ---
    print("\nScraping external job boards ...")
    external_postings = jobspy_scraper.fetch_jobs(settings)
    all_postings.extend(external_postings)

    print(f"\nTotal postings this run: {len(all_postings)}")

    result = db.sync(
        conn,
        all_postings,
        role_for=lambda p: classify.classify(p.title, settings),
    )

    print(f"New: {len(result.new_jobs)}  Updated: {result.updated}  "
          f"Removed: {result.removed}")

    notify.post_new_jobs(result.new_jobs)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
