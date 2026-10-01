"""Entry point: scrape every source, classify, persist, detect reposts, announce.

    python -m src.main          # from the repo root
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
import zlib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import yaml

from . import (accuracy, ashby_scraper, classify, db, dedupe, enrich, greenhouse_scraper, health,
               icims_scraper, jobspy_scraper, lever_scraper, notify, oracle_scraper,
               repost, scraper, sheets,
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
    ("oracle",          "oracle.yaml",           oracle_scraper),
    ("icims",           "icims.yaml",            icims_scraper),
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
                  slot: int, hot_every: int = 1) -> list[dict]:
    """Boards with open entry-level jobs run every `hot_every` runs; the long
    tail every `every` runs. Each group is split into stable slices by a hash
    of the board name, one slice per run. A board that isn't scraped keeps
    its rows untouched (it's not in complete_scopes)."""
    def due(c, n):
        return n <= 1 or zlib.crc32(_company_name(c).encode()) % n == slot % n
    return [c for c in companies
            if due(c, hot_every if _company_name(c) in productive else every)]


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


def _fetch_one(source: str, module, company: dict, settings: dict):
    """One board. An unexpected error (a malformed record, an API change) is
    that board's problem: it's reported and counted incomplete, so its jobs
    aren't marked removed and the other ~6,000 boards still get scraped."""
    try:
        return module.fetch_company_jobs(company, settings)
    except Exception as exc:
        print(f"  ! {source} {_company_name(company)}: {type(exc).__name__}: {exc}")
        return [], False


def _scrape_source(source: str, module, companies: list[dict], settings: dict):
    """One ATS's boards, concurrently. Returns (postings, complete scopes, failed)."""
    workers = (settings.get("scrape_workers_by_source") or {}).get(
        source, settings.get("scrape_workers", 8))
    postings, scopes, failed, t0 = [], set(), 0, time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = pool.map(lambda c: _fetch_one(source, module, c, settings), companies)
        for company, (found, complete) in zip(companies, results):
            postings.extend(found)
            if complete:
                # Keyed by the company name the postings carry (= db rows).
                scopes.add((source, _company_name(company)))
            else:
                failed += 1
    print(f"{source}: {len(postings)} postings from {len(companies)} boards "
          f"in {time.time() - t0:.0f}s" + (f" ({failed} incomplete)" if failed else ""))
    return postings, scopes, failed


def _scrape_ats(settings: dict, postings: list, complete_scopes: set,
                conn) -> dict[str, tuple[int, int]]:
    """Scrape every ATS; returns source → (boards attempted, boards incomplete).

    Sources run in parallel: each is a different set of hosts with its own
    politeness limits (iCIMS, the slowest, is throttled at its own edge), so
    wall time is the slowest source rather than the sum of all of them.
    """
    rotation = settings.get("long_tail_rotation") or {}
    slot = int(time.time() // 3600)   # advances once per hourly run
    plan = []
    disabled = set(settings.get("disabled_sources") or ())
    for source, config_file, module in _ATS_SCRAPERS:
        if source in disabled:
            print(f"  {source}: disabled (settings.disabled_sources)")
            continue
        companies = _load_yaml(os.path.join(_CONFIG_DIR, config_file)) \
            .get("companies", []) or []
        # int = tail every N runs (productive boards every run);
        # {tail: N, hot: M} = productive boards every M runs too.
        cfg = rotation.get(source, 1)
        every, hot = (cfg.get("tail", 1), cfg.get("hot", 1)) if isinstance(cfg, dict) else (cfg, 1)
        total = len(companies)
        if every > 1 or hot > 1:   # db lookups stay on this thread (sqlite)
            companies = _due_this_run(companies, db.productive_boards(conn, source),
                                      every, slot, hot)
        print(f"  {source}: {len(companies)} of {total} boards due"
              + (f" (open-job boards every {hot}, others every {every} runs)"
                 if every > 1 or hot > 1 else ""))
        plan.append((source, module, companies))

    print(f"\n=== ATS boards ({len(plan)} sources in parallel) ===")
    board_stats: dict[str, tuple[int, int]] = {}
    with ThreadPoolExecutor(max_workers=len(plan)) as pool:
        futures = [pool.submit(_scrape_source, src, mod, cos, settings)
                   for src, mod, cos in plan]
        # Collected in _ATS_SCRAPERS order so dedupe precedence is stable.
        for (source, _, companies), fut in zip(plan, futures):
            found, scopes, failed = fut.result()
            postings.extend(found)
            complete_scopes |= scopes
            board_stats[source] = (len(companies), failed)
    return board_stats


def main() -> int:
    run_started = time.time()
    run_at = db._now()
    settings = _load_yaml(_SETTINGS)
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = db.connect(_DB_PATH)

    # Order matters: dedupe keeps the first copy of a job, so first-party ATS
    # boards come before curated lists, which come before aggregators.
    all_postings: list = []
    complete_scopes: set[tuple[str, str]] = set()
    complete_sources: set[str] = set()

    _apply_config_renames(conn)

    # Curated lists and aggregators are other hosts and never touch the DB,
    # so they run alongside the ATS scrape; results are appended afterwards
    # in precedence order.
    with ThreadPoolExecutor(max_workers=2) as side:
        curated_f = side.submit(simplify_scraper.fetch_jobs, settings)
        external_f = side.submit(jobspy_scraper.fetch_jobs, settings)
        board_stats = _scrape_ats(settings, all_postings, complete_scopes, conn)
        curated, curated_ok = curated_f.result()
        external = external_f.result()
    all_postings.extend(curated)
    if curated_ok:
        complete_sources.add("simplify")
    all_postings.extend(external)

    print(f"\nTotal postings this run: {len(all_postings)}")
    health.record(conn, run_at, dict(Counter(p.source for p in all_postings)), board_stats)
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
    reclassified = db._meta_get(conn, "classifier_version") != str(classify.VERSION)
    fresh, relisted_from, skipped = repost.triage(
        result.new_jobs, history, new_boards, db.board_of, db.AGGREGATOR_SOURCES,
        reclassified=reclassified)
    if reclassified:
        db._meta_set(conn, "classifier_version", str(classify.VERSION))
        conn.commit()
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
    db.mark_announced(conn, [j["job_id"] for j in targets])
    sheets.publish(conn, settings)

    # Once a day, a one-line digest pointing at the sheet's Today tab.
    last_digest = db._meta_get(conn, "discord_digest_at")
    if not last_digest or last_digest < (datetime.now(timezone.utc)
                                         - timedelta(hours=23)).isoformat(timespec="seconds"):
        day_ago = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
        conn.row_factory = sqlite3.Row
        recent = [dict(r) for r in conn.execute(
            "SELECT * FROM jobs WHERE announced_at >= ?", (day_ago,))]
        notify.post_daily_summary(recent, os.environ.get("GOOGLE_SHEET_URL", ""))
        db._meta_set(conn, "discord_digest_at", db._now())
        conn.commit()

    if settings.get("max_listing_age_days"):
        purged = db.purge_old(conn, settings["max_listing_age_days"])
        if purged:
            print(f"Purged {purged} listings older than "
                  f"{settings['max_listing_age_days']} days.")
    pruned = db.prune(conn, settings.get("prune_removed_after_days", 30), store_roles)
    if pruned:
        print(f"Pruned {pruned} rows that are no longer kept.")
    try:
        report = accuracy.record_daily(conn, settings)
        if report:
            accuracy._print(report)
    except Exception as exc:          # a measurement problem must not fail the run
        print(f"  ! accuracy measurement failed: {exc}")

    problems = health.check(conn, run_at, time.time() - run_started, settings)
    if problems:
        print("Health check: " + "; ".join(problems))
    notify.post_alert(health.due_alerts(conn, problems))
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
