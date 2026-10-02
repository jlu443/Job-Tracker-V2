import pytest

from src import enrich, h1b, sheets


@pytest.mark.parametrize("text,flag", [
    ("CPT/OPT candidates are welcome to apply.", "yes"),
    ("F-1 students on OPT are eligible.", "yes"),
    ("Candidates on STEM OPT will be considered.", "yes"),
    ("We are unable to support OPT, CPT or visa sponsorship.", "no"),
    ("This role is not eligible for CPT.", "no"),
    ("Must be a U.S. citizen.", "no"),
    ("You can opt in to our newsletter.", ""),
])
def test_opt_cpt_flag(text, flag):
    assert enrich.parse_flags(text)["opt_cpt"] == flag


def test_normalize_employer_names():
    assert h1b.normalize("JPMORGAN CHASE & CO") == "jpmorgan chase"
    assert h1b.normalize("Susquehanna International Group (SIG)") == "susquehanna international"
    assert h1b.normalize("Amazon.com Services LLC") == "amazon com services"


def test_h1b_lookup_from_committed_data():
    assert h1b.approvals("Google") > 10_000
    assert h1b.approvals("AMD") > 500                    # alias -> Advanced Micro Devices
    assert h1b.approvals("JP Morgan Chase") > 1_000      # spacing differs from USCIS
    assert h1b.approvals("Zzqx Nonexistent Labs") is None


def _c(roles=1, offers=0, rules_out=0, citizens=0):
    return {"roles": roles, "offers": offers, "rules_out": rules_out, "citizens": citizens}


@pytest.mark.parametrize("counts,n,verdict", [
    (_c(citizens=1), 5000, "🇺🇸 US citizens / clearance only"),
    (_c(rules_out=1), 5000, "❌ Postings rule out sponsorship"),
    (_c(roles=2, rules_out=1), 5000, "🟢 Likely: sponsors H-1Bs regularly"),
    (_c(offers=1), None, "✅ Postings offer sponsorship / OPT"),
    (_c(), 7, "🟡 Some H-1B history"),
    (_c(), None, "⚪ Unknown"),
])
def test_company_outlook(counts, n, verdict):
    assert sheets.company_outlook(counts, n) == verdict


def test_phd_tab_and_sponsor_tab():
    from datetime import date
    from src import db
    from src.posting import JobPosting
    conn = db.connect(":memory:")
    today = date.today().isoformat()
    mk = lambda jid, co, title, **kw: JobPosting(jid, co, title, f"https://x/{jid}",
                                                 "Mountain View, CA", today, "greenhouse", **kw)
    db.sync(conn, [mk("gh_1", "Google", "Research Scientist Intern, PhD"),
                   mk("gh_2", "Google", "Research Intern", opt_cpt="yes", checked=True),
                   mk("gh_3", "Zzqx Labs", "Research Intern")],
            lambda p: "intern", set(), set())
    settings = {"announce_categories": ["software", "data_ml"]}
    phd_rows = sheets.build_phd_tab(conn, settings)
    assert "Days ago" not in sheets.PHD_COLUMNS
    assert all(len(r) == len(sheets.PHD_COLUMNS) for r in phd_rows)
    rows = sheets.build_sponsor_tab(conn, settings)
    assert [r[0] for r in rows] == ["Google", "Zzqx Labs"]      # known H-1B record first
    assert rows[0][1] == "✅ Postings offer sponsorship / OPT" and rows[0][3] == 2
    assert rows[1][2] == "" and len(rows[0]) == len(sheets.sponsor_columns())
