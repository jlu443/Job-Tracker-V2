"""jobs.db schema and migrations.

Columns are declared in COLUMNS and added on connect when missing (cheap and
idempotent, so a new column is one line here). Changes to existing data are
numbered MIGRATIONS, each run exactly once per database; the version reached
is stored in meta.schema_version.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from urllib.parse import urlparse

from . import classify, dates, dedupe, phd

log = logging.getLogger(__name__)


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id      TEXT PRIMARY KEY,
    company     TEXT NOT NULL,
    title       TEXT NOT NULL,
    apply_url   TEXT NOT NULL,
    location    TEXT,
    role_type   TEXT CHECK(role_type IN ('intern','new_grad','mid','senior')),
    posted_on   TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT 'workday',
    sponsorship TEXT NOT NULL DEFAULT '',
    clearance   TEXT NOT NULL DEFAULT '',
    grad_year   TEXT NOT NULL DEFAULT '',
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- Every (source, company) board that has completed a scrape at least once.
-- Aggregators and curated lists are one board each, company '*'.
-- Ids deleted by purge_old(). Remembered so a listing that is still open
-- isn't re-inserted (and re-announced) as "new" on the next run.
CREATE TABLE IF NOT EXISTS purged (
    job_id    TEXT PRIMARY KEY,
    purged_on TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS boards (
    source        TEXT NOT NULL,
    company       TEXT NOT NULL,
    first_scraped TEXT NOT NULL,
    PRIMARY KEY (source, company)
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_role   ON jobs(role_type);
"""


COLUMNS = {
    "posted_on": "TEXT NOT NULL DEFAULT ''",
    "source": "TEXT NOT NULL DEFAULT 'workday'",
    "sponsorship": "TEXT NOT NULL DEFAULT ''",
    "clearance": "TEXT NOT NULL DEFAULT ''",
    "grad_year": "TEXT NOT NULL DEFAULT ''",
    "job_key": "TEXT NOT NULL DEFAULT ''",
    "category": "TEXT NOT NULL DEFAULT ''",
    "repost": "TEXT NOT NULL DEFAULT ''",
    "repost_of": "TEXT NOT NULL DEFAULT ''",
    "applicants": "TEXT NOT NULL DEFAULT ''",
    "relisted_on": "TEXT NOT NULL DEFAULT ''",
    "bump_count": "INTEGER NOT NULL DEFAULT 0",
    "announced_at": "TEXT NOT NULL DEFAULT ''",
    "research_track": "TEXT NOT NULL DEFAULT ''",
    "citizenship": "TEXT NOT NULL DEFAULT ''",
    "opt_cpt": "TEXT NOT NULL DEFAULT ''",
    "pay": "TEXT NOT NULL DEFAULT ''",
    # The employer's own apply link when an aggregator (Indeed) reveals it.
    "direct_url": "TEXT NOT NULL DEFAULT ''",
    # When the description was read; '' = never read (flags unknown), set
    # with all flags '' = read but nothing mentioned.
    "checked_at": "TEXT NOT NULL DEFAULT ''",
}


def _migrate_v2(conn: sqlite3.Connection) -> None:
    """One-time backfill for rows written by the v1 pipeline.

    * Workday ids gain a tenant namespace (R12345 is not unique across
      companies) and match what scraper.job_id_for() now produces, so
      existing rows are recognized next run instead of re-announced.
    * Workday's relative "Posted 3 Days Ago" becomes an ISO date, anchored
      at the row's first_seen.
    * job_key / category are filled in for every row.
    """
    log.info("Migrating jobs.db to schema v2 ...")
    rows = conn.execute(
        "SELECT job_id, apply_url, posted_on, first_seen FROM jobs "
        "WHERE source = 'workday' AND job_id NOT LIKE 'wd\\_%' ESCAPE '\\'").fetchall()
    for r in rows:
        new_id = dedupe.canonical_job_id(r["apply_url"])
        if not new_id:
            tenant = (urlparse(r["apply_url"]).hostname or "").split(".")[0]
            new_id = f"wd_{tenant.lower()}_{r['job_id']}"
        anchor = datetime.fromisoformat(r["first_seen"]).date()
        posted = dates.relative_to_iso(r["posted_on"], anchor)
        cur = conn.execute("UPDATE OR IGNORE jobs SET job_id = ?, posted_on = ? "
                           "WHERE job_id = ?", (new_id, posted, r["job_id"]))
        if cur.rowcount == 0:   # a twin already holds new_id; this row is redundant
            conn.execute("DELETE FROM jobs WHERE job_id = ?", (r["job_id"],))

    # Boards already represented in the DB have had their backlog stored.
    conn.execute(
        "INSERT OR IGNORE INTO boards (source, company, first_scraped) "
        "SELECT source, CASE WHEN source IN ('indeed','linkedin','glassdoor',"
        "'zip_recruiter','google','simplify') THEN '*' ELSE company END, MIN(first_seen) "
        "FROM jobs GROUP BY 1, 2")

    # Re-run the (improved) keyword classifier over stored titles. Otherwise
    # e.g. "ASIC Engineer - New College Grad 2026", stored as 'mid' by v1,
    # is pruned as mid and then re-announced as a "new" new_grad job.
    rows = conn.execute("SELECT job_id, title, role_type FROM jobs").fetchall()
    conn.executemany(
        "UPDATE jobs SET role_type = ? WHERE job_id = ?",
        [(new, r["job_id"]) for r in rows
         if (new := classify.classify_by_keyword(r["title"])) and new != r["role_type"]])

    rows = conn.execute("SELECT job_id, company, title, location FROM jobs").fetchall()
    conn.executemany(
        "UPDATE jobs SET job_key = ?, category = ? WHERE job_id = ?",
        [(dedupe.fuzzy_key(r["company"], r["title"], r["location"]) or "",
          classify.categorize(r["title"]), r["job_id"]) for r in rows])


