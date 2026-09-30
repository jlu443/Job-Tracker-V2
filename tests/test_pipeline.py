import collections
import sqlite3
from datetime import date, datetime, timedelta, timezone

from src import db, dedupe, repost, scraper
from src.posting import JobPosting


def _p(job_id, company="Acme", title="Software Engineer Intern", source="greenhouse",
       url="", **kw):
    return JobPosting(job_id=job_id, company=company, title=title,
                      apply_url=url or f"https://example.com/{job_id}",
                      location="Austin, TX", posted_on="2026-09-20", source=source, **kw)


def test_workday_scraper_id_matches_url_mapping():
    ext = "/job/Santa-Clara/Software_Engineer_Intern_JR2017296-1"
    assert scraper.job_id_for("NVIDIA", ext) == "wd_nvidia_JR2017296-1"
    url = f"https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite{ext}"
    assert dedupe.canonical_job_id(url) == "wd_nvidia_JR2017296-1"


def test_dedupe_exact_then_fuzzy():
    ats = _p("gh_1", url="https://boards.greenhouse.io/acme/jobs/1")
    simplify_copy = _p("gh_1", source="simplify", url="https://boards.greenhouse.io/acme/jobs/1")
    indeed_copy = _p("indeed_x", source="indeed", title="Software Engineer Intern",
                     direct_url="https://boards.greenhouse.io/acme/jobs/1")
    fuzzy_copy = _p("indeed_y", source="indeed", title="Intern, Software Engineer")
    unrelated = _p("gh_2", title="Data Science Intern")
    kept, dropped = dedupe.dedupe_postings(
        [ats, unrelated, simplify_copy, indeed_copy, fuzzy_copy])
    assert [p.job_id for p in kept] == ["gh_1", "gh_2"]
    assert dropped == 3


def test_dedupe_drops_aggregator_copy_of_known_row():
    indeed_copy = _p("indeed_x", source="indeed",
                     direct_url="https://jobs.lever.co/acme/0a1b2c3d-1111-2222-3333-444455556666")
    kept, dropped = dedupe.dedupe_postings(
        [indeed_copy], known_ids={"lv_0a1b2c3d-1111-2222-3333-444455556666"})
    assert kept == [] and dropped == 1


def test_linkedin_repost_age():
    frontier = (4471636508, date(2026, 9, 26))
    # a year-old id displayed as posted last week → bumped
    assert repost.linkedin_repost_age(4309583918, "2026-09-20", frontier) > 300
    # a genuinely fresh id
    assert abs(repost.linkedin_repost_age(4470016722, "2026-09-25", frontier)) < 5
    # an early-dated card (date before id creation) is not a repost
    assert repost.linkedin_repost_age(4463532754, "2026-08-14", frontier) < 0


def test_linkedin_frontier_ignores_outliers():
    obs = [(4470000000 + i * 100_000, "2026-09-25") for i in range(20)]
    obs.append((4309583918, "2026-09-26"))   # old id with a bumped, newer date
    fid, fdate = repost.linkedin_frontier(obs)
    assert fid > 4470000000 and fdate == date(2026, 9, 25)


def test_annotate_reasons():
    frontier = (4471636508, date(2026, 9, 26))
    today = date.today().isoformat()
    jobs = [
        {"job_id": "gh_9", "job_key": "acme|intern|austin", "source": "greenhouse",
         "posted_on": today},
        {"job_id": "li_4309583918", "job_key": "b", "source": "linkedin",
         "apply_url": "https://www.linkedin.com/jobs/view/4309583918",
         "posted_on": "2026-09-20"},
        {"job_id": "gh_10", "job_key": "c", "source": "greenhouse",
         "posted_on": "2025-01-01"},
        {"job_id": "gh_11", "job_key": "d", "source": "greenhouse", "posted_on": today},
    ]
    prior = {"gh_9": ("gh_3", "2026-07-01T00:00:00+00:00")}
    repost.annotate(jobs, prior, frontier, {})
    assert [j["repost"] for j in jobs] == ["relisted", "linkedin", "stale", ""]
    assert jobs[0]["repost_of"] == "gh_3"


def _mem():
    return db.connect(":memory:")


