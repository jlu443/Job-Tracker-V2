"""Countries from Workday job pages decide the US filter for unplaceable
locations ("Crawley", "2 Locations")."""
from src import db, enrich, geo, maintenance, notify
from src.posting import JobPosting


def _wd(job_id, location, company="Thales", title="Software Engineer Intern"):
    return JobPosting(job_id=job_id, company=company, title=title,
                      apply_url=f"https://thales.wd3.myworkdayjobs.com/Careers/job/x/{job_id}",
                      location=location, posted_on="2026-10-09", source="workday")


def _info(code, descriptor="", additional=None):
    return {"jobRequisitionLocation": {"country": {"alpha2Code": code} if code else {}},
            "country": {"descriptor": descriptor}, "additionalLocations": additional}


def test_known_country_decides_is_us_job():
    assert geo.is_us_job("AI Engineer Apprentice", "Crawley") is True      # can't tell
    assert geo.is_us_job("AI Engineer Apprentice", "Crawley", "GB") is False
    assert geo.is_us_job("Software Intern", "2 Locations", "US") is True
    assert geo.is_us_job("Software Intern", "Aguadilla", "PR") is True
    # '-' = page read, no country: the location decides as before
    assert geo.is_us_job("Software Intern", "Crawley", "-") is True
    assert geo.is_us_job("Software Intern", "London, UK", "-") is False


def test_workday_country():
    assert enrich.workday_country(_info("GB")) == "GB"
    assert enrich.workday_country(_info("CA", additional=["Austin, TX"])) == "US"
    assert enrich.workday_country(_info("CA", additional=["Waterloo, Ontario"])) == "CA"
    assert enrich.workday_country(_info("", "United States of America")) == "US"
    assert enrich.workday_country(_info("", "United Kingdom")) == "XX"
    assert enrich.workday_country(_info("")) == "-"


def test_country_is_shared_by_place_and_inherited_by_new_rows(tmp_path):
    conn = db.connect(str(tmp_path / "c.db"))
    db.sync(conn, [_wd("wd_t_1", "Crawley"), _wd("wd_t_2", "Crawley"),
                   _wd("wd_t_3", "2 Locations"), _wd("wd_t_4", "2 Locations"),
                   _wd("wd_t_5", "Crawley", company="Airbus")],
            lambda p: "intern", set(), set())
    db.update_enrichment(conn, [{"job_id": "wd_t_1", "company": "Thales", "location": "Crawley",
                                 "country": "GB"},
                                {"job_id": "wd_t_3", "company": "Thales",
                                 "location": "2 Locations", "country": "CA"}])
    country = dict(conn.execute("SELECT job_id, country FROM jobs"))
    # Same company and place: shared. "2 Locations" and another company: not.
    assert country == {"wd_t_1": "GB", "wd_t_2": "GB", "wd_t_3": "CA", "wd_t_4": "",
                       "wd_t_5": ""}
    res = db.sync(conn, [_wd("wd_t_6", "Crawley")], lambda p: "intern", set(), set())
    assert res.new_jobs[0]["country"] == "GB"
    assert notify.announceable(res.new_jobs, {}) == []


def test_learn_countries_reads_one_page_per_place(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "l.db"))
    db.sync(conn, [_wd("wd_t_1", "Crawley"), _wd("wd_t_2", "Crawley"),
                   _wd("wd_t_3", "Austin, TX"), _wd("wd_t_4", "3 Locations"),
                   _wd("wd_t_5", "Main Campus")],
            lambda p: "intern", set(), set())
    asked = []

    def detail(job):
        asked.append(job["job_id"])
        if job["job_id"] == "wd_t_5":
            raise enrich.requests.ConnectionError("timeout")
        return _info({"wd_t_1": "GB", "wd_t_2": "GB", "wd_t_4": "US"}[job["job_id"]])

    monkeypatch.setattr(enrich, "_workday_detail", detail)
    assert maintenance.learn_countries(conn) == 3
    # Not Austin (placed by its location); one of the two Crawley jobs.
    assert len(asked) == 3 and {"wd_t_4", "wd_t_5"} < set(asked) and "wd_t_3" not in asked
    country = dict(conn.execute("SELECT job_id, country FROM jobs"))
    assert country == {"wd_t_1": "GB", "wd_t_2": "GB", "wd_t_3": "", "wd_t_4": "US",
                       "wd_t_5": ""}                          # failed read: retried later
