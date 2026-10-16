"""BambooHR and JazzHR: listing parsing and id mapping."""
from src import ats_specs, bamboohr_scraper, dedupe, jazzhr_scraper

JAZZ_PAGE = """
<h1 class="brand-text">
     Montgomery Investment Technology, Inc.     </h1>
<ul class='list-group'>
    <li class="list-group-item">
        <h3 class='list-group-item-heading'>
            <a href="https://fintools.applytojob.com/apply/o7r6gV8IMh/Computer-Programming-Internship-Winter-2027">
                Computer Programming Internship Winter 2027            </a>
        </h3>
        <ul class='list-inline list-group-item-text'>
            <li><i class='fa fa-map-marker'></i>Cinnaminson, NJ</li>
            <li><i class='fa fa-sitemap'></i>Development</li>
        </ul>
    </li>
    <li class="list-group-item">
        <h3 class='list-group-item-heading'>
            <a href="http://fintools.applytojob.com/apply/r9VEpD9jo0/Marketing-Intern">
                Marketing &amp; Social Media Intern            </a>
        </h3>
        <ul class='list-inline list-group-item-text'>
        </ul>
    </li>
</ul>
"""


def test_jazzhr_board_page():
    jobs = jazzhr_scraper.parse_board(JAZZ_PAGE, "fintools", "Montgomery Investment Technology")
    assert [(j.job_id, j.title, j.location) for j in jobs] == [
        ("jazz_fintools_o7r6gV8IMh", "Computer Programming Internship Winter 2027", "Cinnaminson, NJ"),
        ("jazz_fintools_r9VEpD9jo0", "Marketing & Social Media Intern", ""),
    ]
    assert jobs[1].apply_url.startswith("https://")


def test_bamboohr_listing(monkeypatch):
    class Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"result": [
                {"id": "121", "jobOpeningName": "Image Processing Co-Op - Spring 2027",
                 "employmentStatusLabel": "Co-op",
                 "location": {"city": "Boston", "state": "Massachusetts"}, "atsLocation": {}},
                {"id": "72", "jobOpeningName": "Embedded Software Engineer",
                 "employmentStatusLabel": "Full-Time", "location": {}, "atsLocation": {},
                 "isRemote": True},
            ]}
    monkeypatch.setattr(bamboohr_scraper._SESSION, "get", lambda *a, **k: Resp())
    jobs, complete = bamboohr_scraper.fetch_company_jobs({"slug": "spectera", "name": "Spectera"},
                                                         {})
    assert complete
    assert [(j.job_id, j.location, j.role_hint) for j in jobs] == [
        ("bhr_spectera_121", "Boston, Massachusetts", "intern"),
        ("bhr_spectera_72", "Remote", ""),
    ]


def test_curated_links_map_to_scraped_ids():
    assert dedupe.canonical_job_id("https://specteraerospace.bamboohr.com/careers/121/") \
        == "bhr_specteraerospace_121"
    assert dedupe.canonical_job_id("https://acme.bamboohr.com/jobs/view.php?id=55") \
        == "bhr_acme_55"
    assert dedupe.canonical_job_id(
        "https://k1im.applytojob.com/apply/ISSTIdON0L/AI-Automation-Intern") \
        == "jazz_k1im_ISSTIdON0L"


def test_discovery_finds_boards_in_links():
    text = ("see https://specteraerospace.bamboohr.com/careers/121 and "
            "https://k1im.applytojob.com/apply/ISSTIdON0L/AI and "
            "https://ats.rippling.com/en-GB/rippling/jobs/3fd9615a-d0c7-458c-a0fc-5d9d7f0ce77c")
    found = ats_specs.extract_all(text, ats_specs.ATS_SPECS)
    assert [c["slug"] for c in found["bamboohr"]] == ["specteraerospace"]
    assert [c["slug"] for c in found["jazzhr"]] == ["k1im"]
    assert [c["slug"] for c in found["rippling"]] == ["rippling"]
