from src import dedupe, main
from src.posting import JobPosting


def _p(jid, source, title="SWE Intern"):
    return JobPosting(jid, "Acme", title, f"https://x/{jid}", "Austin, TX", "2026-09-30", source)


def test_shards_cover_every_board_once_and_keep_tenants_together():
    boards = [{"tenant": f"t{i % 40}", "site": f"s{i}"} for i in range(200)]
    shards = [[b for b in boards if main._shard_of(b, 2) == k] for k in (0, 1)]
    assert sum(map(len, shards)) == 200 and all(shards)
    for tenant in {b["tenant"] for b in boards}:
        assert len({main._shard_of(b, 2) for b in boards if b["tenant"] == tenant}) == 1


def test_parts_round_trip_and_merge_in_precedence_order(tmp_path):
    rest = main.Collected([_p("li_1", "linkedin"), _p("sim_1", "simplify"), _p("gh_1", "greenhouse")],
                          {("greenhouse", "Acme")}, {"simplify"}, {"greenhouse": (5, 1)})
    wd0 = main.Collected([_p("wd_a_1", "workday")], {("workday", "A")}, set(), {"workday": (10, 2)})
    wd1 = main.Collected([_p("wd_b_1", "workday", title="New Grad SWE")], {("workday", "B")}, set(),
                         {"workday": (12, 0)})
    for name, part in (("p0", wd0), ("p1", wd1), ("p2", rest)):
        main.save_part(str(tmp_path / f"{name}.json.gz"), part)
    merged = main.load_parts([str(f) for f in tmp_path.glob("*.json.gz")])
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