def test_sync_only_removes_within_complete_scopes():
    conn = _mem()
    first = [_p("gh_1", company="Acme"), _p("gh_2", company="Globex"),
             _p("indeed_1", company="Initech", source="indeed")]
    db.sync(conn, first, lambda p: "intern", set(), set())
    # Next run: Acme scraped fine but gh_1 is gone; Globex's board timed out;
    # the Indeed job simply aged out of the search window.
    res = db.sync(conn, [], lambda p: "intern", {("greenhouse", "Acme")}, set())
    status = dict(conn.execute("SELECT job_id, status FROM jobs"))
    assert status == {"gh_1": "removed", "gh_2": "active", "indeed_1": "active"}
    assert res.removed == 1


def test_sync_expires_aggregator_rows_by_age():
    conn = _mem()
    db.sync(conn, [_p("indeed_1", source="indeed")], lambda p: "intern", set(), set())
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="seconds")
    conn.execute("UPDATE jobs SET last_seen = ?", (old,))
    db.sync(conn, [], lambda p: "intern", set(), set(), aggregator_ttl_days=21)
    assert conn.execute("SELECT status FROM jobs").fetchone()[0] == "removed"


def test_migration_namespaces_workday_ids(tmp_path):
    path = tmp_path / "v1.db"
    raw = sqlite3.connect(path)
    raw.executescript(db._SCHEMA.split("CREATE TABLE IF NOT EXISTS meta")[0])
    raw.execute(
        "INSERT INTO jobs (job_id, company, title, apply_url, location, role_type, "
        "posted_on, source, first_seen, last_seen, status) VALUES "
        "('JR1', 'NVIDIA', 'SWE Intern', "
        "'https://nvidia.wd5.myworkdayjobs.com/Site/job/X/SWE-Intern_JR1', 'Santa Clara, CA', "
        "'intern', 'Posted 3 Days Ago', 'workday', '2026-09-20T00:00:00+00:00', "
        "'2026-09-20T00:00:00+00:00', 'active')")
    raw.commit()
    raw.close()
    conn = db.connect(str(path))
    row = conn.execute("SELECT job_id, posted_on, job_key, category FROM jobs").fetchone()
    assert tuple(row) == ("wd_nvidia_JR1", "2026-09-17", "nvidia|intern swe|santa clara",
                          "software")
    conn.close()
    db.connect(str(path)).close()   # idempotent


def _new(job_id, source="greenhouse", key="acme|intern software|austin", loc="Austin, TX"):
    return {"job_id": job_id, "source": source, "job_key": key, "location": loc}


def test_triage():
    old = "2026-01-01T00:00:00+00:00"
    recent = datetime.now(timezone.utc).isoformat(timespec="seconds")
    history = {
        "acme|intern software": db.RoleHistory(False, "gh_1", old, "greenhouse"),   # closed ATS
        "live|intern": db.RoleHistory(True, "gh_2", recent, "greenhouse"),
        "agg|intern": db.RoleHistory(False, "indeed_1", recent, "indeed"),
    }
    jobs = [
        _new("gh_10"),                                               # re-opened → relisted
        _new("li_1", "linkedin", "acme|intern software|denver", "Denver, CO"),  # twin
        _new("gh_11", key="live|intern|austin"),                     # already live
        _new("sim_1", "simplify", key="agg|intern|austin"),          # seen on Indeed lately
        _new("gh_12", key="brand|new|austin"),
        _new("x_1", "brandnewsource", key="z|z|"),                   # bootstrap
    ]
    new_boards = {("brandnewsource", "")}
    board_of = lambda j: (j["source"], j.get("company", ""))
    cands, relisted, skipped = repost.triage(jobs, history, new_boards, board_of,
                                             db.AGGREGATOR_SOURCES)
    assert [j["job_id"] for j in cands] == ["gh_10", "gh_12"]
    assert cands[0]["location"] == "Austin, TX; Denver, CO"
    assert relisted == {"gh_10": ("gh_1", old)}
    assert skipped == {"same_run_twin": 1, "already_tracked": 2, "bootstrap": 1}


def test_unwrap_reshare():
    assert dedupe.unwrap_reshare(
        "Berkeley IEOR", "GE Vernova Software Engineering - Co-op (Open) at GE Vernova"
    ) == ("GE Vernova", "GE Vernova Software Engineering - Co-op")
    assert dedupe.unwrap_reshare("Google", "Intern at Google") == ("Google", "Intern at Google")


