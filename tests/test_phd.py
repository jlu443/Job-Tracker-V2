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


def test_posted_sort_key_newest_first_with_first_seen_fallback():
    jobs = [{"posted_on": "2026-09-20", "first_seen": "2026-09-21T00:00:00+00:00"},
            {"posted_on": "", "first_seen": "2026-09-28T00:00:00+00:00"},
            {"posted_on": "2026-09-25", "first_seen": "2026-09-25T00:00:00+00:00"}]
    jobs.sort(key=phd.posted_sort_key, reverse=True)
    assert [j["posted_on"] or "seen" for j in jobs] == ["seen", "2026-09-25", "2026-09-20"]


def test_phd_tab_lists_us_research_internships_newest_first():
    conn = db.connect(":memory:")
    today = date.today().isoformat()
    older = (date.today() - timedelta(days=5)).isoformat()
    mk = lambda jid, co, title, loc, posted: JobPosting(jid, co, title, f"https://x/{jid}", loc,
                                                        posted, "greenhouse")
    posts = [mk("gh_1", "Google", "Research Scientist Intern, PhD", "Mountain View, CA", older),
             mk("gh_2", "Acme", "Research Intern", "Austin, TX", today),
             mk("gh_3", "Acme", "Software Engineering Intern", "Austin, TX", today),   # no track
             mk("gh_4", "Google", "Research Intern, PhD", "London, UK", today)]        # non-US
    db.sync(conn, posts, lambda p: "intern", set(), set())
    rows = sheets.build_phd_tab(conn, {"announce_categories": ["software", "data_ml"]})
    assert [r[-1] for r in rows] == ["gh_2", "gh_1"]            # newest posted first
    assert rows[0][0] == "Research (MS/PhD)" and rows[1][0] == "PhD"
    assert len(rows[0]) == len(sheets.PHD_COLUMNS)
