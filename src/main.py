"""Entry point: scrape every source, classify, persist, detect reposts, announce.

    python -m src.main                                  # everything, one process
    python -m src.main --scrape-part workday:0/2 --out p0.json.gz
    python -m src.main --scrape-part rest --out p2.json.gz
    python -m src.main --from-parts DIR                 # merge parts, then process

CI runs the scrape parts as parallel jobs (separate machines, so separate
IPs for Workday's per-IP rate limit) and one final job merges and processes.
"""

from __future__ import annotations

import argparse
import dataclasses
import glob
import gzip
import json
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
               icims_scraper, jobspy_scraper, lever_scraper, notify, oracle_scraper, profile,
               repost, scraper, sheets,
               simplify_scraper, smartrecruiters_scraper)
from .posting import JobPosting
from . import bigtech_scrapers, jibe_scraper, rippling_scraper


class _One:
    """Adapter: a single-company fetch function as a scraper module."""
    def __init__(self, fn):
        self.fetch_company_jobs = fn

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
    ("oracle",          "oracle.yaml",           oracle_scraper),
    ("icims",           "icims.yaml",            icims_scraper),
    ("jibe",            "jibe.yaml",             jibe_scraper),
    ("rippling",        "rippling.yaml",         rippling_scraper),
    ("tiktok",          "tiktok.yaml",           _One(bigtech_scrapers.fetch_tiktok)),
    ("amazon",          "amazon.yaml",           _One(bigtech_scrapers.fetch_amazon)),
    ("apple",           "apple.yaml",            _One(bigtech_scrapers.fetch_apple)),
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
            elif complete is False:      # None = recent-only by design, not a failure
                failed += 1
    print(f"{source}: {len(postings)} postings from {len(companies)} boards "
          f"in {time.time() - t0:.0f}s" + (f" ({failed} incomplete)" if failed else ""))
    return postings, scopes, failed


def _shard_of(company: dict, n: int) -> int:
    """Stable shard for a board; one tenant's sites stay together."""
    key = company.get("tenant") or company.get("host") or _company_name(company)
    return zlib.crc32(f"shard:{key}".encode()) % n


def _scrape_ats(settings: dict, postings: list, complete_scopes: set, conn,
                only: set | None = None, exclude: set = frozenset(),
                shard: tuple[int, int] | None = None) -> dict[str, tuple[int, int]]:
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
        if (only is not None and source not in only) or source in exclude:
            continue
        companies = _load_yaml(os.path.join(_CONFIG_DIR, config_file)) \
            .get("companies", []) or []
        if shard:
            companies = [c for c in companies if _shard_of(c, shard[1]) == shard[0]]
        # int = tail every N runs (productive boards every run);
        # {tail: N, hot: M} = productive boards every M runs too.
        cfg = rotation.get(source, 1)
        every, hot = (cfg.get("tail", 1), cfg.get("hot", 1)) if isinstance(cfg, dict) else (cfg, 1)
        total = len(companies)
        due = companies
        if every > 1 or hot > 1:   # db lookups stay on this thread (sqlite)
            due = _due_this_run(companies, db.productive_boards(conn, source),
                                every, slot, hot)
        if source in (settings.get("recency_check") or {}):
            # Every board gets a cheap newest-postings check each run; boards
            # due in the rotation also get the full sweep (which is what
            # detects closed listings).
            due_names = {_company_name(c) for c in due}
            companies = [{**c, "_mode": "recent+sweep" if _company_name(c) in due_names
                          else "recent"} for c in companies]
            print(f"  {source}: all {total} boards checked for new postings; "
                  f"{len(due)} also fully swept")
        else:
            companies = due
            print(f"  {source}: {len(companies)} of {total} boards due"
                  + (f" (open-job boards every {hot}, others every {every} runs)"
                     if every > 1 or hot > 1 else ""))
        plan.append((source, module, companies))

    print(f"\n=== ATS boards ({len(plan)} sources in parallel) ===")
    board_stats: dict[str, tuple[int, int]] = {}
    if not plan:
        return board_stats
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


# -- collect: scraping, all of it or one part -------------------------------

@dataclasses.dataclass
class Collected:
    postings: list
    complete_scopes: set
    complete_sources: set
    board_stats: dict


def collect(settings: dict, conn, part: str | None = None) -> Collected:
    """Scrape everything, or one part:
         "SOURCE:k/n"  shard k of n of one ATS source's boards
         "rest"        every source not split into shards (settings.ci_shards),
                       plus the curated lists and aggregators
    """
    only, exclude, shard, side = None, set(), None, True
    if part and part != "rest":
        source, _, frac = part.partition(":")
        k, _, n = frac.partition("/")
        only, shard, side = {source}, (int(k), int(n)), False
    elif part == "rest":
        exclude = set((settings.get("ci_shards") or {}).keys())

    postings: list = []
    scopes: set = set()
    sources: set = set()
    # Curated lists and aggregators are other hosts and never touch the DB,
    # so they run alongside the ATS scrape; results are appended afterwards
    # in precedence order.
    with ThreadPoolExecutor(max_workers=2) as side_pool:
        curated_f = side_pool.submit(simplify_scraper.fetch_jobs, settings) if side else None
        external_f = side_pool.submit(jobspy_scraper.fetch_jobs, settings) if side else None
        board_stats = _scrape_ats(settings, postings, scopes, conn, only, exclude, shard)
        if side:
            curated, curated_ok = curated_f.result()
            postings.extend(curated)
            if curated_ok:
                sources.add("simplify")
            postings.extend(external_f.result())
    return Collected(postings, scopes, sources, board_stats)


