from src import collect, dedupe
from src.posting import JobPosting


def _p(jid, source, title="SWE Intern"):
    return JobPosting(jid, "Acme", title, f"https://x/{jid}", "Austin, TX", "2026-09-30", source)


def test_shards_cover_every_board_once_and_keep_tenants_together():
    boards = [{"tenant": f"t{i % 40}", "site": f"s{i}"} for i in range(200)]
    shards = [[b for b in boards if collect.shard_of(b, 2) == k] for k in (0, 1)]
    assert sum(map(len, shards)) == 200 and all(shards)
    for tenant in {b["tenant"] for b in boards}:
        assert len({collect.shard_of(b, 2) for b in boards if b["tenant"] == tenant}) == 1


def test_parts_round_trip_and_merge_in_precedence_order(tmp_path):
    rest = collect.Collected([_p("li_1", "linkedin"), _p("sim_1", "simplify"), _p("gh_1", "greenhouse")],
                          {("greenhouse", "Acme")}, {"simplify"}, {"greenhouse": (5, 1)})
    wd0 = collect.Collected([_p("wd_a_1", "workday")], {("workday", "A")}, set(), {"workday": (10, 2)})
    wd1 = collect.Collected([_p("wd_b_1", "workday", title="New Grad SWE")], {("workday", "B")}, set(),
                         {"workday": (12, 0)})
    for name, part in (("p0", wd0), ("p1", wd1), ("p2", rest)):
        collect.save_part(str(tmp_path / f"{name}.json.gz"), part)
    merged = collect.load_parts([str(f) for f in tmp_path.glob("*.json.gz")])
    assert [p.source for p in merged.postings] == ["workday", "workday", "greenhouse",
                                                   "simplify", "linkedin"]
    assert merged.board_stats == {"workday": (22, 2), "greenhouse": (5, 1)}
    assert merged.complete_scopes == {("greenhouse", "Acme"), ("workday", "A"), ("workday", "B")}
    assert merged.complete_sources == {"simplify"}
    assert merged.postings[1].title == "New Grad SWE"


def test_collapse_roles_merges_locations_with_cap():
    jobs = [{"job_id": f"j{i}", "job_key": f"acme|intern swe|city{i}", "location": f"City{i}, TX"}
            for i in range(7)]
    jobs.append({"job_id": "other", "job_key": "acme|data intern|austin", "location": "Austin, TX"})
    out = dedupe.collapse_roles(jobs)
    assert [j["job_id"] for j in out] == ["j0", "other"]
    assert out[0]["location"] == "City0, TX; City1, TX; City2, TX; City3, TX +3 more"


def test_trim_for_handoff_keeps_known_and_storable_and_counts_first(tmp_path):
    from src import db
    conn = db.connect(":memory:")
    db.sync(conn, [_p("gh_old", "greenhouse", title="Staff Engineer")], lambda p: "intern",
            set(), set())                                   # already stored (as intern)
    c = collect.Collected([_p("gh_old", "greenhouse", title="Staff Engineer"),
                        _p("gh_new", "greenhouse", title="Senior Engineer"),
                        _p("gh_int", "greenhouse", title="SWE Intern"),
                        _p("sim_1", "simplify", title="Software Engineer")],
                       set(), set(), {}, {"greenhouse": 3, "simplify": 1})
    object.__setattr__(c.postings[3], "role_hint", "new_grad")
    dropped = collect.trim_for_handoff(c, conn, {"store_roles": ["intern", "new_grad"]})
    assert dropped == 1 and [p.job_id for p in c.postings] == ["gh_old", "gh_int", "sim_1"]
    collect.save_part(str(tmp_path / "p.json.gz"), c)
    merged = collect.load_parts([str(tmp_path / "p.json.gz")])
    assert merged.source_counts == {"greenhouse": 3, "simplify": 1}
    assert collect.trim_for_handoff(c, conn, {"store_roles": ["intern"], "use_llm_fallback": True}) == 0
