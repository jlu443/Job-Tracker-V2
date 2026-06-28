"""SQLite persistence with new/updated/removed tracking.

The DB file is committed back to the repo by the GitHub Actions workflow, so it
survives between runs on ephemeral runners.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id      TEXT PRIMARY KEY,
    company     TEXT NOT NULL,
    title       TEXT NOT NULL,
    apply_url   TEXT NOT NULL,
    location    TEXT,
    role_type   TEXT CHECK(role_type IN ('intern','new_grad','mid','senior')),
    posted_on   TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT 'workday',
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active'
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_role   ON jobs(role_type);
CREATE INDEX IF NOT EXISTS idx_jobs_source ON jobs(source);
"""

_MIGRATIONS = [
    "ALTER TABLE jobs ADD COLUMN posted_on TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE jobs ADD COLUMN source TEXT NOT NULL DEFAULT 'workday'",
]


@dataclass
class UpsertResult:
    new_jobs: list[dict]      # rows seen for the first time this run
    updated: int              # rows that already existed and were refreshed
    removed: int              # rows that dropped out (marked 'removed')


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    # Apply migrations idempotently — SQLite ALTER TABLE errors if column exists.
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    for stmt in _MIGRATIONS:
        col = stmt.split("ADD COLUMN")[1].strip().split()[0]
        if col not in existing_cols:
            conn.execute(stmt)
    conn.commit()
    return conn


def sync(conn: sqlite3.Connection, postings: list, role_for) -> UpsertResult:
    """Reconcile this run's postings against the DB.

    `postings` is a list of scraper.JobPosting; `role_for(posting)` returns the
    classified role_type. Returns what changed so the notifier can act on it.
    """
    now = _now()
    new_jobs: list[dict] = []
    updated = 0
    seen_ids: set[str] = set()

    for p in postings:
        seen_ids.add(p.job_id)
        row = conn.execute("SELECT job_id FROM jobs WHERE job_id = ?",
                           (p.job_id,)).fetchone()
        if row is None:
            role = role_for(p)
            source = getattr(p, "source", "workday")
            conn.execute(
                "INSERT INTO jobs (job_id, company, title, apply_url, location, "
                "role_type, posted_on, source, first_seen, last_seen, status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,'active')",
                (p.job_id, p.company, p.title, p.apply_url, p.location,
                 role, p.posted_on, source, now, now),
            )
            new_jobs.append({
                "job_id": p.job_id, "company": p.company, "title": p.title,
                "apply_url": p.apply_url, "location": p.location,
                "role_type": role, "posted_on": p.posted_on, "source": source,
            })
        else:
            conn.execute(
                "UPDATE jobs SET last_seen = ?, status = 'active', "
                "title = ?, apply_url = ?, location = ? WHERE job_id = ?",
                (now, p.title, p.apply_url, p.location, p.job_id),
            )
            updated += 1

    # Mark anything we used to see but didn't this run as removed.
    # (Only flips currently-active rows so the count is accurate.)
    removed = 0
    active = conn.execute(
        "SELECT job_id FROM jobs WHERE status = 'active'").fetchall()
    gone = [r["job_id"] for r in active if r["job_id"] not in seen_ids]
    if gone:
        conn.executemany(
            "UPDATE jobs SET status = 'removed', last_seen = ? WHERE job_id = ?",
            [(now, jid) for jid in gone],
        )
        removed = len(gone)

    conn.commit()
    return UpsertResult(new_jobs=new_jobs, updated=updated, removed=removed)
