from src import config, main


def test_committed_settings_are_valid():
    assert config.validate(main._load_yaml(main._SETTINGS)) == []


def test_typos_and_wrong_types_are_reported():
    errors = config.validate({
        "discord": {"digest_hour": 3},          # typo
        "max_pages_per_term": "ten",            # wrong type
        "enrich_descriptions": True,            # fine
        "recency_check": {"workday": {"window_days": True}},   # bool is not an int
        "srore_roles": ["intern"],              # unknown top-level
    })
    assert "discord.digest_hour: unknown setting (did you mean 'digest_hours'?)" in errors
    assert "max_pages_per_term: expected int, got str" in errors
    assert "recency_check.workday.window_days: expected int, got bool" in errors
    assert "srore_roles: unknown setting (did you mean 'store_roles'?)" in errors
    assert len(errors) == 4
