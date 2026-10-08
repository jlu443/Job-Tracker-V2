"""Retention and housekeeping on jobs.db, run after each sync.

    purge_old       delete listings past the post-date cutoff (tombstoned)
    prune           delete rows outside store_roles
    apply_renames   move rows to a board's new company name
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from . import dates, db, dedupe
from .db import _now

log = logging.getLogger(__name__)


def apply_renames(conn: sqlite3.Connection, renames: dict[tuple[str, str], str]) -> int:
    """Move rows and board records from an old company name to a new one.

    renames maps (source, old name) → new name (config `aliases` → `name`).
    Only pairs actually present are touched, so it's a no-op once applied.
    """
    present = {(r[0], r[1]) for r in conn.execute("SELECT DISTINCT source, company FROM jobs")}
    present |= {(r[0], r[1]) for r in conn.execute("SELECT source, company FROM boards")}
    moved = 0
    for (source, old), new in renames.items():
        if (source, old) not in present or old == new:
            continue
        rows = conn.execute("SELECT job_id, title, location FROM jobs "
                            "WHERE source = ? AND company = ?", (source, old)).fetchall()
        conn.executemany(
            "UPDATE jobs SET company = ?, job_key = ? WHERE job_id = ?",
            [(new, dedupe.fuzzy_key(new, r["title"], r["location"]) or "", r["job_id"])
             for r in rows])
        # Keep the board's "already backfilled" status under its new name.
        conn.execute("UPDATE OR IGNORE boards SET company = ? WHERE source = ? AND company = ?",
                     (new, source, old))
        conn.execute("DELETE FROM boards WHERE source = ? AND company = ?", (source, old))
        moved += len(rows)
    conn.commit()
    return moved


def purge_old(conn: sqlite3.Connection, max_age_days: int,
              tombstone_days: int = 365, exempt_sources: frozenset = frozenset(),
              exempt_unseen_days: int = 14) -> int:
    """Delete listings older than max_age_days, whatever their status.

    Age is measured from the post date when the source gave one (the latest
    re-listing date if it was reposted), otherwise from when we first saw it.
    Sources in exempt_sources keep long-running postings (Apple's internship
    programs stay open for months) and are deleted instead once unseen for
    exempt_unseen_days. Titles recruiting for a season still ahead ("Summer
    2027 Intern", posted in July) outlive max_age_days until unseen for
    exempt_unseen_days. Deleted ids go into `purged` so a still-open listing
    isn't re-announced; tombstones themselves expire after tombstone_days.
    """
    cutoff = (dates.today() - timedelta(days=max_age_days)).isoformat()
    unseen = (datetime.now(timezone.utc)
              - timedelta(days=exempt_unseen_days)).isoformat(timespec="seconds")
    now = _now()
    rows = conn.execute("SELECT job_id, source, title, posted_on, relisted_on, first_seen, "
                        "last_seen FROM jobs").fetchall()
    ids = []
    for jid, source, title, posted, relisted, first_seen, last_seen in rows:
        outlives = dates.outlives_age_limit(source, title, max(posted, relisted), exempt_sources)
        if source in exempt_sources and outlives:
            if last_seen < unseen:            # retired once unseen, whatever its age
                ids.append(jid)
        elif ((max(posted, relisted) if posted else first_seen[:10]) < cutoff
              and not (outlives and last_seen >= unseen)):   # only ever extends a life
            ids.append(jid)
    conn.executemany("INSERT OR REPLACE INTO purged (job_id, purged_on) VALUES (?, ?)",
                     [(i, now) for i in ids])
    conn.executemany("DELETE FROM jobs WHERE job_id = ?", [(i,) for i in ids])
    tomb_cutoff = (datetime.now(timezone.utc)
                   - timedelta(days=tombstone_days)).isoformat(timespec="seconds")
    conn.execute("DELETE FROM purged WHERE purged_on < ?", (tomb_cutoff,))
    conn.commit()
    if ids:
        conn.execute("VACUUM")   # the file is committed; don't carry free pages
    return len(ids)


def prune(conn: sqlite3.Connection, keep_days: int = 30,
          store_roles: frozenset[str] | None = None) -> int:
    """Drop rows nobody will ever look at again.

    Rows outside store_roles go entirely (they're no longer being stored);
    otherwise long-removed mid/senior rows go. Mid/senior rows are never
    announced, and repost detection only uses intern/new_grad history.
    """
    if store_roles is not None:
        roles = sorted(store_roles)
        cur = conn.execute(
            f"DELETE FROM jobs WHERE role_type NOT IN ({','.join('?' * len(roles))})",
            roles)
    else:
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=keep_days)).isoformat(timespec="seconds")
        cur = conn.execute(
            "DELETE FROM jobs WHERE status = 'removed' AND last_seen < ? "
            "AND role_type IN ('mid', 'senior')", (cutoff,))
    conn.commit()
    if cur.rowcount:
        conn.execute("VACUUM")
    return cur.rowcount


def recheck_workday_removals(conn: sqlite3.Connection, per_run: int = 800) -> None:
    """One-time repair, a batch per run: before 2026-10-03 a Workday job the
    keyword sweep couldn't find was marked removed even when still open (58%
    of a sample were). Re-check removals from the 14 days before the fix and
    reopen the live ones. Finishes on its own; the cursor lives in meta."""
    from . import enrich
    if db._meta_get(conn, "workday_recheck_done") == "1":
        return
    cursor = db._meta_get(conn, "workday_recheck_cursor") or "2026-09-19"
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT job_id, source, apply_url, last_seen FROM jobs WHERE status = 'removed' "
        "AND source = 'workday' AND last_seen > ? AND last_seen < '2026-10-04' "
        "ORDER BY last_seen LIMIT ?", (cursor, per_run))]
    if not rows:
        db._meta_set(conn, "workday_recheck_done", "1")
        conn.commit()
        return
    keep = enrich.still_open(rows, limit=per_run)
    live = [r["job_id"] for r in rows if r["job_id"] in keep]
    # Unknown answers (rate limits) count as open here too; a wrongly reopened
    # job is retired again, with a definite check, the next time it's unseen.
    db.reopen(conn, live)
    db._meta_set(conn, "workday_recheck_cursor", rows[-1]["last_seen"])
    conn.commit()
    log.info(f"Workday removal repair: reopened {len(live)} of {len(rows)} re-checked jobs")
