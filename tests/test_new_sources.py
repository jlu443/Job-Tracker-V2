import pytest

from src import bigtech_scrapers, dedupe, jibe_scraper, rippling_scraper


@pytest.mark.parametrize("url,job_id", [
    ("https://lifeattiktok.com/search/7602395891774802181", "tt_7602395891774802181"),
    ("https://www.amazon.jobs/en/jobs/3066646/business-intelligence-engineer-co-op", "amzn_3066646"),
    ("https://jobs.apple.com/en-us/details/200664323", "apple_200664323"),
    ("https://careers.amd.com/jobs/91183?icims=1", "jibe_careers.amd.com_91183"),
    ("https://ats.rippling.com/omnis-corporation/jobs/e389ff2d-5be5-4571-8cc1-f361a139b753",
     "rip_e389ff2d-5be5-4571-8cc1-f361a139b753"),
    ("https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/210673021",
     "orc_jpmc_210673021"),
    ("https://careers-gdms.icims.com/jobs/71647/x/job", "icims_careers-gdms_71647"),   # unchanged
])
def test_canonical_ids(url, job_id):
    assert dedupe.canonical_job_id(url) == job_id


@pytest.mark.parametrize("title,hint", [
    ("SoC Design Verification Engineer", "new_grad"),
    ("US-Technical Expert", ""),                       # Apple Store student role
    ("Software Engineering Masters Intern", ""),       # the title already says intern
    ("Business Development Associate", ""),            # not a tech role
])
def test_apple_hint(title, hint):
    assert bigtech_scrapers._apple_hint(title) == hint


class _Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def json(self):
        return self._data

    def raise_for_status(self):
        pass


def test_jibe_parses_and_pages(monkeypatch):
    pages = {1: [{"data": {"slug": "91183", "title": "Software Intern", "full_location": "Austin, TX",
                           "posted_date": "2026-09-29T07:00:00+0000",
                           "apply_url": "https://careers-amd.icims.com/jobs/91183/login"}}]}
    monkeypatch.setattr(jibe_scraper._SESSION, "get",
                        lambda url, params, timeout: _Resp({"jobs": pages.get(params["page"], []),
                                                            "totalCount": 1}))
    posts, ok = jibe_scraper.fetch_company_jobs({"host": "careers.amd.com", "name": "AMD",
                                                 "search_terms": ["intern"]}, {})
    p = posts[0]
    assert ok and p.job_id == "jibe_careers.amd.com_91183" and p.posted_on == "2026-09-29"
    assert p.apply_url == "https://careers.amd.com/jobs/91183?icims=1"
    assert dedupe.canonical_job_id(p.apply_url) == p.job_id


def test_rippling_location_and_paging(monkeypatch):
    item = {"id": "e389ff2d-5be5-4571-8cc1-f361a139b753", "name": "Robotics Intern",
            "url": "https://ats.rippling.com/x/jobs/e389ff2d-5be5-4571-8cc1-f361a139b753",
            "locations": [{"name": "Brisbane, CA", "country": "United States"}]}
    monkeypatch.setattr(rippling_scraper._SESSION, "get",
                        lambda url, params, timeout: _Resp({"items": [item], "totalPages": 1}))
    posts, ok = rippling_scraper.fetch_company_jobs({"slug": "x", "name": "X"}, {})
    assert ok and posts[0].location == "Brisbane, CA, United States"


def test_greenhouse_company_hosted_link_finds_its_board(monkeypatch):
    from src import enrich
    calls = []

    class Resp:
        def __init__(self, url, data=None):
            self.url, self._data = url, data or {}

        def json(self):
            return self._data

    def get(url, timeout):
        calls.append(url)
        if "embed/job_app" in url:
            return Resp("https://job-boards.greenhouse.io/embed/job_app?for=waymo&token=8193295")
        return Resp(url, {"content": "&lt;p&gt;Must be a US citizen.&lt;/p&gt;"})

    monkeypatch.setattr(enrich._SESSION, "get", get)
    text = enrich._fetch_greenhouse({"job_id": "gh_8193295",
                                     "apply_url": "https://careers.withwaymo.com/jobs?gh_jid=8193295"})
    assert "US citizen" in text
    assert calls[-1] == "https://boards-api.greenhouse.io/v1/boards/waymo/jobs/8193295"


