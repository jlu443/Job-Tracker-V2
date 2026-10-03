"""Collect stage: scrape every source (or one CI part), trim, save and merge parts.

    collect(settings, conn)            everything, in one process
    collect(settings, conn, part)      one CI part: "workday:0/2", "icims:0/1", "rest"
    trim_for_handoff / save_part       what a CI scrape job hands to the process job
    load_parts                         merge the parts, in dedupe precedence order
"""

from __future__ import annotations

import dataclasses
import gzip
import json
import logging
import os
import time
import zlib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from . import (ashby_scraper, bigtech_scrapers, classify, db, eightfold_scraper, greenhouse_scraper,
               icims_scraper, jibe_scraper, jobspy_scraper, lever_scraper, oracle_scraper,
               rippling_scraper, scraper, simplify_scraper, smartrecruiters_scraper)
from .config import CONFIG_DIR, load_yaml
from .posting import JobPosting

log = logging.getLogger(__name__)


class _One:
    """Adapter: a single-company fetch function as a scraper module."""
    def __init__(self, fn):
        self.fetch_company_jobs = fn


# (source, config file, scraper module). Every module exposes
# fetch_company_jobs(company, settings) -> (list[JobPosting], complete: bool).
ATS_SCRAPERS = [
    ("workday",         "companies.yaml",        scraper),
    ("greenhouse",      "greenhouse.yaml",       greenhouse_scraper),
    ("lever",           "lever.yaml",            lever_scraper),
    ("ashby",           "ashby.yaml",            ashby_scraper),
    ("smartrecruiters", "smartrecruiters.yaml",  smartrecruiters_scraper),
    ("oracle",          "oracle.yaml",           oracle_scraper),
    ("eightfold",       "eightfold.yaml",        eightfold_scraper),
    ("icims",           "icims.yaml",            icims_scraper),
    ("jibe",            "jibe.yaml",             jibe_scraper),
    ("rippling",        "rippling.yaml",         rippling_scraper),
    ("tiktok",          "tiktok.yaml",           _One(bigtech_scrapers.fetch_tiktok)),
    ("amazon",          "amazon.yaml",           _One(bigtech_scrapers.fetch_amazon)),
    ("apple",           "apple.yaml",            _One(bigtech_scrapers.fetch_apple)),
]


def company_name(company: dict) -> str:
    return (company.get("name") or company.get("tenant") or company.get("token")
            or company.get("slug") or company.get("company") or "?")


def due_this_run(companies: list[dict], productive: set[str], every: int,
                  slot: int, hot_every: int = 1) -> list[dict]:
    """Boards with open entry-level jobs run every `hot_every` runs; the long
    tail every `every` runs. Each group is split into stable slices by a hash
    of the board name, one slice per run. A board that isn't scraped keeps
    its rows untouched (it's not in complete_scopes)."""
    def due(c, n):
        return n <= 1 or zlib.crc32(company_name(c).encode()) % n == slot % n
    return [c for c in companies
            if due(c, hot_every if company_name(c) in productive else every)]


def fetch_one(source: str, module, company: dict, settings: dict):
    """One board. An unexpected error (a malformed record, an API change) is
    that board's problem: it's reported and counted incomplete, so its jobs
    aren't marked removed and the other ~6,000 boards still get scraped."""
    try:
        return module.fetch_company_jobs(company, settings)
    except Exception as exc:
        log.warning(f"  ! {source} {company_name(company)}: {type(exc).__name__}: {exc}")
        return [], False


def scrape_source(source: str, module, companies: list[dict], settings: dict):
    """One ATS's boards, concurrently. Returns (postings, complete scopes, failed).

    Boards that fail in the main pass (mostly rate limits, which are per IP
    and hit while every thread is firing) get one more try at low
    concurrency after a pause, when settings.retry_incomplete lists the
    source: {source: {delay: seconds, workers: n}}.
    """
    workers = (settings.get("scrape_workers_by_source") or {}).get(
        source, settings.get("scrape_workers", 8))
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda c: fetch_one(source, module, c, settings), companies))

    retry = (settings.get("retry_incomplete") or {}).get(source)
    failed_at = [i for i, (_, complete) in enumerate(results) if complete is False]
    recovered = 0
    if retry and failed_at:
        time.sleep(retry.get("delay", 30))
        with ThreadPoolExecutor(max_workers=retry.get("workers", 4)) as pool:
            again = list(pool.map(lambda i: fetch_one(source, module, companies[i], settings),
                                  failed_at))
        for i, (found, complete) in zip(failed_at, again):
            if complete is not False:
                results[i] = (found, complete)      # a clean second pass replaces the first
                recovered += 1
            else:                                   # keep whatever either pass saw
                seen = {p.job_id for p in results[i][0]}
                results[i] = (results[i][0] + [p for p in found if p.job_id not in seen], False)

    postings, failed = [], 0
    # Removal is scoped by company name (what db rows carry), and one company
    # can have several boards (CVS Health has two Workday sites). A name only
    # counts as complete when every one of its boards completed this run.
    complete_by_name: dict[str, bool] = {}
    for company, (found, complete) in zip(companies, results):
        postings.extend(found)
        name = company_name(company)
        complete_by_name[name] = complete_by_name.get(name, True) and bool(complete)
        if complete is False:        # None = recent-only by design, not a failure
            failed += 1
    scopes = {(source, name) for name, ok in complete_by_name.items() if ok}
    log.info(f"{source}: {len(postings)} postings from {len(companies)} boards "
             f"in {time.time() - t0:.0f}s" + (f" ({failed} incomplete)" if failed else "")
             + (f"; {recovered} of {len(failed_at)} recovered on retry" if failed_at and retry
                else ""))
    return postings, scopes, failed


