"""Entry point: scrape every configured company, classify, persist, announce.

    python -m src.main          # from the repo root
"""

from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import yaml

from . import (ashby_scraper, classify, db, greenhouse_scraper, jobspy_scraper,
               lever_scraper, notify, scraper, sheets, smartrecruiters_scraper,
               workable_scraper)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DB_PATH = os.path.join(_ROOT, "data", "jobs.db")
_CONFIG_DIR = os.path.join(_ROOT, "config")
_SETTINGS = os.path.join(_CONFIG_DIR, "settings.yaml")

# (label, config file, scraper module). Every module exposes
# fetch_company_jobs(company, settings) -> list[JobPosting].
_ATS_SCRAPERS = [
    ("Workday",         "companies.yaml",        scraper),
    ("Greenhouse",      "greenhouse.yaml",       greenhouse_scraper),
    ("Lever",           "lever.yaml",            lever_scraper),
    ("Ashby",           "ashby.yaml",            ashby_scraper),
    ("SmartRecruiters", "smartrecruiters.yaml",  smartrecruiters_scraper),
    ("Workable",        "workable.yaml",         workable_scraper),
]


def _load_yaml(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _company_name(company: dict) -> str:
    return (company.get("name") or company.get("tenant") or company.get("token")
            or company.get("slug") or company.get("company") or "?")


def main() -> int:
    settings = _load_yaml(_SETTINGS)

    boards = []
    for label, config_file, module in _ATS_SCRAPERS:
        companies = _load_yaml(os.path.join(_CONFIG_DIR, config_file)) \
            .get("companies", []) or []
        boards.append((label, companies, module))

    if not any(companies for _, companies, _ in boards):
        print("No companies configured in any config file.")
        return 1

    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = db.connect(_DB_PATH)

    all_postings: list = []

    # Companies are scraped concurrently: discovery can surface thousands of
    # boards, and one-at-a-time with a politeness sleep would take hours.
    workers = settings.get("scrape_workers", 8)
    for label, companies, module in boards:
        print(f"\n=== {label} ({len(companies)} companies) ===")
        count_before, t0 = len(all_postings), time.time()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for company, postings in zip(
                    companies,
                    pool.map(lambda c: module.fetch_company_jobs(c, settings),
                             companies)):
                if postings:
                    print(f"  {_company_name(company)}: {len(postings)} postings")
                all_postings.extend(postings)
        print(f"{label} total: {len(all_postings) - count_before} "
              f"in {time.time() - t0:.0f}s")

    # --- External job boards (Indeed, Glassdoor, ZipRecruiter) ---
    print("\n=== External job boards ===")
    t0 = time.time()
    all_postings.extend(jobspy_scraper.fetch_jobs(settings))
    print(f"External boards done in {time.time() - t0:.0f}s")

    print(f"\nTotal postings this run: {len(all_postings)}")

    # Classify only genuinely new postings, in one batched pass — calling the
    # zero-shot model per title serially is what blows up CI runtime.
    t0 = time.time()
    existing = db.existing_ids(conn)
    new_postings = [p for p in all_postings if p.job_id not in existing]
    print(f"Classifying {len(new_postings)} new postings ...")
    roles = classify.classify_batch([p.title for p in new_postings], settings)
    role_by_id = {p.job_id: r for p, r in zip(new_postings, roles)}
    print(f"Classification done in {time.time() - t0:.0f}s")

    result = db.sync(
        conn,
        all_postings,
        role_for=lambda p: role_by_id.get(p.job_id, "mid"),
    )

    print(f"New: {len(result.new_jobs)}  Updated: {result.updated}  "
          f"Removed: {result.removed}")

    notify.post_new_jobs(result.new_jobs)
    sheets.post_new_jobs(result.new_jobs)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