def test_posting_normalizes_whitespace():
    p = _p("gh_1", title="Software Engineer Intern\nTorrance, California")
    assert p.title == "Software Engineer Intern Torrance, California"


def test_register_boards_reports_only_first_scrape():
    conn = _mem()
    assert db.register_boards(conn, {("workday", "NVIDIA"), ("simplify", "*")}) ==         {("workday", "NVIDIA"), ("simplify", "*")}
    assert db.register_boards(conn, {("workday", "NVIDIA"), ("lever", "Acme")}) ==         {("lever", "Acme")}
    assert db.board_of({"source": "linkedin", "company": "X"}) == ("linkedin", "*")
    assert db.board_of({"source": "lever", "company": "Acme"}) == ("lever", "Acme")


def test_long_tail_rotation_covers_every_board_once_per_cycle():
    from src import main
    boards = [{"tenant": f"co{i}"} for i in range(200)]
    hot = {"co7"}
    seen = []
    for slot in range(6):
        due = [b["tenant"] for b in main._due_this_run(boards, hot, 6, slot)]
        assert "co7" in due
        seen += [t for t in due if t != "co7"]
    assert sorted(seen) == sorted(f"co{i}" for i in range(200) if i != 7)


def test_sync_stores_only_configured_roles_and_prune_drops_the_rest():
    conn = _mem()
    db.sync(conn, [_p("gh_old", title="Senior Engineer")], lambda p: "senior", set(), set())
    roles = {"gh_1": "intern", "gh_2": "senior"}
    res = db.sync(conn, [_p("gh_1"), _p("gh_2", title="Staff Engineer"), _p("gh_old")],
                  lambda p: roles[p.job_id], set(), set(),
                  store_roles=frozenset({"intern", "new_grad"}))
    assert [j["job_id"] for j in res.new_jobs] == ["gh_1"]
    db.prune(conn, store_roles=frozenset({"intern", "new_grad"}))
    assert [r[0] for r in conn.execute("SELECT job_id FROM jobs")] == ["gh_1"]


def test_is_bump():
    today = date.today()
    d = lambda n: (today - timedelta(days=n)).isoformat()
    assert repost.is_bump(d(20), d(2))              # re-dated forward 18 days
    assert not repost.is_bump(d(20), d(19))         # jitter
    assert not repost.is_bump(d(40), d(30))         # Workday "30+ days" drift
    assert not repost.is_bump("", d(2)) and not repost.is_bump(d(2), "")


def test_sync_detects_same_id_redating_once_and_keeps_original_date():
    conn = _mem()
    today = date.today()
    first = (today - timedelta(days=20)).isoformat()
    again = (today - timedelta(days=1)).isoformat()
    li = lambda d: JobPosting("li_1", "Acme", "SWE Intern", "https://www.linkedin.com/jobs/view/1",
                              "Austin, TX", d, "linkedin")
    db.sync(conn, [li(first)], lambda p: "intern", set(), set())
    res = db.sync(conn, [li(again)], lambda p: "intern", set(), set())
    assert [b["job_id"] for b in res.bumped] == ["li_1"]
    assert db.sync(conn, [li(again)], lambda p: "intern", set(), set()).bumped == []
    row = conn.execute("SELECT posted_on, relisted_on, bump_count, repost FROM jobs").fetchone()
    assert tuple(row) == (first, again, 1, "bumped")


def test_apply_renames_moves_rows_and_board():
    conn = _mem()
    db.sync(conn, [_p("wd_amat_R1", company="amat", source="workday")],
            lambda p: "intern", set(), set())
    db.register_boards(conn, {("workday", "amat")})
    assert db.apply_renames(conn, {("workday", "amat"): "Applied Materials"}) == 1
    assert conn.execute("SELECT company, job_key FROM jobs").fetchone()[0] == "Applied Materials"
    assert db.register_boards(conn, {("workday", "Applied Materials")}) == set()
    assert db.apply_renames(conn, {("workday", "amat"): "Applied Materials"}) == 0


def test_name_helpers():
    from src import names
    assert names.is_slug_name("workday", {"tenant": "amat", "name": "amat"})
    assert not names.is_slug_name("workday", {"tenant": "amat", "name": "Applied Materials"})
    assert names._pretty_slug("magnet-forensics") == "Magnet Forensics"
    assert names._pretty_slug("abcsupply") is None


