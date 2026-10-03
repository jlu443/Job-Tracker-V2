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
