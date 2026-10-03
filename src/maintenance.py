"""Retention and housekeeping on jobs.db, run after each sync.

    purge_old       delete listings past the post-date cutoff (tombstoned)
    prune           delete rows outside store_roles
    apply_renames   move rows to a board's new company name
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from . import dates, dedupe
from .db import _now


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
    exempt_unseen_days. Deleted ids go into `purged` so a still-open listing
    isn't re-announced; tombstones themselves expire after tombstone_days.
    """
    cutoff = (dates.today() - timedelta(days=max_age_days)).isoformat()
    unseen = (datetime.now(timezone.utc)
              - timedelta(days=exempt_unseen_days)).isoformat(timespec="seconds")
    now = _now()
    ex = sorted(exempt_sources) or [""]
    marks = ",".join("?" * len(ex))
    rows = conn.execute(
        f"SELECT job_id FROM jobs WHERE "
        f"(source NOT IN ({marks}) AND CASE WHEN posted_on != '' THEN MAX(posted_on, relisted_on) "
        f"ELSE substr(first_seen, 1, 10) END < ?) "
        f"OR (source IN ({marks}) AND last_seen < ?)", (*ex, cutoff, *ex, unseen)).fetchall()
    ids = [r[0] for r in rows]
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
