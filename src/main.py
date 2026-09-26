"""Entry point: scrape every source, classify, persist, detect reposts, announce.

    python -m src.main          # from the repo root
"""

from __future__ import annotations

import os
import sys
import time
import zlib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import yaml

from . import (ashby_scraper, classify, db, dedupe, enrich, greenhouse_scraper,
               jobspy_scraper, lever_scraper, notify, repost, scraper, sheets,
               simplify_scraper, smartrecruiters_scraper, workable_scraper)

# Windows consoles default to cp1252; job titles are frequently Unicode.
# Never let a print() kill the run after the DB has already synced.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DB_PATH = os.environ.get("JOBS_DB_PATH") or os.path.join(_ROOT, "data", "jobs.db")
_CONFIG_DIR = os.path.join(_ROOT, "config")
_SETTINGS = os.path.join(_CONFIG_DIR, "settings.yaml")

# (source, config file, scraper module). Every module exposes
# fetch_company_jobs(company, settings) -> (list[JobPosting], complete: bool).
_ATS_SCRAPERS = [
    ("workday",         "companies.yaml",        scraper),
    ("greenhouse",      "greenhouse.yaml",       greenhouse_scraper),
    ("lever",           "lever.yaml",            lever_scraper),
    ("ashby",           "ashby.yaml",            ashby_scraper),
    ("smartrecruiters", "smartrecruiters.yaml",  smartrecruiters_scraper),
    ("workable",        "workable.yaml",         workable_scraper),
]


