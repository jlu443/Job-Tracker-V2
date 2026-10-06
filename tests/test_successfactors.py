from src import dedupe, successfactors_scraper as sf

# Trimmed from real result pages (careers.qorvo.com, jobs.l3harris.com), 2026-10-03.
TABLE = """
<span class="paginationLabel">Results <b>1 &ndash; 25</b> of <b>209</b></span>
<tr class="data-row">
 <td class="colTitle"><span class="jobTitle hidden-phone">
  <a href="/job/Greensboro-Physical-Verification-Intern-NC-27409/1422901500/" class="jobTitle-link">Physical Verification Intern</a></span>
  <span class="jobLocation visible-phone"><span class="jobLocation"> Greensboro, NC, US, 27409 </span></span></td>
</tr>
<tr class="data-row">
 <td class="colTitle"><a class="jobTitle-link" href="/job/Richardson-Sourcing-Analyst-Intern-TX-75080/1422800500/">Sourcing Analyst Intern &amp; Co-op</a>
 <span class="jobLocation"> Richardson, TX, US, 75080 </span></td>
</tr>
"""
TILES = """
<ul id="job-tile-list" class="container job-list" aria-rowcount="949" data-per-page="100">
<li class="job-tile job-id-1413204900 job-row-index-1" data-url="/job/Nashville-Principal%2C-Contracts-TN-37203/1413204900/">
 <a class="jobTitle-link fontcolor59" href="/job/Nashville-Principal%2C-Contracts-TN-37203/1413204900/"> Principal, Contracts </a>
 <div id="job-1413204900-desktop-section-location" class="section-field location">
  <span id="job-1413204900-desktop-section-location-label" class="section-label">Location</span>
  <div><span id="job-1413204900-desktop-section-location-value" class="section-value">Nashville, TN, US, 37203</span></div>
 </div>
</li>
"""


def test_parses_both_layouts():
    rows, total = sf.parse_search(TABLE)
    assert total == 209
    assert rows == [
        ("1422901500", "/job/Greensboro-Physical-Verification-Intern-NC-27409/1422901500/",
         "Physical Verification Intern", "Greensboro, NC, US, 27409"),
        ("1422800500", "/job/Richardson-Sourcing-Analyst-Intern-TX-75080/1422800500/",
         "Sourcing Analyst Intern & Co-op", "Richardson, TX, US, 75080")]
    rows, total = sf.parse_search(TILES)
    assert total == 949
    assert rows == [("1413204900", "/job/Nashville-Principal%2C-Contracts-TN-37203/1413204900/",
                     "Principal, Contracts", "Nashville, TN, US, 37203")]


def test_stops_when_site_repeats_its_last_page(monkeypatch):
    calls = []

    class Resp:
        text = TABLE.replace("<b>209</b>", "<b>9999</b>")     # claims more than it has

        def raise_for_status(self):
            pass

    def get(url, timeout, params):
        calls.append(params["startrow"])
        return Resp()

    monkeypatch.setattr(sf._SESSION, "get", get)
    monkeypatch.setattr(sf.time, "sleep", lambda s: None)
    jobs, complete = sf.fetch_company_jobs(
        {"host": "careers.qorvo.com", "name": "Qorvo", "search_terms": ["intern"]}, {})
    assert complete and calls == [0, 2] and len(jobs) == 2
    assert jobs[0].job_id == "sf_qorvo_1422901500"
    assert jobs[0].apply_url == ("https://careers.qorvo.com/job/"
                                 "Greensboro-Physical-Verification-Intern-NC-27409/1422901500/")


def test_ids_only_for_configured_hosts(monkeypatch):
    monkeypatch.setattr(sf, "_configured_hosts", lambda: frozenset({"careers.qorvo.com"}))
    assert dedupe.canonical_job_id(
        "https://careers.qorvo.com/job/Greensboro-Intern-NC-27409/1422901500/") == \
        "sf_qorvo_1422901500"
    assert dedupe.canonical_job_id("https://example.com/job/Some-Job/1234567/") is None
    assert sf.site_key("assaabloy.jobs2web.com") == "assaabloy"


def test_bytedance_urls_map_to_scraper_ids():
    assert dedupe.canonical_job_id(
        "https://jobs.bytedance.com/en/position/7668212952030841093/detail") == \
        "bd_7668212952030841093"
    assert dedupe.canonical_job_id("https://joinbytedance.com/search/7668212952030841093") == \
        "bd_7668212952030841093"


def test_brand_prefixed_links():
    """Multi-brand sites (Mohawk) put the brand before /job/."""
    page = TILES.replace('href="/job/', 'href="/DalTile/job/')
    rows, _ = sf.parse_search(page)
    assert rows[0][:2] == ("1413204900", "/DalTile/job/Nashville-Principal%2C-Contracts-TN-37203/1413204900/")


def test_reads_whole_listing_when_small(monkeypatch):
    queries = []

    class Resp:
        text = TABLE.replace("<b>209</b>", "<b>2</b>")

        def raise_for_status(self):
            pass

    def get(url, timeout, params):
        queries.append(params["q"])
        return Resp()

    monkeypatch.setattr(sf._SESSION, "get", get)
    monkeypatch.setattr(sf.time, "sleep", lambda s: None)
    jobs, complete = sf.fetch_company_jobs({"host": "careers.qorvo.com", "name": "Qorvo"}, {})
    assert complete and len(jobs) == 2
    assert set(queries) == {""}         # no keyword searches


def test_keyword_fallback_for_large_listing(monkeypatch):
    queries = []

    class Resp:
        def __init__(self, q):
            self.text = TABLE.replace("<b>209</b>", "<b>99999</b>" if q == "" else "<b>2</b>")

        def raise_for_status(self):
            pass

    def get(url, timeout, params):
        queries.append(params["q"])
        return Resp(params["q"])

    monkeypatch.setattr(sf._SESSION, "get", get)
    monkeypatch.setattr(sf.time, "sleep", lambda s: None)
    jobs, complete = sf.fetch_company_jobs({"host": "careers.qorvo.com", "name": "Qorvo"},
                                           {"successfactors_full_sweep_max_pages": 10})
    assert complete and queries[0] == "" and set(queries[1:]) == set(sf._TERMS)
