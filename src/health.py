"""Per-run source health: notice when a source silently breaks.

A scraper that starts returning nothing (API change, block, rename) still
exits cleanly, so the run reports success while coverage quietly vanishes.
Each run records how many postings every source produced; a source far below
its own recent median, a burst of failed boards, or a run creeping toward the
CI timeout raises an alert. Each distinct problem alerts at most once a day.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from statistics import median

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_at     TEXT NOT NULL,
    source     TEXT NOT NULL,
    postings   INTEGER NOT NULL,
    boards     INTEGER NOT NULL DEFAULT 0,
    incomplete INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_at, source)
);
CREATE TABLE IF NOT EXISTS alerts (key TEXT PRIMARY KEY, last_sent TEXT NOT NULL);
"""


def record(conn: sqlite3.Connection, run_at: str, counts: dict[str, int],
           boards: dict[str, tuple[int, int]]) -> None:
    """counts: source → postings; boards: source → (attempted, incomplete)."""
    conn.executescript(_SCHEMA)
    conn.executemany(
        "INSERT OR REPLACE INTO runs (run_at, source, postings, boards, incomplete) "
        "VALUES (?, ?, ?, ?, ?)",
        [(run_at, src, n, *boards.get(src, (0, 0))) for src, n in counts.items()]
        + [(run_at, src, 0, *boards[src]) for src in boards if src not in counts])
    # Keep ~2 months of history; it's only used for medians.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat(timespec="seconds")
    conn.execute("DELETE FROM runs WHERE run_at < ?", (cutoff,))
    conn.commit()


def check(conn: sqlite3.Connection, run_at: str, elapsed_s: float,
          settings: dict) -> list[str]:
    """Problems with this run, compared to each source's own recent history."""
    cfg = settings.get("health", {})
    window = cfg.get("history_runs", 12)
    drop_ratio = cfg.get("drop_ratio", 0.3)
    min_baseline = cfg.get("min_baseline", 20)
    conn.executescript(_SCHEMA)

    problems = []
    current = {r[0]: (r[1], r[2], r[3]) for r in conn.execute(
        "SELECT source, postings, boards, incomplete FROM runs WHERE run_at = ?", (run_at,))}
    sources = {r[0] for r in conn.execute("SELECT DISTINCT source FROM runs")}
    for src in sorted(sources):
        history = [r[0] for r in conn.execute(
            "SELECT postings FROM runs WHERE source = ? AND run_at < ? "
            "ORDER BY run_at DESC LIMIT ?", (src, run_at, window))]
        if len(history) < 3:
            continue            # not enough history to judge yet
        baseline = median(history)
        now, attempted, incomplete = current.get(src, (0, 0, 0))
        if baseline >= min_baseline and now < baseline * drop_ratio:
            problems.append(f"{src}: {now} postings vs usual ~{baseline:.0f}")
        if attempted >= 10 and incomplete / attempted > 0.5:
            problems.append(f"{src}: {incomplete} of {attempted} boards failed to scrape")

    limit = cfg.get("max_run_minutes", 25)
    if elapsed_s > limit * 60:
        problems.append(f"run took {elapsed_s / 60:.0f} min (CI kills it at 30)")
    return problems


def due_alerts(conn: sqlite3.Connection, problems: list[str],
               cooldown_hours: int = 24) -> list[str]:
    """Problems not already alerted within cooldown_hours; marks them sent."""
    conn.executescript(_SCHEMA)
    now = datetime.now(timezone.utc)
    due = []
    for p in problems:
        key = p.split(":", 1)[0] if ":" in p else p.split(" ", 2)[0]
        row = conn.execute("SELECT last_sent FROM alerts WHERE key = ?", (key,)).fetchone()
        if row and (now - datetime.fromisoformat(row[0])).total_seconds() < cooldown_hours * 3600:
            continue
        conn.execute("INSERT OR REPLACE INTO alerts (key, last_sent) VALUES (?, ?)",
                     (key, now.isoformat(timespec="seconds")))
        due.append(p)
    conn.commit()
    return due
