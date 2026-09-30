from datetime import date, timedelta

import pytest

from src import db, phd, sheets
from src.posting import JobPosting


@pytest.mark.parametrize("title,desc,track", [
    ("Research Scientist Intern (PhD)", "", "phd"),
    ("Machine Learning Intern", "Candidates must be currently pursuing a PhD in CS.", "phd"),
    ("Machine Learning Intern", "Open to MS or PhD students in ML.", "research_ms"),
    ("Research Intern - Vision", "", "research_ms"),
    ("Applied Scientist Intern", "", "research_ms"),
    ("Software Engineering Intern", "Pursuing a BS in CS.", ""),
    ("Doctoral Intern - Materials", "", "phd"),
])
def test_track(title, desc, track):
    assert phd.track(title, desc) == track


def test_org_tier_matches_word_prefix_only():
    orgs = {"google": 1, "meta": 1, "intel": 2}
    assert phd.org_tier("Google LLC", orgs) == 1
    assert phd.org_tier("Google DeepMind", orgs) == 1
    assert phd.org_tier("Metamaterial Inc", orgs) is None
    assert phd.org_tier("Intel Corporation", orgs) == 2


def test_score_orders_research_strength_and_freshness():
    orgs = {"google": 1}
    today = date.today().isoformat()
    old = (date.today() - timedelta(days=40)).isoformat()
    top, why = phd.score({"company": "Google", "title": "Research Scientist Intern",
                          "research_track": "phd", "posted_on": today,
                          "applicants": "Be among the first 25 applicants"}, orgs)
    low, _ = phd.score({"company": "Unknown Co", "title": "ML Intern",
                        "research_track": "research_ms", "posted_on": old,
                        "applicants": "300 applicants", "repost": "stale"}, orgs)
    assert top > 80 > 20 > low
    assert "tier-1 research org" in why and "<25 applicants" in why


def test_phd_tab_ranks_us_research_internships_only():
    conn = db.connect(":memory:")
    today = date.today().isoformat()
    mk = lambda jid, co, title, loc: JobPosting(jid, co, title, f"https://x/{jid}", loc, today,
                                                "greenhouse")
    posts = [mk("gh_1", "Google", "Research Scientist Intern, PhD", "Mountain View, CA"),
             mk("gh_2", "Acme", "Research Intern", "Austin, TX"),
             mk("gh_3", "Acme", "Software Engineering Intern", "Austin, TX"),   # no track
             mk("gh_4", "Google", "Research Intern, PhD", "London, UK")]         # non-US
    db.sync(conn, posts, lambda p: "intern", set(), set())
    rows = sheets.build_phd_tab(conn, {"announce_categories": ["software", "data_ml"]})
    assert [r[-1] for r in rows] == ["gh_1", "gh_2"]
    assert rows[0][2] == "PhD" and rows[1][2] == "Research (MS/PhD)"
    assert len(rows[0]) == len(sheets.PHD_COLUMNS)
