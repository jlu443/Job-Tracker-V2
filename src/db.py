"""SQLite persistence with new/updated/removed tracking.

The DB file is committed back to the repo by the GitHub Actions workflow, so it
survives between runs on ephemeral runners — which is also why prune() exists:
every byte here is re-committed on every run.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from . import classify, dates, dedupe, repost

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

_COLUMNS = {
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
}

# Aggregator searches are time-windowed (JobSpy hours_old), so a job missing
# from one run's results hasn't been taken down; it has just aged out of the
# search. These rows expire by age instead of by absence.
AGGREGATOR_SOURCES = {"indeed", "linkedin", "glassdoor", "zip_recruiter", "google"}


@dataclass
class UpsertResult:
    new_jobs: list[dict]      # rows seen for the first time this run
    updated: int              # rows that already existed and were refreshed
    bumped: list[dict]        # existing rows whose source re-dated them (reposts)
    removed: int              # rows that dropped out (marked 'removed')


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _meta_get(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _meta_set(conn, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    for col, decl in _COLUMNS.items():
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {decl}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_source ON jobs(source)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_key ON jobs(job_key)")
    if _meta_get(conn, "schema_version") != "2":
        _migrate_v2(conn)
        _meta_set(conn, "schema_version", "2")
    conn.commit()
    return conn


def _migrate_v2(conn: sqlite3.Connection) -> None:
    """One-time backfill for rows written by the v1 pipeline.

    * Workday ids gain a tenant namespace (R12345 is not unique across
      companies) and match what scraper.job_id_for() now produces, so
      existing rows are recognized next run instead of re-announced.
    * Workday's relative "Posted 3 Days Ago" becomes an ISO date, anchored
      at the row's first_seen.
    * job_key / category are filled in for every row.
    """
    print("Migrating jobs.db to schema v2 ...")
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


def existing_ids(conn: sqlite3.Connection) -> set[str]:
    """All job_ids ever seen (stored or purged) — lets callers pre-classify
    only genuinely new rows."""
    return ({r[0] for r in conn.execute("SELECT job_id FROM jobs")}
            | {r[0] for r in conn.execute("SELECT job_id FROM purged")})


@dataclass
class RoleHistory:
    active: bool          # some listing of this role is live right now
    job_id: str           # most recent prior listing
    last_seen: str
    source: str


def role_history(conn: sqlite3.Connection) -> dict[str, RoleHistory]:
    """role_key → what we already know about that role. Snapshot it before
    sync() so "known" means "known before this run"."""
    out: dict[str, RoleHistory] = {}
    for r in conn.execute("SELECT job_key, job_id, last_seen, source, status FROM jobs "
                          "WHERE job_key != '' ORDER BY last_seen"):
        rk = dedupe.role_key(r["job_key"])
        prev = out.get(rk)
        out[rk] = RoleHistory(
            active=(prev.active if prev else False) or r["status"] == "active",
            job_id=r["job_id"], last_seen=r["last_seen"], source=r["source"])
    return out


def register_boards(conn: sqlite3.Connection, scopes: set[tuple[str, str]]) -> set[tuple[str, str]]:
    """Record boards that completed a scrape; return the ones seen for the
    first time, whose postings are backlog rather than news."""
    known = {(r[0], r[1]) for r in conn.execute("SELECT source, company FROM boards")}
    fresh = {s for s in scopes if s not in known}
    now = _now()
    conn.executemany("INSERT INTO boards (source, company, first_scraped) VALUES (?, ?, ?)",
                     [(src, co, now) for src, co in fresh])
    conn.commit()
    return fresh


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


def productive_boards(conn: sqlite3.Connection, source: str) -> set[str]:
    """Companies on `source` that have ever listed an intern/new_grad job."""
    return {r[0] for r in conn.execute(
        "SELECT DISTINCT company FROM jobs WHERE source = ? "
        "AND role_type IN ('intern', 'new_grad')", (source,))}


def board_of(job: dict) -> tuple[str, str]:
    """The boards-table scope a job belongs to."""
    if job["source"] in AGGREGATOR_SOURCES or job["source"] == "simplify":
        return (job["source"], "*")
    return (job["source"], job["company"])


def linkedin_observations(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """(apply_url, posted_on) of recent LinkedIn rows, for repost calibration."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    return [(r[0], r[1]) for r in conn.execute(
        "SELECT apply_url, posted_on FROM jobs WHERE source = 'linkedin' "
        "AND posted_on != '' AND first_seen >= ?", (cutoff,))]


def update_enrichment(conn: sqlite3.Connection, jobs: list[dict]) -> None:
    """Persist the flags enrich/repost set on the job dicts."""
    if not jobs:
        return
    conn.executemany(
        "UPDATE jobs SET sponsorship = ?, clearance = ?, grad_year = ?, "
        "applicants = ?, repost = ?, repost_of = ? WHERE job_id = ?",
        [(j.get("sponsorship", ""), j.get("clearance", ""), j.get("grad_year", ""),
          j.get("applicants", ""), j.get("repost", ""), j.get("repost_of", ""),
          j["job_id"]) for j in jobs],
    )
    conn.commit()


