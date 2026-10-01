from datetime import datetime, timedelta, timezone

from src import db, sheets
from src.posting import JobPosting


def _job(jid, title, loc, category_title=None, hours_ago=1, source="greenhouse"):
    return JobPosting(jid, "Acme", title, f"https://x/{jid}", loc, "2026-09-20", source)


def test_build_tabs_uses_announcements_and_keeps_all_stored():
    conn = db.connect(":memory:")
    posts = [_job("gh_1", "Software Engineer Intern", "Austin, TX"),
             _job("gh_2", "Sales Intern", "Austin, TX"),                  # never announced
             _job("gh_4", "Data Science Intern", "Boston, MA"),
             _job("gh_5", "Senior Engineer", "Austin, TX")]               # not stored role
    roles = {"gh_5": "senior"}
    db.sync(conn, posts, lambda p: roles.get(p.job_id, "intern"), set(), set(),
            store_roles=frozenset({"intern", "new_grad"}))
    db.mark_announced(conn, ["gh_1", "gh_4"])
    conn.execute("UPDATE jobs SET status = 'removed' WHERE job_id = 'gh_2'")
    old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(timespec="seconds")
    conn.execute("UPDATE jobs SET announced_at = ? WHERE job_id = 'gh_4'", (old,))

    tabs = sheets.build_tabs(conn, include_all=True)
    ids = lambda tab: sorted(r[-1] for r in tabs[tab])
    assert ids("Today") == ["gh_1"]
    assert ids("This Week") == ["gh_1", "gh_4"]
    assert ids("All Open") == ["gh_1", "gh_4"]                        # closed gh_2 left out
    today = tabs["Today"][0]
    assert today[0] == '=HYPERLINK("https://x/gh_1", "Apply")' and len(today) == len(sheets.COLUMNS)
    assert "All Open" not in sheets.build_tabs(conn, include_all=False)


def test_discord_grouping_and_hot_flag():
    from datetime import date
    from src import notify
    today = date.today().isoformat()
    jobs = [
        {"job_id": "a", "company": "Acme", "title": "SWE Intern", "apply_url": "https://a",
         "location": "Austin, TX", "posted_on": today, "role_type": "intern",
         "category": "software", "applicants": "Be among the first 25 applicants"},
        {"job_id": "b", "company": "Beta", "title": "Data Intern", "apply_url": "https://b",
         "location": "Boston, MA", "posted_on": "2026-01-01", "role_type": "intern",
         "category": "data_ml", "repost": "stale"},
        {"job_id": "c", "company": "Gamma", "title": "SWE New Grad", "apply_url": "https://c",
         "location": "NYC", "posted_on": today, "role_type": "new_grad",
         "category": "software", "applicants": "212 applicants"},
    ]
    embeds = notify._embeds(jobs)
    assert [e["title"] for e in embeds] == ["💻 Software (2)", "📊 Data / ML (1)"]
    first, second = embeds[0]["description"].split("\n")
    assert first.startswith("⭐ **Acme**") and not second.startswith("⭐")   # 212 applicants
    assert "🕰️ old post" in embeds[1]["description"]


def test_flag_columns_distinguish_unchecked_from_not_mentioned():
    base = {"apply_url": "https://x", "company": "A", "title": "T", "role_type": "intern",
            "source": "greenhouse", "status": "active", "job_id": "gh_1"}
    col = lambda name: sheets.COLUMNS.index(name)
    unchecked = sheets._row({**base})
    read_nothing = sheets._row({**base, "checked_at": "2026-10-01T00:00:00+00:00"})
    flagged = sheets._row({**base, "checked_at": "x", "sponsorship": "no",
                           "citizenship": "required", "clearance": "none"})
    assert unchecked[col("Visa sponsorship")] == ""
    assert read_nothing[col("Visa sponsorship")] == "Not mentioned"
    assert [flagged[col(c)] for c in ("Visa sponsorship", "US citizenship", "Clearance")] == \
        ["Not offered", "Required", "Not required"]


def test_sync_stores_scrape_time_flags_and_backfills_unchecked_rows():
    conn = db.connect(":memory:")
    plain = _job("gh_1", "Software Engineer Intern", "Austin, TX")
    db.sync(conn, [plain], lambda p: "intern", set(), set())
    assert conn.execute("SELECT checked_at FROM jobs").fetchone()[0] == ""
    from dataclasses import replace
    described = replace(plain, sponsorship="no", citizenship="required", clearance="yes",
                        checked=True)
    db.sync(conn, [described], lambda p: "intern", set(), set())
    row = conn.execute("SELECT sponsorship, citizenship, clearance, checked_at FROM jobs").fetchone()
    assert tuple(row)[:3] == ("no", "required", "yes") and row[3]
    assert [j["job_id"] for j in db.unchecked_open(conn, 10)] == []
