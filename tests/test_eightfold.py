from src import dedupe, eightfold_scraper


def test_canonical_ids():
    assert dedupe.canonical_job_id(
        "https://apply.careers.microsoft.com/careers/job/1970393556953113") == \
        "ef_microsoft_1970393556953113"
    assert dedupe.canonical_job_id(
        "https://qualcomm.eightfold.ai/careers/job/446716226621?domain=qualcomm.com") == \
        "ef_qualcomm_446716226621"


def test_pages_until_results_stop_being_entry_level(monkeypatch):
    pages = {0: [("Software Engineering Intern", 1), ("Data Science Intern", 2)],
             10: [("Senior Engineer", 3), ("Principal Engineer", 4)],
             20: [("Hardware Intern", 5)]}
    calls = []

    class Resp:
        status_code = 200

        def __init__(self, start):
            self.start = start

        def raise_for_status(self):
            pass

        def json(self):
            return {"data": {"count": 25, "positions": [
                {"id": pid, "name": name, "standardizedLocations": ["Redmond, WA, US"],
                 "postedTs": 1790712178, "positionUrl": f"/careers/job/{pid}"}
                for name, pid in pages[self.start]]}}

    def get(url, timeout, params):
        calls.append(params["start"])
        return Resp(params["start"])

    monkeypatch.setattr(eightfold_scraper._SESSION, "get", get)
    monkeypatch.setattr(eightfold_scraper.time, "sleep", lambda s: None)
    jobs, complete = eightfold_scraper.fetch_company_jobs(
        {"tenant": "microsoft", "host": "apply.careers.microsoft.com", "domain": "microsoft.com",
         "name": "Microsoft", "search_terms": ["intern"]}, {})
    assert complete and calls == [0, 10]          # page 2 had no entry-level titles
    assert [j.job_id for j in jobs] == ["ef_microsoft_1", "ef_microsoft_2",
                                        "ef_microsoft_3", "ef_microsoft_4"]
    assert jobs[0].apply_url == "https://apply.careers.microsoft.com/careers/job/1"
    assert jobs[0].posted_on == "2026-09-29" and jobs[0].location == "Redmond, WA, US"
