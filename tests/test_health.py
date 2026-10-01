import sqlite3

from src import health


def _runs(conn, history, now_counts, now_boards=None):
    for i, n in enumerate(history):
        health.record(conn, f"2026-09-2{i}T00:00:00+00:00", {"greenhouse": n}, {})
    health.record(conn, "2026-09-29T00:00:00+00:00", now_counts, now_boards or {})


def test_flags_collapsed_source_and_failed_boards():
    conn = sqlite3.connect(":memory:")
    _runs(conn, [58000, 58100, 57900, 58050], {}, {"greenhouse": (822, 700)})
    problems = health.check(conn, "2026-09-29T00:00:00+00:00", 600, {})
    assert any("greenhouse: 0 postings" in p for p in problems)
    assert any("700 of 822 boards failed" in p for p in problems)


def test_quiet_when_normal_or_history_too_short():
    conn = sqlite3.connect(":memory:")
    _runs(conn, [58000, 58100, 57900], {"greenhouse": 56000}, {"greenhouse": (822, 3)})
    assert health.check(conn, "2026-09-29T00:00:00+00:00", 600, {}) == []
    fresh = sqlite3.connect(":memory:")
    _runs(fresh, [100], {"greenhouse": 0})
    assert health.check(fresh, "2026-09-29T00:00:00+00:00", 600, {}) == []


def test_slow_run_and_alert_cooldown():
    conn = sqlite3.connect(":memory:")
    _runs(conn, [1, 1, 1], {"greenhouse": 1})
    problems = health.check(conn, "2026-09-29T00:00:00+00:00", 27 * 60, {})
    assert problems == ["run took 27 min (CI kills it at 30)"]
    assert health.due_alerts(conn, problems) == problems
    assert health.due_alerts(conn, problems) == []      # once a day


def test_disabled_source_is_not_flagged():
    conn = sqlite3.connect(":memory:")
    _runs(conn, [1400, 1390, 1380], {}, {"greenhouse": (0, 0)})
    assert health.check(conn, "2026-09-29T00:00:00+00:00", 600,
                        {"disabled_sources": ["greenhouse"]}) == []
