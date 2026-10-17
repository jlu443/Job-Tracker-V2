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


AVATURE_PAGE = """
<article class="article article--result" id="article--1">
  <h3 class="article__header__text__title"><a class="link link_result"
     href="https://jobs.ea.com/en_US/careers/JobDetail/Analytics-Intern/216232"> Analytics Intern </a></h3>
  <div class="article__header__text__subtitle"><span class="list-item-location">Austin, United States</span></div>
</article>
<article class="article--result">
  <h3><a href="https://koch.avature.net/en_US/careers/JobDetail/PIPELINE-RELIABILITY-INTERN/196283">
     PIPELINE RELIABILITY INTERN </a></h3>
  <div class="article__content__field"><div class="article__content__field__label"> Location: </div>
  <div class="article__content__field__value"> Rosemount, Minnesota </div></div>
</article>
<article class="article--result">
  <h3><a class="link" href="https://careers.twosigma.com/careers/JobDetail/NY-Quant-Researcher/14000">
     Quantitative Researcher </a></h3>
  <p class="paragraph"><strong>Location</strong> <span class="paragraph_inner-span">New York</span></p>
  <p class="paragraph"><strong>Experience Level</strong> <span class="paragraph_inner-span">Internship</span></p>
</article>
"""


def test_avature_results_in_each_layout():
    from src import avature_scraper
    jobs = avature_scraper.parse_results(AVATURE_PAGE, "X")
    assert [(j.job_id, j.title, j.location, j.role_hint) for j in jobs] == [
        ("av_jobs.ea.com_216232", "Analytics Intern", "Austin, United States", ""),
        ("av_koch_196283", "PIPELINE RELIABILITY INTERN", "Rosemount, Minnesota", ""),
        ("av_twosigma_14000", "Quantitative Researcher", "New York", "intern"),
    ]
    # curated links on the avature.net name map to the same ids
    assert dedupe.canonical_job_id("https://twosigma.avature.net/careers/JobDetail/14000") \
        == "av_twosigma_14000"
    found = ats_specs.extract_all("https://apply.deloitte.com/en_US/careers/JobDetail/x/370741",
                                  ats_specs.ATS_SPECS)
    assert found["avature"] == [{"base": "https://apply.deloitte.com/en_US/careers",
                                 "name": "apply.deloitte.com"}]


JOBVITE_PAGE = """
<table class="jv-job-list"><tbody>
<tr><td class="jv-job-list-name"> <a href="/tylertech/job/o5APAfwO">DevOps Engineer Intern, Summer 2027</a> </td>
    <td class="jv-job-list-location"> Plano, Texas </td></tr>
<tr><td class="jv-job-list-name"> <a href="/tylertech/job/odCGAfwP">Receptionist</a> </td>
    <td class="jv-job-list-location"> <div class="jv-meta"> 2 Locations </div> </td></tr>
</tbody></table>
"""


def test_jobvite_list_and_ids():
    from src import jobvite_scraper
    jobs = jobvite_scraper.parse_list(JOBVITE_PAGE, "Tyler Technologies")
    assert [(j.job_id, j.title, j.location) for j in jobs] == [
        ("jv_tylertech_o5APAfwO", "DevOps Engineer Intern, Summer 2027", "Plano, Texas"),
        ("jv_tylertech_odCGAfwP", "Receptionist", "2 Locations"),
    ]
    assert dedupe.canonical_job_id("https://jobs.jobvite.com/tylertech/job/o5APAfwO?nl=1&fr=false") \
        == "jv_tylertech_o5APAfwO"
    found = ats_specs.extract_all("https://jobs.jobvite.com/src-inc/jobs and "
                                  "https://jobs.jobvite.com/nutanix/job/oQLOAfwJ", ats_specs.ATS_SPECS)
    assert [c["slug"] for c in found["jobvite"]] == ["src-inc", "nutanix"]


def test_taleo_and_paylocity_ids():
    from src import taleo_scraper
    assert dedupe.canonical_job_id(
        "https://textron.taleo.net/careersection/textron/jobdetail.ftl?job=342550") \
        == "tl_textron_342550"
    assert taleo_scraper.job_id_for("textron.taleo.net", "342550") == "tl_textron_342550"
    assert taleo_scraper._locations('["US-Maryland-Hunt Valley", "US-Texas-Austin"]') \
        == "Hunt Valley, Maryland, US; Austin, Texas, US"
    assert dedupe.canonical_job_id(
        "https://recruiting.paylocity.com/Recruiting/Jobs/Details/4361617") == "pl_4361617"
    found = ats_specs.extract_all(
        "https://textron.taleo.net/careersection/textron/jobdetail.ftl?job=342550 "
        "https://recruiting.paylocity.com/Recruiting/Jobs/All/5bc712a4-af9e-4b78-a8c7-a17c4cbac043",
        ats_specs.ATS_SPECS)
    assert found["taleo"][0]["host"] == "textron.taleo.net" and found["taleo"][0]["section"] == "textron"
    assert found["paylocity"][0]["guid"] == "5bc712a4-af9e-4b78-a8c7-a17c4cbac043"
