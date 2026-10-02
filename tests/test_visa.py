import pytest

from src import enrich, h1b, sheets


@pytest.mark.parametrize("text,flag", [
    ("CPT/OPT candidates are welcome to apply.", "yes"),
    ("F-1 students on OPT are eligible.", "yes"),
    ("Candidates on STEM OPT will be considered.", "yes"),
    ("We are unable to support OPT, CPT or visa sponsorship.", "no"),
    ("This role is not eligible for CPT.", "no"),
    ("Must be a U.S. citizen.", "no"),
    ("You can opt in to our newsletter.", ""),
])
def test_opt_cpt_flag(text, flag):
    assert enrich.parse_flags(text)["opt_cpt"] == flag


def test_normalize_employer_names():
    assert h1b.normalize("JPMORGAN CHASE & CO") == "jpmorgan chase"
    assert h1b.normalize("Susquehanna International Group (SIG)") == "susquehanna international"
    assert h1b.normalize("Amazon.com Services LLC") == "amazon com services"


def test_h1b_lookup_from_committed_data():
    assert h1b.approvals("Google") > 10_000
    assert h1b.approvals("AMD") > 500                    # alias -> Advanced Micro Devices
    assert h1b.approvals("JP Morgan Chase") > 1_000      # spacing differs from USCIS
    assert h1b.approvals("Zzqx Nonexistent Labs") is None


@pytest.mark.parametrize("job,n,verdict", [
    ({"citizenship": "required"}, 5000, "🇺🇸 US citizens / clearance only"),
    ({"sponsorship": "no"}, 5000, "❌ Posting rules out sponsorship"),
    ({"opt_cpt": "yes"}, None, "✅ Posting offers sponsorship / OPT"),
    ({}, 5000, "🟢 Likely: sponsors H-1Bs regularly"),
    ({}, 7, "🟡 Some H-1B history"),
    ({}, None, "⚪ Unknown"),
])
def test_visa_outlook(job, n, verdict):
    assert sheets.visa_outlook(job, n) == verdict
