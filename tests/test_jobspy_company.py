import pytest

from src.jobspy_scraper import company_from_url, guess_company


@pytest.mark.parametrize("description,url,company", [
    # Real Indeed descriptions that came without a company field (2026-10-03).
    (r"Engineering \& Sales Coordinator" "\n\n" r"Homeland Safety Systems \| Shreveport, LA"
     "\n" r"**Entry\-level role.**" "\n\n**About Homeland Safety Systems**\n\nHomeland Safety ...",
     "", "Homeland Safety Systems"),
    ("**Position Summary**\nImpact Electronic Solutions is seeking an Electronic Engineering "
     "Intern to join", "", "Impact Electronic Solutions"),
    ("**Job Summary:** GM Performance Power Units is seeking an intern to support",
     "", "GM Performance Power Units"),
    ("Transpo Group is looking for an entry level Transportation/Traffic Planners",
     "", "Transpo Group"),
    ("Description: The Java Software Engineer must be able to design",
     "https://recruiting.paylocity.com/Recruiting/Jobs/Details/4555923/C-Mack-Solutions-LLC/"
     "Java-Software-Engineer?source=Indeed_Feed", "C Mack Solutions LLC"),
    ("**About the Role**\nThe team is seeking a developer.", "", ""),
    ("Overview: We are seeking an accomplished Senior Application Strategist", "", ""),
    ("Summary: As a Software Engineer at NSA, you can focus on", "", ""),
])
def test_guess_company(description, url, company):
    assert guess_company(description, url) == company


@pytest.mark.parametrize("url,company", [
    # Unnamed Indeed rows, 2026-10-06
    ("https://jobs.ashbyhq.com/parisi-labs/c5fb9254-f3ad-45f4-9056-4d8ec8427225?utm_source=x",
     "Parisi Labs"),
    ("https://baskandlather.bamboohr.com/careers/93?source=indeed", "Baskandlather"),
    ("https://apply.workable.com/j/850451DAD1", ""),           # no account in the link
    ("https://www.usajobs.gov:443/job/887394300", ""),
    ("", ""),
])
def test_company_from_url(url, company):
    assert company_from_url(url)[0] == company


def test_configured_board_name_beats_the_text(monkeypatch):
    monkeypatch.setattr("src.jobspy_scraper._board_names",
                        lambda: {("greenhouse", "acmerobotics"): "Acme Robotics"})
    assert company_from_url("https://boards.greenhouse.io/acmerobotics/jobs/123") == \
        ("Acme Robotics", True)
    assert guess_company("About Other Name\n", "https://boards.greenhouse.io/acmerobotics/jobs/1") \
        == "Acme Robotics"
