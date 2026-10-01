from src import scraper

SETTINGS = {"page_limit": 2, "request_timeout": 5, "delay_between_requests": 0,
            "max_pages_per_term": 5, "search_terms": ["intern"],
            "recency_check": {"workday": {"window_days": 2, "max_pages": 8}}}
BOARD = {"tenant": "acme", "wd": "wd1", "site": "ext", "name": "Acme"}


def _p(n, posted, title="Software Engineer 1"):
    return {"externalPath": f"/job/x/Role_R{n}", "title": title, "postedOn": posted}


def _fake_pages(pages_by_term):
    def page(endpoint, term, offset, settings, name):
        pages = pages_by_term.get(term, [])
        i = offset // settings["page_limit"]
        return {"total": 99, "jobPostings": pages[i] if i < len(pages) else []}
    return page


def test_recent_mode_pages_until_nothing_recent_and_is_partial(monkeypatch):
    pages = {"": [[_p(1, "Posted 30+ Days Ago"), _p(2, "Posted Today")],   # pinned old job on top
                  [_p(3, "Posted Yesterday"), _p(4, "Posted 5 Days Ago")],
                  [_p(5, "Posted 9 Days Ago"), _p(6, "Posted 12 Days Ago")],
                  [_p(7, "Posted Today")]]}                             # never reached
    calls = []
    fake = _fake_pages(pages)
    monkeypatch.setattr(scraper, "_page", lambda *a: calls.append(a[1:3]) or fake(*a))
    posts, complete = scraper.fetch_company_jobs({**BOARD, "_mode": "recent"}, SETTINGS)
    assert sorted(p.job_id for p in posts) == [f"wd_acme_R{i}" for i in range(1, 7)]
    assert complete is None                         # recent-only: never marks jobs removed
    assert calls == [("", 0), ("", 2), ("", 4)]      # stopped at the page with nothing recent


def test_undated_board_falls_back_to_sweep(monkeypatch):
    pages = {"": [[_p(1, ""), _p(2, "")]],
             "intern": [[_p(3, "", "Intern"), _p(4, "", "Intern")]]}
    monkeypatch.setattr(scraper, "_page", _fake_pages(pages))
    posts, complete = scraper.fetch_company_jobs({**BOARD, "_mode": "recent"}, SETTINGS)
    assert {"wd_acme_R3", "wd_acme_R4"} <= {p.job_id for p in posts}
    assert complete is True


def test_recent_plus_sweep_is_complete_and_unions(monkeypatch):
    pages = {"": [[_p(1, "Posted Today"), _p(2, "Posted 10 Days Ago")]],
             "intern": [[_p(9, "Posted 40 Days Ago", "Intern")]]}
    monkeypatch.setattr(scraper, "_page", _fake_pages(pages))
    posts, complete = scraper.fetch_company_jobs({**BOARD, "_mode": "recent+sweep"}, SETTINGS)
    assert {"wd_acme_R1", "wd_acme_R9"} <= {p.job_id for p in posts} and complete is True


def test_failed_request_is_incomplete(monkeypatch):
    monkeypatch.setattr(scraper, "_page", lambda *a: None)
    assert scraper.fetch_company_jobs({**BOARD, "_mode": "recent"}, SETTINGS) == ([], False)