def sync(conn: sqlite3.Connection, postings: list, role_for,
         complete_scopes: set[tuple[str, str]], complete_sources: set[str],
         aggregator_ttl_days: int = 21,
         store_roles: frozenset[str] | None = None,
         max_age_days: int | None = None) -> UpsertResult:
    """Reconcile this run's postings against the DB.

    A job is only marked removed when the scrape that should have returned it
    succeeded: its (source, company) is in complete_scopes, or its whole
    source is in complete_sources. A timeout on one board no longer "removes"
    that board's jobs. Aggregator rows expire after aggregator_ttl_days unseen.

    New postings whose role isn't in store_roles are not stored (None = all):
    full-board ATS APIs return every role, and keeping hundreds of thousands
    of senior rows would push the committed DB past GitHub's 100 MB limit.
    Nor are new postings whose post date is older than max_age_days, and ids
    purge_old() already deleted are skipped entirely.
    """
    now = _now()
    purged = {r[0] for r in conn.execute("SELECT job_id FROM purged")}
    new_jobs: list[dict] = []
    updated = 0
    seen_ids: set[str] = set()
    posted_before = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT job_id, posted_on, relisted_on FROM jobs")}
    known = posted_before.keys()
    bumped: list[dict] = []

    for p in postings:
        if p.job_id in seen_ids:
            continue
        seen_ids.add(p.job_id)
        if p.job_id in purged:
            continue
        if p.job_id not in known:
            age = dates.age_days(p.posted_on)
            if max_age_days is not None and age is not None and age > max_age_days:
                continue
            role = role_for(p)
            if store_roles is not None and role not in store_roles:
                continue
            job = {
                "job_id": p.job_id, "company": p.company, "title": p.title,
                "apply_url": p.apply_url, "location": p.location,
                "role_type": role, "posted_on": p.posted_on, "source": p.source,
                "first_seen": now,
                "job_key": dedupe.fuzzy_key(p.company, p.title, p.location) or "",
                "category": p.category or classify.categorize(p.title),
                # scrape-time description (aggregators) for the enrichment pass
                "description": p.description,
                "sponsorship": p.sponsorship, "clearance": "", "grad_year": "",
            }
            conn.execute(
                "INSERT INTO jobs (job_id, company, title, apply_url, location, "
                "role_type, posted_on, source, sponsorship, first_seen, last_seen, "
                "status, job_key, category) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,'active',?,?)",
                (p.job_id, p.company, p.title, p.apply_url, p.location, role,
                 p.posted_on, p.source, p.sponsorship, now, now,
                 job["job_key"], job["category"]),
            )
            new_jobs.append(job)
        else:
            old, last_relist = posted_before[p.job_id]
            # posted_on keeps the earliest date ever seen; a later, newer date
            # on the *same* id means the listing was re-dated (LinkedIn's
            # "Reposted", Indeed/Workday refreshes).
            if repost.is_bump(old, p.posted_on) and p.posted_on != last_relist:
                conn.execute(
                    "UPDATE jobs SET relisted_on = ?, bump_count = bump_count + 1, "
                    "repost = CASE WHEN repost = '' THEN 'bumped' ELSE repost END "
                    "WHERE job_id = ?", (p.posted_on, p.job_id))
                bumped.append({"job_id": p.job_id, "company": p.company,
                               "title": p.title, "source": p.source,
                               "posted_on": old, "relisted_on": p.posted_on})
            conn.execute(
                "UPDATE jobs SET last_seen = ?, status = 'active', title = ?, "
                "apply_url = COALESCE(NULLIF(?, ''), apply_url), location = ?, "
                "posted_on = CASE WHEN posted_on = '' OR (? != '' AND ? < posted_on) "
                "THEN ? ELSE posted_on END WHERE job_id = ?",
                (now, p.title, p.apply_url, p.location,
                 p.posted_on, p.posted_on, p.posted_on, p.job_id),
            )
            updated += 1

    ttl_cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=aggregator_ttl_days)).isoformat(timespec="seconds")
    gone = []
    for r in conn.execute(
            "SELECT job_id, source, company, last_seen FROM jobs WHERE status = 'active'"):
        if r["job_id"] in seen_ids:
            continue
        if r["source"] in AGGREGATOR_SOURCES:
            if r["last_seen"] < ttl_cutoff:
                gone.append(r["job_id"])
        elif r["source"] in complete_sources or (r["source"], r["company"]) in complete_scopes:
            gone.append(r["job_id"])
    if gone:
        conn.executemany(
            "UPDATE jobs SET status = 'removed', last_seen = ? WHERE job_id = ?",
            [(now, jid) for jid in gone])

    conn.commit()
    return UpsertResult(new_jobs=new_jobs, updated=updated, removed=len(gone),
                        bumped=bumped)


def purge_old(conn: sqlite3.Connection, max_age_days: int,
              tombstone_days: int = 365) -> int:
    """Delete listings older than max_age_days, whatever their status.

    Age is measured from the post date when the source gave one (the latest
    re-listing date if it was reposted), otherwise from when we first saw it.
    Deleted ids go into `purged` so a still-open listing isn't re-announced;
    tombstones themselves expire after tombstone_days.
    """
    cutoff = (dates.today() - timedelta(days=max_age_days)).isoformat()
    now = _now()
    rows = conn.execute(
        "SELECT job_id FROM jobs WHERE "
        "CASE WHEN posted_on != '' THEN MAX(posted_on, relisted_on) "
        "ELSE substr(first_seen, 1, 10) END < ?", (cutoff,)).fetchall()
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