def test_purge_old_by_post_date_with_tombstone():
    conn = _mem()
    today = date.today()
    ago = lambda n: (today - timedelta(days=n)).isoformat()
    mk = lambda jid, posted: JobPosting(jid, "Acme", f"Intern {jid}", f"https://x/{jid}",
                                        "Austin, TX", posted, "greenhouse")
    db.sync(conn, [mk("old", ago(90)), mk("fresh", ago(5)), mk("undated", ""),
                   mk("reposted", ago(90))], lambda p: "intern", set(), set())
    conn.execute("UPDATE jobs SET relisted_on = ? WHERE job_id = 'reposted'", (ago(3),))
    old_seen = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat(timespec="seconds")
    conn.execute("UPDATE jobs SET first_seen = ? WHERE job_id = 'undated'", (old_seen,))

    assert db.purge_old(conn, 60) == 2               # 'old' by post date, 'undated' by first_seen
    left = {r[0] for r in conn.execute("SELECT job_id FROM jobs")}
    assert left == {"fresh", "reposted"}

    # The purged, still-open listing shows up again: not re-inserted or re-announced.
    res = db.sync(conn, [mk("old", ago(90)), mk("fresh", ago(5))], lambda p: "intern",
                  set(), set(), max_age_days=60)
    assert res.new_jobs == []
    assert "old" in db.existing_ids(conn)


def test_sync_skips_new_postings_older_than_max_age():
    conn = _mem()
    old = (date.today() - timedelta(days=75)).isoformat()
    res = db.sync(conn, [JobPosting("sim_1", "Acme", "Intern", "https://x", "", old, "simplify")],
                  lambda p: "intern", set(), set(), max_age_days=60)
    assert res.new_jobs == [] and conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_triage_silences_reclassified_backlog_but_not_fresh_posts():
    fresh = date.today().isoformat()
    jobs = [{"job_id": "gh_1", "source": "greenhouse", "job_key": "a|x|", "location": "",
             "posted_on": "2026-01-01"},
            {"job_id": "gh_2", "source": "greenhouse", "job_key": "b|y|", "location": "",
             "posted_on": fresh}]
    cands, _, skipped = repost.triage(jobs, {}, set(), lambda j: ("greenhouse", "A"),
                                      db.AGGREGATOR_SOURCES, reclassified=True)
    assert [j["job_id"] for j in cands] == ["gh_2"]
    assert skipped == {"reclassified_backlog": 1}


def test_one_broken_board_does_not_abort_the_source():
    from src import main

    class Flaky:
        @staticmethod
        def fetch_company_jobs(company, settings):
            if company["token"] == "bad":
                raise AttributeError("'NoneType' object has no attribute 'strip'")
            return [_p(f"gh_{company['token']}", company=company["token"])], True

    boards = [{"token": "a"}, {"token": "bad"}, {"token": "b"}]
    posts, scopes, failed = main._scrape_source("greenhouse", Flaky, boards, {})
    assert sorted(p.job_id for p in posts) == ["gh_a", "gh_b"]
    assert scopes == {("greenhouse", "a"), ("greenhouse", "b")} and failed == 1


def test_greenhouse_null_location_name(monkeypatch):
    from src import greenhouse_scraper

    class Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"jobs": [{"id": 1, "title": "SWE Intern", "location": {"name": None},
                              "absolute_url": "https://x", "first_published": None}]}

    monkeypatch.setattr(greenhouse_scraper._SESSION, "get", lambda *a, **k: Resp())
    posts, ok = greenhouse_scraper.fetch_company_jobs({"token": "t"}, {"delay_between_requests": 0})
    assert ok and posts[0].location == "" and posts[0].posted_on == ""


def test_rotation_hot_boards_every_other_run():
    from src import main
    boards = [{"tenant": f"co{i}"} for i in range(60)]
    hot = {f"co{i}" for i in range(20)}
    seen = collections.Counter()
    for slot in range(6):
        for b in main._due_this_run(boards, hot, 6, slot, hot_every=2):
            seen[b["tenant"]] += 1
    assert all(seen[f"co{i}"] == 3 for i in range(20))        # hot: every 2nd run
    assert all(seen[f"co{i}"] == 1 for i in range(20, 60))    # tail: every 6th run
