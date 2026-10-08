"""SQLite persistence with new/updated/removed tracking.

The DB file is committed back to the repo by the GitHub Actions workflow, so it
survives between runs on ephemeral runners — which is also why prune() exists:
every byte here is re-committed on every run.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import classify, dates, dedupe, phd, repost, schema


# Aggregator searches are time-windowed (JobSpy hours_old), so a job missing
# from one run's results hasn't been taken down; it has just aged out of the
# search. These rows expire by age instead of by absence.
AGGREGATOR_SOURCES = {"indeed", "linkedin", "glassdoor", "zip_recruiter", "google"}
# Sources that only relay other sites' jobs; a first-party scrape supersedes them.
_SECONDHAND = {"simplify"} | AGGREGATOR_SOURCES


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
    schema.migrate(conn)
    return conn


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


# Empty description reads before the backlog stops retrying a job (closed
# postings, or ones whose page really has no text).
MAX_ENRICH_TRIES = 3


def unchecked_open(conn: sqlite3.Connection, limit: int) -> list[dict]:
    """Open intern/new-grad rows whose description was never read, in the
    order the Sheet most needs them: PhD/research internships, announced
    jobs, then newest."""
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(
        "SELECT * FROM jobs WHERE status = 'active' AND checked_at = '' "
        f"AND enrich_tries < {MAX_ENRICH_TRIES} "
        "AND role_type IN ('intern', 'new_grad') "
        "ORDER BY (role_type = 'intern' AND research_track != '') DESC, "
        "(announced_at != '') DESC, first_seen DESC LIMIT ?", (limit,))]


def undated_open(conn: sqlite3.Connection, source: str, limit: int) -> list[dict]:
    """Open rows of a source whose post date is unknown, newest first, minus
    ones already read MAX_ENRICH_TRIES times without finding one."""
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(
        "SELECT * FROM jobs WHERE status = 'active' AND source = ? AND posted_on = '' "
        f"AND enrich_tries < {MAX_ENRICH_TRIES} ORDER BY first_seen DESC LIMIT ?",
        (source, limit))]


def reopen(conn: sqlite3.Connection, job_ids: list[str]) -> None:
    conn.executemany("UPDATE jobs SET status = 'active', last_seen = ? WHERE job_id = ?",
                     [(_now(), jid) for jid in job_ids])
    conn.commit()


def mark_undated(conn: sqlite3.Connection, job_ids: list[str]) -> None:
    """Count a read that found no post date toward MAX_ENRICH_TRIES."""
    conn.executemany("UPDATE jobs SET enrich_tries = enrich_tries + 1 WHERE job_id = ?",
                     [(jid,) for jid in job_ids])
    conn.commit()


def mark_announced(conn: sqlite3.Connection, job_ids: list[str]) -> None:
    """The Sheet's Today/This Week tabs and the daily digest are built from
    announced_at, so silently backfilled rows never show up as 'new'."""
    now = _now()
    conn.executemany("UPDATE jobs SET announced_at = ? WHERE job_id = ?",
                     [(now, jid) for jid in job_ids])
    conn.commit()


def productive_boards(conn: sqlite3.Connection, source: str) -> set[str]:
    """Companies on `source` with an open intern/new_grad job right now."""
    return {r[0] for r in conn.execute(
        "SELECT DISTINCT company FROM jobs WHERE source = ? AND status = 'active' "
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
        "UPDATE jobs SET sponsorship = ?, clearance = ?, grad_year = ?, citizenship = ?, "
        "opt_cpt = ?, pay = COALESCE(NULLIF(?, ''), pay), "
        "applicants = ?, repost = ?, repost_of = ?, research_track = ?, "
        "checked_at = COALESCE(NULLIF(?, ''), checked_at), "
        "enrich_tries = enrich_tries + ?, "
        "posted_on = CASE WHEN posted_on = '' THEN ? ELSE posted_on END WHERE job_id = ?",
        [(j.get("sponsorship", ""), j.get("clearance", ""), j.get("grad_year", ""),
          j.get("citizenship", ""), j.get("opt_cpt", ""), j.get("pay", ""),
          j.get("applicants", ""), j.get("repost", ""),
          j.get("repost_of", ""), j.get("research_track", ""), j.get("checked_at", ""),
          int(bool(j.get("enrich_attempted")) and not j.get("checked_at")),
          j.get("posted_on", ""), j["job_id"]) for j in jobs],
    )
    # A detail page that says the posting is gone (LinkedIn 404).
    closed = [j["job_id"] for j in jobs if j.get("closed")]
    if closed:
        conn.executemany("UPDATE jobs SET status = 'removed', last_seen = ? WHERE job_id = ?",
                         [(_now(), jid) for jid in closed])
    conn.commit()


def sync(conn: sqlite3.Connection, postings: list, role_for,
         complete_scopes: set[tuple[str, str]], complete_sources: set[str],
         aggregator_ttl_days: int = 21,
         store_roles: frozenset[str] | None = None,
         max_age_days: int | None = None,
         age_exempt: frozenset = frozenset(),
         still_open=None) -> UpsertResult:
    """Reconcile this run's postings against the DB.

    A job is only marked removed when the scrape that should have returned it
    succeeded: its (source, company) is in complete_scopes, or its whole
    source is in complete_sources. A timeout on one board no longer "removes"
    that board's jobs. Aggregator rows expire after aggregator_ttl_days unseen.
    still_open(rows) -> ids, when given, gets the rows about to be removed
    and returns those that must stay (their job page is still up).

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
        # Exempt from the age limit: sources whose listings run for months,
        # and titles recruiting for a season still ahead ("Summer 2027").
        exempt = dates.outlives_age_limit(p.source, p.title, p.posted_on, age_exempt)
        # A tombstone stops an aged-out listing from coming back as "new".
        # Exempt listings are deleted only once unseen, so seeing one again
        # means it's open again.
        if p.job_id in purged and not exempt:
            continue
        if p.job_id not in known:
            age = dates.age_days(p.posted_on)
            if (max_age_days is not None and age is not None and age > max_age_days
                    and not exempt):
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
                "sponsorship": p.sponsorship, "clearance": p.clearance,
                "citizenship": p.citizenship, "grad_year": p.grad_year, "pay": p.pay,
                "opt_cpt": p.opt_cpt,
                "direct_url": p.direct_url,
                "checked_at": now if p.checked else "",
                "research_track": p.research_track or phd.track(p.title),
            }
            conn.execute(
                "INSERT INTO jobs (job_id, company, title, apply_url, location, "
                "role_type, posted_on, source, sponsorship, clearance, citizenship, "
                "opt_cpt, grad_year, pay, direct_url, checked_at, first_seen, last_seen, "
                "status, job_key, category, research_track) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?,?,?)",
                (p.job_id, p.company, p.title, p.apply_url, p.location, role,
                 p.posted_on, p.source, p.sponsorship, p.clearance, p.citizenship,
                 p.opt_cpt, p.grad_year, p.pay, p.direct_url, job["checked_at"], now, now,
                 job["job_key"], job["category"], job["research_track"]),
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
                # a row stored without an employer takes one found later
                "company = CASE WHEN company = '' THEN ? ELSE company END, "
                "posted_on = CASE WHEN posted_on = '' OR (? != '' AND ? < posted_on) "
                "THEN ? ELSE posted_on END, "
                # a description-based track (scrape time) upgrades a title-only one
                "research_track = CASE WHEN ? != '' THEN ? ELSE research_track END, "
                # Scrape-time description flags fill rows never checked before
                # (e.g. stored before this existed); they never overwrite.
                "sponsorship = CASE WHEN checked_at = '' AND ? THEN ? ELSE sponsorship END, "
                "clearance = CASE WHEN checked_at = '' AND ? THEN ? ELSE clearance END, "
                "citizenship = CASE WHEN checked_at = '' AND ? THEN ? ELSE citizenship END, "
                "opt_cpt = CASE WHEN checked_at = '' AND ? THEN ? ELSE opt_cpt END, "
                "grad_year = CASE WHEN checked_at = '' AND ? THEN ? ELSE grad_year END, "
                # pay / direct link: fill whenever the source provides one
                "pay = CASE WHEN ? != '' THEN ? ELSE pay END, "
                "direct_url = CASE WHEN ? != '' THEN ? ELSE direct_url END, "
                "checked_at = CASE WHEN checked_at = '' AND ? THEN ? ELSE checked_at END "
                "WHERE job_id = ?",
                (now, p.title, p.apply_url, p.location, p.company,
                 p.posted_on, p.posted_on, p.posted_on,
                 p.research_track, p.research_track,
                 p.checked, p.sponsorship or "", p.checked, p.clearance,
                 p.checked, p.citizenship, p.checked, p.opt_cpt, p.checked, p.grad_year,
                 p.pay, p.pay, p.direct_url, p.direct_url, p.checked, now,
                 p.job_id),
            )
            # A job first stored from a curated list or aggregator under its
            # ATS id, now confirmed by that ATS's own scraper: take on the
            # first-party source and company name. Removal is scoped by
            # (source, company), so without this a closed listing would only
            # ever be retired when the curated list noticed.
            if p.source not in _SECONDHAND:
                conn.execute(
                    f"UPDATE jobs SET source = ?, company = ?, job_key = ? WHERE job_id = ? "
                    f"AND source IN ({','.join('?' * len(_SECONDHAND))})",
                    (p.source, p.company,
                     dedupe.fuzzy_key(p.company, p.title, p.location) or "", p.job_id,
                     *sorted(_SECONDHAND)))
            updated += 1

    ttl_cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=aggregator_ttl_days)).isoformat(timespec="seconds")
    gone, unseen = [], []
    for r in conn.execute("SELECT job_id, source, company, last_seen, apply_url, title "
                          "FROM jobs WHERE status = 'active'"):
        if r["job_id"] in seen_ids:
            continue
        if r["source"] in AGGREGATOR_SOURCES:
            if r["last_seen"] < ttl_cutoff:
                gone.append(r["job_id"])
        elif r["source"] in complete_sources or (r["source"], r["company"]) in complete_scopes:
            unseen.append(dict(r))
    keep = still_open(unseen) if still_open and unseen else set()
    gone += [r["job_id"] for r in unseen if r["job_id"] not in keep]
    if gone:
        conn.executemany(
            "UPDATE jobs SET status = 'removed', last_seen = ? WHERE job_id = ?",
            [(now, jid) for jid in gone])

    conn.commit()
    return UpsertResult(new_jobs=new_jobs, updated=updated, removed=len(gone),
                        bumped=bumped)