def _load_yaml(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _company_name(company: dict) -> str:
    return (company.get("name") or company.get("tenant") or company.get("token")
            or company.get("slug") or company.get("company") or "?")


def _due_this_run(companies: list[dict], productive: set[str], every: int,
                  slot: int) -> list[dict]:
    """Boards that have produced entry-level jobs run every time; the long
    tail is split into `every` stable slices, one slice per run. A board that
    isn't scraped keeps its rows untouched (it's not in complete_scopes)."""
    if every <= 1:
        return companies
    return [c for c in companies
            if _company_name(c) in productive
            or zlib.crc32(_company_name(c).encode()) % every == slot % every]


def _apply_config_renames(conn) -> None:
    """Boards renamed by `python -m src.names` keep their old name under
    `aliases`; move stored rows over so the board's history stays attached."""
    renames = {}
    for source, config_file, _ in _ATS_SCRAPERS:
        for c in _load_yaml(os.path.join(_CONFIG_DIR, config_file)).get("companies") or []:
            for alias in c.get("aliases") or []:
                renames[(source, alias)] = _company_name(c)
    moved = db.apply_renames(conn, renames)
    if moved:
        print(f"Renamed {moved} stored rows to their boards' company names.")


def _scrape_ats(settings: dict, postings: list, complete_scopes: set, conn) -> None:
    # Companies are scraped concurrently: discovery surfaces thousands of
    # boards, and one-at-a-time with a politeness sleep would take hours.
    rotation = settings.get("long_tail_rotation") or {}
    slot = int(time.time() // 3600)   # advances once per hourly run
    for source, config_file, module in _ATS_SCRAPERS:
        workers = (settings.get("scrape_workers_by_source") or {}).get(
            source, settings.get("scrape_workers", 8))
        companies = _load_yaml(os.path.join(_CONFIG_DIR, config_file)) \
            .get("companies", []) or []
        every = rotation.get(source, 1)
        if every > 1:
            total = len(companies)
            companies = _due_this_run(companies, db.productive_boards(conn, source),
                                      every, slot)
            print(f"\n=== {source} ({len(companies)} of {total} companies; "
                  f"long tail rotates 1/{every} per run) ===")
        else:
            print(f"\n=== {source} ({len(companies)} companies) ===")
        before, t0, failed = len(postings), time.time(), 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = pool.map(lambda c: module.fetch_company_jobs(c, settings), companies)
            for company, (found, complete) in zip(companies, results):
                if found:
                    print(f"  {_company_name(company)}: {len(found)} postings")
                postings.extend(found)
                if complete:
                    # Keyed by the company name the postings carry, which is
                    # what db rows store.
                    complete_scopes.add((source, _company_name(company)))
                else:
                    failed += 1
        print(f"{source} total: {len(postings) - before} in {time.time() - t0:.0f}s"
              + (f" ({failed} boards incomplete)" if failed else ""))


def main() -> int:
    settings = _load_yaml(_SETTINGS)
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = db.connect(_DB_PATH)

    # Order matters: dedupe keeps the first copy of a job, so first-party ATS
    # boards come before curated lists, which come before aggregators.
    all_postings: list = []
    complete_scopes: set[tuple[str, str]] = set()
    complete_sources: set[str] = set()

    _apply_config_renames(conn)
    _scrape_ats(settings, all_postings, complete_scopes, conn)

    print("\n=== Curated lists (SimplifyJobs format) ===")
    curated, curated_ok = simplify_scraper.fetch_jobs(settings)
    all_postings.extend(curated)
    if curated_ok:
        complete_sources.add("simplify")

    print("\n=== External job boards ===")
    t0 = time.time()
    all_postings.extend(jobspy_scraper.fetch_jobs(settings))
    print(f"External boards done in {time.time() - t0:.0f}s")

    print(f"\nTotal postings this run: {len(all_postings)}")
    known = db.existing_ids(conn)
    all_postings, dupes = dedupe.dedupe_postings(all_postings, known)
    if dupes:
        print(f"Dropped {dupes} duplicate postings (same job via another source).")

    # Classify only genuinely new postings, in one batched pass — calling the
    # zero-shot model per title serially is what blows up CI runtime.
    t0 = time.time()
    new_postings = [p for p in all_postings if p.job_id not in known and not p.role_hint]
    print(f"Classifying {len(new_postings)} new postings ...")
    roles = classify.classify_batch([p.title for p in new_postings], settings)
    role_by_id = {p.job_id: r for p, r in zip(new_postings, roles)}
    print(f"Classification done in {time.time() - t0:.0f}s")

    def role_for(p) -> str:
        # A curated list's intern/new_grad label wins unless the title
        # clearly says otherwise ("Senior ..." slipping into a list).
        if p.role_hint:
            by_title = classify.classify_by_keyword(p.title)
            return by_title if by_title in ("intern", "new_grad", "senior") else p.role_hint
        return role_by_id.get(p.job_id, "mid")

    # Snapshot before sync so "known" means "known before this run".
    history = db.role_history(conn)

    store_roles = settings.get("store_roles")
    store_roles = frozenset(store_roles) if store_roles else None
    result = db.sync(conn, all_postings, role_for, complete_scopes, complete_sources,
                     aggregator_ttl_days=settings.get("aggregator_ttl_days", 21),
                     store_roles=store_roles,
                     max_age_days=settings.get("max_listing_age_days"))
    print(f"New: {len(result.new_jobs)}  Updated: {result.updated}  "
          f"Removed: {result.removed}")
    if result.bumped:
        by_source = Counter(b["source"] for b in result.bumped)
        print(f"Re-dated (reposted) listings: {len(result.bumped)} "
              f"({', '.join(f'{s} {n}' for s, n in by_source.most_common())})")

    scraped = complete_scopes | {(src, "*") for src in complete_sources}
    scraped |= {(p.source, "*") for p in all_postings if p.source in db.AGGREGATOR_SOURCES}
    new_boards = db.register_boards(conn, scraped)
    fresh, relisted_from, skipped = repost.triage(
        result.new_jobs, history, new_boards, db.board_of, db.AGGREGATOR_SOURCES)
    if skipped:
        print("Not announced: " + ", ".join(f"{n} {why}" for why, n in skipped.items())
              + ("  (bootstrap = first scrape of a new board; backlog stored silently)"
                 if skipped.get("bootstrap") else ""))

    targets = notify.announceable(fresh, settings)

    # Label reposts: re-opened first-party listings, bumped LinkedIn
    # listings, and stale post dates.
    li_obs = [(repost.linkedin_numeric_id(p.apply_url), p.posted_on)
              for p in all_postings if p.source == "linkedin"]
    li_obs += [(repost.linkedin_numeric_id(u), d) for u, d in db.linkedin_observations(conn)]
    repost.annotate(targets, relisted_from, repost.linkedin_frontier(li_obs), settings)
    n_repost = sum(1 for j in targets if j["repost"])
    if n_repost:
        print(f"Flagged {n_repost} of {len(targets)} announceable jobs as reposts/old.")

    if settings.get("enrich_descriptions", True) and targets:
        # Detail fetches are ~0.5s each; cap them so a burst of new postings
        # can't push the run past the CI timeout. Newest-posted first.
        cap = settings.get("enrich_max_per_run", 400)
        by_recency = sorted(targets, key=lambda j: j.get("posted_on") or "", reverse=True)
        enrich.enrich_jobs(by_recency[:cap])
        if len(targets) > cap:
            print(f"  enrichment capped at {cap}; {len(targets) - cap} left unenriched")
    db.update_enrichment(conn, targets)

    policy = settings.get("reposts", {}).get("announce", "annotate")
    if policy == "suppress":
        targets = [j for j in targets if not j["repost"]]
    if settings.get("exclude_no_sponsorship"):
        before = len(targets)
        targets = [j for j in targets if j.get("sponsorship") != "no"]
        if len(targets) != before:
            print(f"Excluded {before - len(targets)} no-sponsorship jobs.")

    notify.post_new_jobs(targets)
    sheets.post_new_jobs(fresh)

    if settings.get("max_listing_age_days"):
        purged = db.purge_old(conn, settings["max_listing_age_days"])
        if purged:
            print(f"Purged {purged} listings older than "
                  f"{settings['max_listing_age_days']} days.")
    pruned = db.prune(conn, settings.get("prune_removed_after_days", 30), store_roles)
    if pruned:
        print(f"Pruned {pruned} rows that are no longer kept.")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
