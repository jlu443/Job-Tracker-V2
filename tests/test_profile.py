import pytest

from src import profile

ON = {"enabled": True}


@pytest.mark.parametrize("job,prof,reasons", [
    ({"sponsorship": "no"}, {**ON, "needs_sponsorship": True}, ["no visa sponsorship"]),
    ({"citizenship": "required"}, {**ON, "us_citizen": False}, ["US citizens / clearance only"]),
    ({"clearance": "yes"}, {**ON, "us_citizen": True}, []),
    ({"grad_year": "2026"}, {**ON, "grad_year": 2027}, ["grad year 2026"]),
    ({"grad_year": "2026, 2027"}, {**ON, "grad_year": 2027}, []),
    ({"grad_year": ""}, {**ON, "grad_year": 2027}, []),                  # unknown keeps
    ({"location": "Austin, TX"}, {**ON, "locations": ["CA", "Remote"]}, ["location"]),
    ({"location": "San Jose, CA, US"}, {**ON, "locations": ["CA"]}, []),
    ({"location": "Remote - US"}, {**ON, "locations": ["CA", "Remote"]}, []),
    ({"location": "Seattle, WA; Austin, TX"}, {**ON, "locations": ["Seattle"]}, []),
    ({"location": "Canada"}, {**ON, "locations": ["CA"]}, ["location"]),  # 'CA' is a state code
    ({"sponsorship": "no"}, {"enabled": False, "needs_sponsorship": True}, []),
])
def test_reasons_to_skip(job, prof, reasons):
    assert profile.reasons_to_skip(job, prof) == reasons
