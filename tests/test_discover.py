"""Discovery: candidates rejected recently aren't re-validated; our own jobs'
apply links are a harvest source."""
import sqlite3
from datetime import date, timedelta

from src import ats_specs, discover


def test_rejected_candidates_skipped_until_they_age_out(tmp_path, monkeypatch):
    monkeypatch.setattr(discover, "_REJECTED", str(tmp_path / "rejected.json"))
    spec = ats_specs.ATSSpec("jobvite", "jobvite.yaml", ats_specs._extract_jobvite,
                             lambda c: c["slug"] == "good", lambda c: c["slug"], ())
    monkeypatch.setattr(spec.__class__, "config_path",
                        property(lambda self: str(tmp_path / "jobvite.yaml")))
    checked = []
    orig = spec.validate
    object.__setattr__(spec, "validate", lambda c: checked.append(c["slug"]) or orig(c))
    cands = [{"slug": "good", "name": "good"}, {"slug": "dead", "name": "dead"}]
    assert discover.validate_and_merge(spec, cands) == 1
    assert sorted(checked) == ["dead", "good"]
    checked.clear()
    assert discover.validate_and_merge(spec, cands) == 0      # good configured, dead rejected
    assert checked == []
    old = (date.today() - timedelta(days=discover._REJECT_DAYS + 1)).isoformat()
    discover._save_rejected({"jobvite": {"dead": old}})
    discover.validate_and_merge(spec, cands)
    assert checked == ["dead"]                                # re-checked once it aged out


def test_harvest_from_db_reads_apply_links(tmp_path):
    db = tmp_path / "jobs.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE jobs (source TEXT, apply_url TEXT, direct_url TEXT)")
    conn.executemany("INSERT INTO jobs VALUES (?, ?, ?)", [
        ("indeed", "https://www.indeed.com/viewjob?jk=1",
         "https://acme.wd5.myworkdayjobs.com/External/job/Austin/Intern_R123"),
        ("simplify", "https://jobs.jobvite.com/tylertech/job/o5APAfwO", ""),
        ("greenhouse", "https://boards.greenhouse.io/known/jobs/1", ""),  # first-party: ours
    ])
    conn.commit()
    conn.close()
    found = discover.harvest_from_db(str(db))
    assert [c["tenant"] for c in found["workday"]] == ["acme"]
    assert [c["slug"] for c in found["jobvite"]] == ["tylertech"]
    assert not found.get("greenhouse")
