from datetime import date

import pytest

from src import classify, dates, dedupe, geo


@pytest.mark.parametrize("title,role", [
    ("Software Engineering Intern - Summer 2027", "intern"),
    ("Student Web Developer", "intern"),
    ("ASIC Verification Engineer - New College Grad 2026", "new_grad"),
    ("Software Engineer I", "new_grad"),
    ("Software Engineer 1, Payments", "new_grad"),
    ("Junior Backend Developer", "new_grad"),
    ("Associate Product Manager", "new_grad"),
    ("Data Scientist, Core Data - PhD (2026)", "new_grad"),
    ("Early Talent Software Engineer", "new_grad"),
    ("Software Engineer II", "mid"),
    ("Senior Software Engineer", "senior"),
    ("Staff Engineer, Infrastructure", "senior"),
    ("Lead Internal Events Strategist", "senior"),   # "internal" is not "intern"
    ("Software Engineer", None),
])
def test_role_keywords(title, role):
    assert classify.classify_by_keyword(title) == role


@pytest.mark.parametrize("title,category", [
    ("Software Engineer Intern", "software"),
    ("Software Engineer, Manufacturing Systems", "software"),
    ("Machine Learning Engineer - New Grad", "data_ml"),
    ("ASIC Design Verification Engineer", "hardware"),
    ("Quantitative Trading Intern", "quant"),
    ("Associate Product Manager", "product"),
    ("Mechanical Engineer Intern", "other"),
    ("Sales Intern", "other"),
    ("Legal Affairs Intern", "other"),
    ("Forward Deployed Engineer - Intern", "software"),
    ("Traffic Engineering Intern- Summer 2027", "other"),
    ("Bridge Engineering Intern", "other"),
    ("Production Engineer Intern, Summer 2027", "other"),
    ("RF Engineer I", "hardware"),
    ("Software Engineer, Production Systems", "software"),
])
def test_categories(title, category):
    assert classify.categorize(title) == category


@pytest.mark.parametrize("loc,us", [
    ("Austin, TX", True),
    ("Milwaukee, WI", True),                 # contains "uk"
    ("Indianapolis, IN", True),              # contains "india"
    ("San Francisco, CA, US", True),
    ("Remote - US", True),
    ("United States", True),
    ("New York, NY; London, UK", True),
    ("", True),
    ("Pune, MH, IN", False),                 # IN as country, not Indiana
    ("Toronto, ON, CA", False),              # CA as country, not California
    ("London, England, United Kingdom", False),
    ("Bengaluru, Karnataka", False),
    ("Remote - Canada", False),
    ("Latin America", False),
    ("Alzenau,DEU", False),                  # Workday ISO-3 country
    ("Austin,USA", True),
    ("Santa Clara,CA", True),
])
def test_is_us(loc, us):
    assert geo.is_us(loc) is us


def test_relative_dates():
    anchor = date(2026, 9, 25)
    assert dates.relative_to_iso("Posted Today", anchor) == "2026-09-25"
    assert dates.relative_to_iso("Posted Yesterday", anchor) == "2026-09-24"
    assert dates.relative_to_iso("Posted 3 Days Ago", anchor) == "2026-09-22"
    assert dates.relative_to_iso("Posted 30+ Days Ago", anchor) == "2026-08-26"
    assert dates.relative_to_iso("2 weeks ago", anchor) == "2026-09-11"
    assert dates.relative_to_iso("2026-06-08", anchor) == "2026-06-08"
    assert dates.relative_to_iso("", anchor) == ""
    assert dates.relative_to_iso("garbage", anchor) == ""


@pytest.mark.parametrize("url,job_id", [
    ("https://boards.greenhouse.io/stripe/jobs/8172510", "gh_8172510"),
    ("https://job-boards.greenhouse.io/figma/jobs/5551234?gh_src=x", "gh_5551234"),
    ("https://careers.acme.com/open?gh_jid=4242424", "gh_4242424"),
    ("https://jobs.lever.co/palantir/0a1b2c3d-1111-2222-3333-444455556666",
     "lv_0a1b2c3d-1111-2222-3333-444455556666"),
    ("https://jobs.ashbyhq.com/openai/0a1b2c3d-1111-2222-3333-444455556666/application",
     "ash_0a1b2c3d-1111-2222-3333-444455556666"),
    ("https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/US-CA-Santa-Clara/"
     "Intern_JR2017296-1", "wd_nvidia_JR2017296-1"),
    ("https://fau.wd1.myworkdayjobs.com/en-US/FAU/job/Boca-Raton/Dev_REQ22952/apply",
     "wd_fau_REQ22952"),
    ("https://www.linkedin.com/jobs/view/4471636508", "li_4471636508"),
    ("https://www.linkedin.com/jobs/view/software-engineer-at-acme-4471636508", "li_4471636508"),
    ("https://careers.medpace.com/jobs/12564", None),
])
def test_canonical_job_id(url, job_id):
    assert dedupe.canonical_job_id(url) == job_id