def _research_track_backfill(conn: sqlite3.Connection) -> None:
    """Title-only tracks for rows stored before src/phd.py existed;
    description-based tracks arrive as boards are re-scraped/enriched."""
    rows = conn.execute("SELECT job_id, title FROM jobs WHERE research_track = ''").fetchall()
    conn.executemany("UPDATE jobs SET research_track = ? WHERE job_id = ?",
                     [(t, r[0]) for r in rows if (t := phd.track(r[1]))])


def _relabel_senior_titles(conn: sqlite3.Connection) -> None:
    """Classifier v5's "Associate ..." rule briefly matched "Senior Associate
    Engineer" (2026-10-03). Stored entry-level rows whose title the rules now
    call senior are relabeled; prune() then drops them (not a stored role)."""
    rows = conn.execute("SELECT job_id, title FROM jobs "
                        "WHERE role_type IN ('intern', 'new_grad')").fetchall()
    conn.executemany("UPDATE jobs SET role_type = 'senior' WHERE job_id = ?",
                     [(r[0],) for r in rows if classify.classify_by_keyword(r[1]) == "senior"])


def _relabel_student_department_titles(conn: sqlite3.Connection) -> None:
    """"Student" used to count as an intern word even in staff titles like
    "Student Success Coach" or "Student Services Coordinator" (fixed
    2026-10-03). Rows the old rule labeled intern whose title no longer looks
    entry-level become 'mid'; prune() drops them. Curated-list rows keep
    their human label."""
    import re
    rows = conn.execute("SELECT job_id, title FROM jobs WHERE role_type = 'intern' "
                        "AND source != 'simplify'").fetchall()
    conn.executemany("UPDATE jobs SET role_type = 'mid' WHERE job_id = ?",
                     [(r[0],) for r in rows
                      if re.search(r"\bstudent\b", r[1], re.I)
                      and classify.classify_by_keyword(r[1]) not in ("intern", "new_grad")])


def _relabel_campus_and_fellowship_staff(conn: sqlite3.Connection) -> None:
    """"Campus" counted as new-grad even as a place ("MRI Technologist - Main
    Campus") and in staff titles ("Campus Director"); "fellowship" counted as
    intern in "Fellowship Coordinator" or "Fellowship-Trained Physician";
    "University Recruiter" counted as new-grad (fixed 2026-10-03). Stored
    rows with those words are relabeled by the current rules (no rule ->
    'mid'); prune() drops the ones no longer entry-level. Curated-list rows
    keep their human label."""
    import re
    words = re.compile(r"campus|fellow|recruit|talent\s+acquisition|work[\s-]*study", re.I)
    rows = conn.execute("SELECT job_id, title, role_type FROM jobs "
                        "WHERE role_type IN ('intern', 'new_grad') "
                        "AND source != 'simplify'").fetchall()
    conn.executemany("UPDATE jobs SET role_type = ? WHERE job_id = ?",
                     [(new, r[0]) for r in rows
                      if words.search(r[1])
                      and (new := classify.classify_by_keyword(r[1]) or "mid") != r[2]])


# (version, function). Append new ones; never renumber or edit applied ones.
MIGRATIONS = [
    (2, _migrate_v2),
    (3, _research_track_backfill),
    (4, _relabel_senior_titles),
    (5, _relabel_student_department_titles),
    (6, _relabel_campus_and_fellowship_staff),
]


def _meta(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def version(conn: sqlite3.Connection) -> int:
    v = _meta(conn, "schema_version")
    # Databases from before MIGRATIONS recorded step 3 in its own flag.
    if v == "2" and _meta(conn, "research_track_backfill") == "1":
        return 3
    return int(v) if v else 1


def migrate(conn: sqlite3.Connection) -> None:
    """Create tables, add missing columns, then run pending migrations."""
    conn.executescript(SCHEMA)
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    for col, decl in COLUMNS.items():
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {decl}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_source ON jobs(source)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_key ON jobs(job_key)")
    current = version(conn)
    # Record it explicitly (a legacy database's version comes from the old
    # flag deleted below).
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                 (str(current),))
    for number, step in MIGRATIONS:
        if number > current:
            step(conn)
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                         (str(number),))
            conn.commit()
    conn.execute("DELETE FROM meta WHERE key = 'research_track_backfill'")
    conn.commit()
