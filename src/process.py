"""Process stage: everything after scraping.

dedupe -> classify -> sync to the DB -> triage (new vs repost vs backfill) ->
enrich descriptions -> Sheet + Discord digest -> retention -> accuracy and
health checks.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

from . import (accuracy, classify, db, dedupe, enrich, health, maintenance, notify, profile,
               repost, sheets)
from .collect import ATS_SCRAPERS, Collected

log = logging.getLogger(__name__)


def discord_digest(conn, settings: dict) -> None:
    """Post the jobs announced since the last digest once digest_hours have
    passed. Jobs closed in the meantime are left out."""
    hours = (settings.get("discord") or {}).get("digest_hours", 3)
    now = datetime.now(timezone.utc)
    last = db._meta_get(conn, "discord_last_digest_at")
    if last and datetime.fromisoformat(last) > now - timedelta(hours=hours, minutes=-10):
        log.info(f"Discord digest: next one {hours}h after {last[11:16]} UTC")
        return
    since = last or (now - timedelta(hours=hours)).isoformat(timespec="seconds")
    conn.row_factory = sqlite3.Row
    jobs = [dict(r) for r in conn.execute(
        "SELECT * FROM jobs WHERE announced_at > ? AND status = 'active' "
        "ORDER BY posted_on DESC", (since,))]
    notify.post_digest(jobs, f"{since[11:16]} UTC", os.environ.get("GOOGLE_SHEET_URL", ""))
    db._meta_set(conn, "discord_last_digest_at", now.isoformat(timespec="seconds"))
    conn.commit()


def process(conn, settings: dict, collected: Collected, run_at: str,
            run_started: float) -> None:
    all_postings = collected.postings
    complete_scopes = collected.complete_scopes
    complete_sources = collected.complete_sources
    board_stats = collected.board_stats

    log.info(f"\nTotal postings this run: {len(all_postings)}")
    health.record(conn, run_at, collected.source_counts
                  or dict(Counter(p.source for p in all_postings)), board_stats)
    known = db.existing_ids(conn)
    all_postings, dupes = dedupe.dedupe_postings(all_postings, known)
    if dupes:
        log.info(f"Dropped {dupes} duplicate postings (same job via another source).")

    # Classify only genuinely new postings, in one batched pass — calling the
    # zero-shot model per title serially is what blows up CI runtime.
    t0 = time.time()
    new_postings = [p for p in all_postings if p.job_id not in known and not p.role_hint]
    log.info(f"Classifying {len(new_postings)} new postings ...")
    roles = classify.classify_batch([p.title for p in new_postings], settings)
    role_by_id = {p.job_id: r for p, r in zip(new_postings, roles)}
    log.info(f"Classification done in {time.time() - t0:.0f}s")

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
    log.info(f"New: {len(result.new_jobs)}  Updated: {result.updated}  "
          f"Removed: {result.removed}")
    if result.bumped:
        by_source = Counter(b["source"] for b in result.bumped)
        log.info(f"Re-dated (reposted) listings: {len(result.bumped)} "
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
        log.info("Not announced: " + ", ".join(f"{n} {why}" for why, n in skipped.items())
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
        log.info(f"Flagged {n_repost} of {len(targets)} announceable jobs as reposts/old.")

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
            log.info(f"  enrichment capped at {cap}; {len(to_fetch) - cap} left for the backlog")
    db.update_enrichment(conn, targets)

    # Work through open jobs whose description was never read, so the Sheet's
    # sponsorship / citizenship / clearance columns fill in over a few runs.
    # PhD & research internships first, then announced jobs, then newest.
    if db._meta_get(conn, "flags_version") != enrich.FLAGS_VERSION:
        # New description flag: re-read open PhD/research postings (the
        # backlog takes them first) so the PhD tab gets it.
        n = conn.execute("UPDATE jobs SET checked_at = '' WHERE status = 'active' "
                         "AND research_track != ''").rowcount
        db._meta_set(conn, "flags_version", enrich.FLAGS_VERSION)
        conn.commit()
        log.info(f"Description flags v{enrich.FLAGS_VERSION}: re-reading {n} PhD/research postings")
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
            log.info(f"Excluded {before - len(targets)} no-sponsorship jobs.")
    # Personal filters (settings.profile), after enrichment so visa /
    # citizenship / grad-year flags are known.
    skipped_profile = Counter(r for j in targets
                              for r in profile.reasons_to_skip(j, settings.get("profile") or {}))
    targets = [j for j in targets if profile.fits(j, settings)]
    if skipped_profile:
        log.info("Profile filtered: " + ", ".join(f"{n} {r}" for r, n in skipped_profile.items()))

    # Announced = in the Sheet's Today / This Week now; Discord gets them in
    # one digest every few hours instead of a post per hourly run.
    db.mark_announced(conn, [j["job_id"] for j in targets])
    sheets.publish(conn, settings)
    discord_digest(conn, settings)

    if settings.get("max_listing_age_days"):
        purged = maintenance.purge_old(
            conn, settings["max_listing_age_days"],
            exempt_sources=frozenset(settings.get("age_limit_exempt_sources") or ()))
        if purged:
            log.info(f"Purged {purged} listings older than "
                  f"{settings['max_listing_age_days']} days.")
    pruned = maintenance.prune(conn, settings.get("prune_removed_after_days", 30), store_roles)
    if pruned:
        log.info(f"Pruned {pruned} rows that are no longer kept.")
    try:
        report = accuracy.record_daily(conn, settings)
        if report:
            accuracy.log_report(report)
    except Exception as exc:          # a measurement problem must not fail the run
        log.warning(f"  ! accuracy measurement failed: {exc}")

    disabled = set(settings.get("disabled_sources") or ())
    expected = ({s for s, _, _ in ATS_SCRAPERS if s not in disabled}
                | {"simplify"} | set((settings.get("jobspy") or {}).get("sites") or ()))
    problems = health.check(conn, run_at, time.time() - run_started, settings, expected)
    if problems:
        log.info("Health check: " + "; ".join(problems))
    notify.post_alert(health.due_alerts(conn, problems))