@pytest.mark.parametrize("title,foreign", [
    ("NVIDIA 2027 New College Graduate: GPU Architecture Engineering - China", True),
    ("Software Engineer Intern (London)", True),
    ("Software Engineer Intern - Summer 2027", False),
    ("Software Engineer - Machine Learning", False),
])
def test_title_names_foreign_place(title, foreign):
    assert geo.title_names_foreign_place(title) is foreign


@pytest.mark.parametrize("text,sponsorship", [
    ("Required Qualifications:\nHS Diploma.\nUS Citizenship.\nProgress toward a degree.", "no"),
    ("Must be a U.S. citizen.", "no"),
    ("We hire regardless of US citizenship status.", ""),
    ("Visa sponsorship is available for this role.", "yes"),
])
def test_sponsorship_flags(text, sponsorship):
    from src import enrich
    assert enrich.parse_flags(text)["sponsorship"] == sponsorship


def test_icims_location_normalization():
    from src import icims_scraper
    assert icims_scraper.normalize_location("US-VA-Herndon") == "Herndon, VA, US"
    assert icims_scraper.normalize_location("US-MD-Silver Spring | US-NJ-Basking Ridge") == \
        "Silver Spring, MD, US; Basking Ridge, NJ, US"
    assert geo.is_us("Bangalore, KA, IN") is False


@pytest.mark.parametrize("url,job_id", [
    ("https://ibqbjb.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/Honeywell/job/137946",
     "orc_ibqbjb_137946"),
    ("https://careers-gdms.icims.com/jobs/71647/junior-full-stack-engineer/job", "icims_careers-gdms_71647"),
    ("https://wd5.myworkdaysite.com/recruiting/microchiphr/External/job/CA---Santa-Rosa/Engineer-I---Software_R2844-26",
     "wd_microchiphr_R2844-26"),
    ("https://boards.greenhouse.io/embed/job_app?token=7669159003", "gh_7669159003"),
])
def test_new_canonical_ids(url, job_id):
    assert dedupe.canonical_job_id(url) == job_id


@pytest.mark.parametrize("title,desc,hint", [
    ("Software Engineer", "Open to recent graduates. 0-2 years of experience.", "new_grad"),
    ("Software Engineer", "Class of 2026 graduates welcome.", "new_grad"),
    ("Software Engineer", "0-2 years preferred, but 5+ years of experience required.", ""),
    ("Software Engineer", "We build great products.", ""),
    ("Senior Software Engineer", "Recent graduates mentor program.", ""),   # title decides
    ("Software Engineering Intern", "Recent graduates welcome.", ""),        # title decides
])
def test_role_hint_from_description(title, desc, hint):
    assert classify.role_hint_from_description(title, desc) == hint


@pytest.mark.parametrize("title,role", [
    ("Graduate Performance Engineer", "new_grad"),
    ("Quantitative Developer, Graduate", "new_grad"),
    ("Software Engineer (Grad)", "new_grad"),
    ("Graduate Research Assistant", "intern"),
    ("Graduate Student Intern", "intern"),
])
def test_graduate_titles(title, role):
    assert classify.classify_by_keyword(title) == role


@pytest.mark.parametrize("text,expected", [
    ("Must be a U.S. citizen. Active Secret clearance required.",
     {"sponsorship": "no", "citizenship": "required", "clearance": "yes"}),
    ("We are unable to sponsor visas. No security clearance required.",
     {"sponsorship": "no", "citizenship": "", "clearance": "none"}),
    ("H-1B sponsorship is available for this role.",
     {"sponsorship": "yes", "citizenship": "", "clearance": ""}),
    ("Build great software with us.", {"sponsorship": "", "citizenship": "", "clearance": ""}),
])
def test_flags_separate_citizenship_and_explicit_no_clearance(text, expected):
    from src import enrich
    flags = enrich.parse_flags(text)
    assert {k: flags[k] for k in expected} == expected


def test_scrape_time_flags_only_for_entry_level():
    from src import enrich
    desc = "Must be a US citizen."
    assert enrich.scrape_time_flags("Senior Engineer", desc) == {}
    flags = enrich.scrape_time_flags("Software Engineer Intern", desc)
    assert flags["citizenship"] == "required" and flags["checked"] is True


@pytest.mark.parametrize("text,pay", [
    ("Base Pay Range: $141,773.00 - $162,000.00 per year", "$142k–162k/yr"),
    ("the US: $95,698.00-95,702.00 USD (Hourly Role)", "$96k/yr"),
    ("$45/hr - $55/hr", "$45–55/hr"),
    ("pay $25 to $32 per hour", "$25–32/hr"),
    ("$257K - $335K", "$257k–335k/yr"),
    ("raised $10 to $20 million in funding", ""),
    ("We match your 401k", ""),
])
def test_parse_pay(text, pay):
    from src import enrich
    assert enrich.parse_pay(text) == pay