def shard_of(company: dict, n: int) -> int:
    """Stable shard for a board. Keyed by company name, so all of one
    company's boards land in the same shard (closed-job detection needs every
    board of a name to be scraped in the same job)."""
    key = company_name(company)
    return zlib.crc32(f"shard:{key}".encode()) % n


def scrape_ats(settings: dict, postings: list, complete_scopes: set, conn,
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
    for source, config_file, module in ATS_SCRAPERS:
        if source in disabled:
            log.info(f"  {source}: disabled (settings.disabled_sources)")
            continue
        if (only is not None and source not in only) or source in exclude:
            continue
        companies = load_yaml(os.path.join(CONFIG_DIR, config_file)) \
            .get("companies", []) or []
        if shard:
            companies = [c for c in companies if shard_of(c, shard[1]) == shard[0]]
        # int = tail every N runs (productive boards every run);
        # {tail: N, hot: M} = productive boards every M runs too.
        cfg = rotation.get(source, 1)
        every, hot = (cfg.get("tail", 1), cfg.get("hot", 1)) if isinstance(cfg, dict) else (cfg, 1)
        total = len(companies)
        due = companies
        if every > 1 or hot > 1:   # db lookups stay on this thread (sqlite)
            due = due_this_run(companies, db.productive_boards(conn, source),
                                every, slot, hot)
        if source in (settings.get("recency_check") or {}):
            # Every board gets a cheap newest-postings check each run; boards
            # due in the rotation also get the full sweep (which is what
            # detects closed listings).
            due_names = {company_name(c) for c in due}
            companies = [{**c, "_mode": "recent+sweep" if company_name(c) in due_names
                          else "recent"} for c in companies]
            log.info(f"  {source}: all {total} boards checked for new postings; "
                  f"{len(due)} also fully swept")
        else:
            companies = due
            log.info(f"  {source}: {len(companies)} of {total} boards due"
                  + (f" (open-job boards every {hot}, others every {every} runs)"
                     if every > 1 or hot > 1 else ""))
        plan.append((source, module, companies))

    log.info(f"\n=== ATS boards ({len(plan)} sources in parallel) ===")
    board_stats: dict[str, tuple[int, int]] = {}
    if not plan:
        return board_stats
    with ThreadPoolExecutor(max_workers=len(plan)) as pool:
        futures = [pool.submit(scrape_source, src, mod, cos, settings)
                   for src, mod, cos in plan]
        # Collected in ATS_SCRAPERS order so dedupe precedence is stable.
        for (source, _, companies), fut in zip(plan, futures):
            found, scopes, failed = fut.result()
            postings.extend(found)
            complete_scopes |= scopes
            board_stats[source] = (len(companies), failed)
    return board_stats


@dataclasses.dataclass
class Collected:
    postings: list
    complete_scopes: set
    complete_sources: set
    board_stats: dict
    # Postings per source *before* trim_for_handoff(), for the health check.
    source_counts: dict = dataclasses.field(default_factory=dict)


def keyword_role(p) -> str:
    """The role process() assigns without the zero-shot fallback: a curated
    list's label unless the title clearly says otherwise, else the title."""
    by_title = classify.classify_by_keyword(p.title)
    if p.role_hint:
        return by_title if by_title in ("intern", "new_grad", "senior") else p.role_hint
    return by_title or "mid"


def trim_for_handoff(c: Collected, conn, settings: dict) -> int:
    """Drop postings process() would discard anyway: new jobs whose role isn't
    stored (senior/mid, ~85% of what the full boards return). Jobs already in
    the DB are kept (their last_seen and closure tracking need them). Returns
    how many were dropped. Skipped when the zero-shot fallback may reclassify
    titles the keyword rules leave as "mid"."""
    store = settings.get("store_roles")
    if not store or settings.get("use_llm_fallback"):
        return 0
    store, known = set(store), db.existing_ids(conn)
    before = len(c.postings)
    c.postings = [p for p in c.postings if p.job_id in known or keyword_role(p) in store]
    return before - len(c.postings)


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
        board_stats = scrape_ats(settings, postings, scopes, conn, only, exclude, shard)
        if side:
            curated, curated_ok = curated_f.result()
            postings.extend(curated)
            if curated_ok:
                sources.add("simplify")
            postings.extend(external_f.result())
    return Collected(postings, scopes, sources, board_stats,
                     dict(Counter(p.source for p in postings)))


def save_part(path: str, c: Collected) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump({"postings": [dataclasses.asdict(p) for p in c.postings],
                   "complete_scopes": sorted(c.complete_scopes),
                   "complete_sources": sorted(c.complete_sources),
                   "board_stats": c.board_stats,
                   "source_counts": c.source_counts}, fh)


def precedence(source: str) -> int:
    """Dedupe keeps the first copy of a job: first-party ATS boards, then
    curated lists, then aggregators."""
    order = [s for s, _, _ in ATS_SCRAPERS] + ["simplify"]
    return order.index(source) if source in order else len(order)


def load_parts(paths: list) -> Collected:
    merged = Collected([], set(), set(), {})
    for path in sorted(paths):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            d = json.load(fh)
        merged.postings += [JobPosting(**p) for p in d["postings"]]
        merged.complete_scopes |= {tuple(x) for x in d["complete_scopes"]}
        merged.complete_sources |= set(d["complete_sources"])
        for src, n in (d.get("source_counts") or {}).items():
            merged.source_counts[src] = merged.source_counts.get(src, 0) + n
        for src, (attempted, failed) in d["board_stats"].items():
            a0, f0 = merged.board_stats.get(src, (0, 0))
            merged.board_stats[src] = (a0 + attempted, f0 + failed)
    merged.postings.sort(key=lambda p: precedence(p.source))   # stable
    return merged