def save_part(path: str, c: Collected) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump({"postings": [dataclasses.asdict(p) for p in c.postings],
                   "complete_scopes": sorted(c.complete_scopes),
                   "complete_sources": sorted(c.complete_sources),
                   "board_stats": c.board_stats}, fh)


def _precedence(source: str) -> int:
    """Dedupe keeps the first copy of a job: first-party ATS boards, then
    curated lists, then aggregators."""
    order = [s for s, _, _ in _ATS_SCRAPERS] + ["simplify"]
    return order.index(source) if source in order else len(order)


def load_parts(paths: list) -> Collected:
    merged = Collected([], set(), set(), {})
    for path in sorted(paths):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            d = json.load(fh)
        merged.postings += [JobPosting(**p) for p in d["postings"]]
        merged.complete_scopes |= {tuple(x) for x in d["complete_scopes"]}
        merged.complete_sources |= set(d["complete_sources"])
        for src, (attempted, failed) in d["board_stats"].items():
            a0, f0 = merged.board_stats.get(src, (0, 0))
            merged.board_stats[src] = (a0 + attempted, f0 + failed)
    merged.postings.sort(key=lambda p: _precedence(p.source))   # stable
    return merged


# -- process: everything after scraping --------------------------------------

def process(conn, settings: dict, collected: Collected, run_at: str,
            run_started: float) -> None:
    all_postings = collected.postings
    complete_scopes = collected.complete_scopes
    complete_sources = collected.complete_sources
    board_stats = collected.board_stats

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
                     max_age_days=settings.get("max_listing_age_days"),
                     age_exempt=frozenset(settings.get("age_limit_exempt_sources") or ()))
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
        # Descriptions already read at scrape time aren't fetched again
        # (LinkedIn's page is, for its applicant count).
        to_fetch = [j for j in targets if not j.get("checked_at") or j["source"] == "linkedin"]
        to_fetch.sort(key=lambda j: j.get("posted_on") or "", reverse=True)
        enrich.enrich_jobs(to_fetch[:cap])
        if len(to_fetch) > cap:
            print(f"  enrichment capped at {cap}; {len(to_fetch) - cap} left for the backlog")
    db.update_enrichment(conn, targets)

    # Work through open jobs whose description was never read, so the Sheet's
    # sponsorship / citizenship / clearance columns fill in over a few runs.
    # PhD & research internships first, then announced jobs, then newest.
    backlog_n = settings.get("enrich_backlog_per_run", 300)
    if settings.get("enrich_descriptions", True) and backlog_n:
        backlog = [j for j in db.unchecked_open(conn, backlog_n * 3) if enrich.fetchable(j)]
        linkedin = [j for j in backlog if j["source"] == "linkedin"][:30]   # rate-limited host
        backlog = [j for j in backlog if j["source"] != "linkedin"][:backlog_n - len(linkedin)]
        enrich.enrich_jobs(backlog + linkedin, label="unchecked open jobs (backlog)")
        db.update_enrichment(conn, backlog + linkedin)

    policy = settings.get("reposts", {}).get("announce", "annotate")
    if policy == "suppress":
        targets = [j for j in targets if not j["repost"]]
    if settings.get("exclude_no_sponsorship"):
        before = len(targets)
        targets = [j for j in targets if j.get("sponsorship") != "no"]
        if len(targets) != before:
            print(f"Excluded {before - len(targets)} no-sponsorship jobs.")
    # Personal filters (settings.profile), after enrichment so visa /
    # citizenship / grad-year flags are known.
    skipped_profile = Counter(r for j in targets
                              for r in profile.reasons_to_skip(j, settings.get("profile") or {}))
    targets = [j for j in targets if profile.fits(j, settings)]
    if skipped_profile:
        print("Profile filtered: " + ", ".join(f"{n} {r}" for r, n in skipped_profile.items()))

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
        purged = db.purge_old(
            conn, settings["max_listing_age_days"],
            exempt_sources=frozenset(settings.get("age_limit_exempt_sources") or ()))
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


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="Job tracker run")
    ap.add_argument("--scrape-part", help='scrape only this part ("workday:0/2", "rest") '
                                          "and save it with --out; the DB isn't changed")
    ap.add_argument("--out", help="where --scrape-part writes its results (.json.gz)")
    ap.add_argument("--from-parts", help="directory of saved parts: merge them instead of "
                                         "scraping, then run the rest of the pipeline")
    args = ap.parse_args(argv)

    run_started = time.time()
    run_at = db._now()
    settings = _load_yaml(_SETTINGS)
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = db.connect(_DB_PATH)
    _apply_config_renames(conn)

    if args.scrape_part:
        if not args.out:
            ap.error("--scrape-part needs --out")
        collected = collect(settings, conn, args.scrape_part)
        save_part(args.out, collected)
        print(f"Saved {len(collected.postings)} postings for part {args.scrape_part!r}")
        conn.close()
        return 0

    if args.from_parts:
        paths = glob.glob(os.path.join(args.from_parts, "**", "*.json.gz"), recursive=True)
        if not paths:
            print(f"No scrape parts found in {args.from_parts}")
            return 1
        print(f"Merging {len(paths)} scrape parts")
        collected = load_parts(paths)
    else:
        collected = collect(settings, conn)

    process(conn, settings, collected, run_at, run_started)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
