import time

from src import accuracy, db
from src.posting import JobPosting


def test_measure_buckets_and_role_confusion(monkeypatch):
    conn = db.connect(":memory:")
    db.sync(conn, [JobPosting("gh_1", "Acme", "Software Engineer Intern",
                              "https://boards.greenhouse.io/acme/jobs/1", "Austin, TX",
                              "2026-09-20", "greenhouse")], lambda p: "intern", set(), set())
    db.register_boards(conn, {("greenhouse", "Acme")})
    monkeypatch.setattr(accuracy, "_configured_boards", lambda: {"greenhouse": {"acme": "Acme"}})
    now = time.time()
    truth = [
        {"url": "https://boards.greenhouse.io/acme/jobs/1", "title": "Software Engineer Intern",
         "role": "intern", "category": "Software", "date_posted": now},        # found
        {"url": "https://boards.greenhouse.io/acme/jobs/2", "title": "Software Engineer",
         "role": "new_grad", "category": "Software", "date_posted": now},      # title rules drop it
        {"url": "https://boards.greenhouse.io/acme/jobs/3", "title": "Data Intern",
         "role": "intern", "category": "AI/ML/Data", "date_posted": now},      # scraper missed
        {"url": "https://boards.greenhouse.io/other/jobs/4", "title": "Intern",
         "role": "intern", "category": "Software", "date_posted": now},        # board not configured
        {"url": "https://boards.greenhouse.io/acme/jobs/5", "title": "Intern",
         "role": "intern", "category": "Software", "date_posted": now - 90 * 86400},  # too old
        {"url": "https://careers.example.com/jobs/6", "title": "Intern", "role": "intern"},
    ]
    r = accuracy.measure(truth, conn, max_age=60)
    gh = r["coverage"]["greenhouse"]
    assert gh == {"listings": 5, "found": 1, "dropped_by_title_rules": 1, "scraper_missed": 1,
                  "board_not_configured": 1, "older_than_retention": 1}
    assert r["coverage"]["unsupported"]["listings"] == 1
    assert r["in_scope_recall"]["greenhouse"] == round(1 / 3, 3)
    assert r["role"]["confusion"]["new_grad->none"] == 1
