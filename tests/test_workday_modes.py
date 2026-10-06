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


def test_page_cap_with_results_remaining_is_incomplete(monkeypatch):
    pages = {"intern": [[_p(i * 2, "Posted Today", "Pharmacy Intern"),
                         _p(i * 2 + 1, "Posted Today", "Pharmacy Intern")] for i in range(50)]}
    monkeypatch.setattr(scraper, "_page", _fake_pages(pages))
    posts, complete = scraper.fetch_company_jobs({**BOARD, "_mode": "sweep"}, SETTINGS)
    assert len(posts) == 10 and complete is False        # 5 pages x 2, cap reached


def test_same_name_boards_complete_only_together():
    from src import collect

    class Mod:
        @staticmethod
        def fetch_company_jobs(c, settings):
            return [], c["site"] != "private"     # one CVS board fails

    boards = [{"tenant": "cvs", "site": "main", "name": "CVS Health"},
              {"tenant": "cvs", "site": "private", "name": "CVS Health"},
              {"tenant": "acme", "site": "x", "name": "Acme"}]
    _, scopes, failed = collect.scrape_source("workday", Mod, boards, {})
    assert scopes == {("workday", "Acme")} and failed == 1


def test_failed_boards_get_a_second_pass(monkeypatch):
    from src import collect
    calls = {}

    class Mod:
        @staticmethod
        def fetch_company_jobs(c, settings):
            calls[c["tenant"]] = calls.get(c["tenant"], 0) + 1
            if c["tenant"] == "flaky" and calls["flaky"] == 1:
                return [], False                      # rate-limited the first time
            if c["tenant"] == "down":
                return [], False                      # fails both times
            return [], True

    monkeypatch.setattr(collect.time, "sleep", lambda s: None)
    boards = [{"tenant": t, "name": t} for t in ("ok", "flaky", "down")]
    _, scopes, failed = collect.scrape_source(
        "workday", Mod, boards, {"retry_incomplete": {"workday": {"delay": 0, "workers": 2}}})
    assert scopes == {("workday", "ok"), ("workday", "flaky")} and failed == 1
    assert calls == {"ok": 1, "flaky": 2, "down": 2}


# Trimmed from real boards (Sysco, Coca-Cola, Florida Tech, Barclays), 2026-10-06.
FACETS = [
    {"facetParameter": "workerSubType", "values": [
        {"descriptor": "Regular", "id": "reg", "count": 654},
        {"descriptor": "Intern (Trainee)", "id": "int", "count": 26},
        {"descriptor": "Graduate", "id": "grad", "count": 32},
        {"descriptor": "Student Employee (Fixed Term)", "id": "campus", "count": 1},
        {"descriptor": "Intern/Student Worker (Fixed Term)", "id": "int2", "count": 4}]},
    {"facetParameter": "jobFamilyGroup", "values": [
        {"descriptor": "Interim & Interns", "id": "mixed", "count": 9},
        {"descriptor": "Internal Audit", "id": "audit", "count": 22},
        {"descriptor": "Student Finance", "id": "staff", "count": 2},
        {"descriptor": "Early Careers", "id": "ec", "count": 127}]},
    {"facetParameter": "locationMainGroup", "values": [
        {"facetParameter": "locations", "values": [
            {"descriptor": "University Park, Florida", "id": "loc", "count": 2}]}]},
]


def test_entry_facets_pick_early_career_values_only():
    assert scraper.entry_facets(FACETS) == {
        "workerSubType": [("int", "intern"), ("grad", "new_grad"), ("int2", "intern")],
        "jobFamilyGroup": [("ec", "new_grad")]}


def test_facet_listing_labels_plain_titles(monkeypatch):
    """A plain "Software Engineer" filed under Intern comes back as one."""
    def page(endpoint, term, offset, settings, name, facets=None):
        if facets == {"workerSubType": ["int"]}:
            return {"total": 1, "jobPostings": [_p(9, "Posted Today", "Software Engineer")]}
        if facets is None and term == "":
            return {"total": 0, "jobPostings": [], "facets": FACETS[:1]}
        return {"total": 0, "jobPostings": []}
    monkeypatch.setattr(scraper, "_page", page)
    posts, complete = scraper.fetch_company_jobs(BOARD, SETTINGS)
    assert complete
    assert [(p.job_id, p.role_hint) for p in posts] == [("wd_acme_R9", "intern")]
