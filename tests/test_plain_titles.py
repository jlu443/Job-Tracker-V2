"""Plain-titled new-grad jobs, found by reading their descriptions."""
from src import db, enrich, plain_titles
from src.posting import JobPosting

NG = "Join us as a new graduate software engineer. 0-2 years of experience."
SENIOR = "Requires 5+ years of professional experience building services."


def _p(job_id, title="Software Engineer", source="workday", location="Austin, TX",
       posted="2026-10-10", **kw):
    return JobPosting(job_id=job_id, company="Acme", title=title,
                      apply_url=f"https://acme.wd1.myworkdayjobs.com/x/job/y/{job_id}",
                      location=location, posted_on=posted, source=source, **kw)


def test_candidates_are_new_us_tech_plain_titles_on_listing_only_sources():
    ps = [_p("wd_a_1"),                                        # yes
          _p("wd_a_2", title="Software Engineer Intern"),      # title decides
          _p("wd_a_3", title="Senior Software Engineer"),      # title decides
          _p("wd_a_4", title="Payroll Specialist"),            # not tech
          _p("wd_a_5", location="London, UK"),                 # abroad
          _p("gh_6", source="greenhouse"),                     # description in listing
          _p("wd_a_7"),                                        # already stored
          _p("wd_a_8"),                                        # already read
          _p("wd_a_9", posted="2026-10-11"),                   # yes, newer
          _p("wd_a_10", role_hint="new_grad")]                 # source already says
    got = plain_titles.candidates(ps, known={"wd_a_7"}, read={"wd_a_8"})
    assert [p.job_id for p in got] == ["wd_a_9", "wd_a_1"]


def test_find_new_grads_hints_reads_once_and_caps(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "p.db"))
    texts = {"wd_a_1": NG, "wd_a_2": SENIOR, "wd_a_3": "", "wd_a_4": NG}
    monkeypatch.setattr(enrich, "_description_for", lambda job: texts[job["job_id"]])
    monkeypatch.setattr(enrich, "_DELAY", 0)
    ps = [_p("wd_a_1", posted="2026-10-10"), _p("wd_a_2", posted="2026-10-09"),
          _p("wd_a_3", posted="2026-10-08"), _p("wd_a_4", posted="2026-10-01")]
    out, hinted, capped = plain_titles.find_new_grads(conn, ps, set(), limit=3)
    assert hinted == {"wd_a_1"} and capped
    assert [p.role_hint for p in out] == ["new_grad", "", "", ""]
    assert out[0].description == NG
    # Read ones aren't read again; the failed read (empty text) is retried.
    _, hinted, capped = plain_titles.find_new_grads(conn, ps, set(), limit=3)
    assert hinted == {"wd_a_4"} and not capped
    assert {r[0] for r in conn.execute("SELECT job_id FROM described")} == \
        {"wd_a_1", "wd_a_2", "wd_a_4"}


def test_hinted_posting_is_stored_as_new_grad(tmp_path, monkeypatch):
    from src import classify
    conn = db.connect(str(tmp_path / "s.db"))
    monkeypatch.setattr(enrich, "_description_for", lambda job: NG)
    monkeypatch.setattr(enrich, "_DELAY", 0)
    out, _, _ = plain_titles.find_new_grads(conn, [_p("wd_a_1")], set())
    res = db.sync(conn, out, lambda p: classify.role_with_hint(p.title, p.role_hint),
                  set(), set(), store_roles=frozenset({"intern", "new_grad"}))
    assert [(j["job_id"], j["role_type"]) for j in res.new_jobs] == [("wd_a_1", "new_grad")]


def test_fresh_enough():
    from src import dates
    today = dates.today().isoformat()
    assert plain_titles.fresh_enough({"posted_on": today}, capped=True)
    assert not plain_titles.fresh_enough({"posted_on": "2026-01-01"}, capped=False)
    assert plain_titles.fresh_enough({"posted_on": ""}, capped=False)
    assert not plain_titles.fresh_enough({"posted_on": ""}, capped=True)


def test_page_country_abroad_is_not_hinted(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "c.db"))

    def read(job):
        job["country"] = "CN"
        return NG
    monkeypatch.setattr(enrich, "_description_for", read)
    monkeypatch.setattr(enrich, "_DELAY", 0)
    _, hinted, _ = plain_titles.find_new_grads(conn, [_p("wd_a_1", location="Main Campus")],
                                               set())
    assert hinted == set()
