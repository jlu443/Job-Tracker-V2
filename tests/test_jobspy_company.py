import pytest

from src.jobspy_scraper import guess_company


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