def test_workday_status_only_retires_on_a_definite_answer(monkeypatch):
    from src import enrich

    class Resp:
        def __init__(self, code, data=None):
            self.status_code, self._data = code, data

        def json(self):
            return self._data

    job = {"apply_url": "https://acme.wd1.myworkdayjobs.com/External/job/X/Intern_R1"}
    for code, data, want in [(200, {"jobPostingInfo": {"id": "1"}}, True),
                             (403, {"errorCode": "S22"}, False), (404, None, False),
                             (429, None, None), (500, None, None)]:
        monkeypatch.setattr(enrich._SESSION, "get", lambda url, timeout, c=code, d=data: Resp(c, d))
        assert enrich.workday_status(job) is want


def test_hidden_greenhouse_board_tries_newest_job_ids_first(monkeypatch):
    from src import discover
    tried = []

    def embed(job_id):
        tried.append(job_id)
        return {"token": "waymo", "name": "Waymo"} if job_id == "8193295" else None  # others closed

    monkeypatch.setattr(discover, "_embed_token", embed)
    monkeypatch.setattr(discover, "_resolve_one", lambda url, jid: None)
    links = [("https://careers.withwaymo.com/jobs?gh_jid=" + i, i)
             for i in ("5000001", "8193295", "8300000")]
    assert discover._resolve_site(links) == {"token": "waymo", "name": "Waymo"}
    assert tried == ["8300000", "8193295"]


def test_atlassian_portals_share_one_id():
    from src import dedupe
    for url in ("https://campus-americas.icims.com/jobs/26268/machine-learning-intern",
                "https://careers-americas.icims.com/jobs/26268/x",
                "https://campus-globalcareers-atlassian.icims.com/jobs/26268/x"):
        assert dedupe.canonical_job_id(url) == "icims_atlassian_26268"
    assert dedupe.canonical_job_id("https://americas-cookmedical.icims.com/jobs/19325/x") == \
        "icims_americas-cookmedical_19325"


def test_lever_eu_boards_use_eu_api():
    from src import ats_specs, lever_scraper
    assert lever_scraper.api_url({"slug": "cirrus", "region": "eu"}) == \
        "https://api.eu.lever.co/v0/postings/cirrus?mode=json"
    assert lever_scraper.api_url({"slug": "anduril"}) == \
        "https://api.lever.co/v0/postings/anduril?mode=json"
    assert ats_specs._extract_lever("https://jobs.eu.lever.co/Cirrus/abc") == \
        [{"slug": "cirrus", "name": "cirrus", "region": "eu"}]


def test_successfactors_status_and_guard(monkeypatch):
    from src import enrich

    class Resp:
        def __init__(self, code, text=""):
            self.status_code, self.text = code, text

    job = {"apply_url": "https://jobs.l3harris.com/job/X/1427597700/", "source": "successfactors",
           "job_id": "sf_l3harris_1427597700", "last_seen": "2026-10-06"}
    for code, text, want in [(200, '<meta itemprop="datePosted" content="x">', True),
                             (200, "<html>search results</html>", False),     # closed: page drops the job
                             (404, "", False), (503, "", None)]:
        monkeypatch.setattr(enrich._SESSION, "get", lambda url, timeout, c=code, t=text: Resp(c, t))
        assert enrich.successfactors_status(job) is want
    monkeypatch.setattr(enrich._SESSION, "get",
                        lambda url, timeout: Resp(200, 'itemprop="datePosted"'))
    assert enrich.still_open([job]) == {"sf_l3harris_1427597700"}


def test_gone_detail_page_retires_the_job(monkeypatch):
    import requests
    from src import enrich

    def gone(job):
        resp = requests.Response()
        resp.status_code = 410
        raise requests.HTTPError("410 Gone", response=resp)

    monkeypatch.setitem(enrich._FETCHERS, "icims", gone)
    job = {"job_id": "icims_careers-calamp_4326", "source": "icims",
           "apply_url": "https://careers-calamp.icims.com/jobs/4326/job"}
    assert enrich._description_for(job) == "" and job["closed"] is True
