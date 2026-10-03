"""Validate config/settings.yaml before a run uses it.

Settings are read with .get(key, default), so a typo ("digest_hour") or a
wrong type ("max_pages_per_term: ten") would otherwise be silently ignored or
crash mid-run. validate() checks every key against SCHEMA and returns the
problems; main() refuses to start when there are any.
"""

from __future__ import annotations

import difflib

_NUM = (int, float)
_ROTATION = (int, dict)

# key -> type, or a nested dict schema for a section. A section mapping
# source -> settings (recency_check, scrape_workers_by_source, ...) is given
# as ("each", <schema of each value>).
SCHEMA: dict = {
    "search_terms": list,
    "page_limit": int,
    "max_pages_per_term": int,
    "workday_min_relevant_per_page": int,
    "recency_check": ("each", {"window_days": int, "max_pages": int}),
    "ci_shards": ("each", int),
    "disabled_sources": list,
    "long_tail_rotation": ("each", _ROTATION),
    "request_timeout": _NUM,
    "delay_between_requests": _NUM,
    "scrape_workers": int,
    "scrape_workers_by_source": ("each", int),
    "use_llm_fallback": bool,
    "enrich_descriptions": bool,
    "enrich_max_per_run": int,
    "enrich_backlog_per_run": int,
    "exclude_no_sponsorship": bool,
    "profile": {"enabled": bool, "grad_year": (int, type(None)), "needs_sponsorship": bool,
                "us_citizen": bool, "locations": list},
    "discord": {"digest_hours": _NUM},
    "announce_categories": list,
    "reposts": {"announce": str, "linkedin_min_age_days": int, "stale_after_days": int},
    "aggregator_ttl_days": int,
    "max_listing_age_days": int,
    "age_limit_exempt_sources": list,
    "store_roles": list,
    "prune_removed_after_days": int,
    "curated_lists": {"enabled": bool, "repos": list},
    "jobspy": {"enabled": bool, "sites": list, "search_terms": list, "location": str,
               "results_per_term": int, "hours_old": int},
    "health": {"history_runs": int, "drop_ratio": _NUM, "min_baseline": int,
               "max_run_minutes": _NUM},
    "phd": {"enabled": bool},
    "sheets": {"all_open_refresh_hours": _NUM},
}


def _type_name(t) -> str:
    ts = t if isinstance(t, tuple) else (t,)
    return " or ".join("null" if x is type(None) else x.__name__ for x in ts)


def _check(value, schema, path: str, errors: list[str]) -> None:
    if isinstance(schema, tuple) and len(schema) == 2 and schema[0] == "each":
        if not isinstance(value, dict):
            errors.append(f"{path}: expected a mapping, got {type(value).__name__}")
            return
        for k, v in value.items():
            _check(v, schema[1], f"{path}.{k}", errors)
        return
    if isinstance(schema, dict):
        if not isinstance(value, dict):
            errors.append(f"{path}: expected a mapping, got {type(value).__name__}")
            return
        for k, v in value.items():
            if k not in schema:
                hint = difflib.get_close_matches(k, list(schema), n=1)
                errors.append(f"{path}.{k}: unknown setting"
                              + (f" (did you mean {hint[0]!r}?)" if hint else ""))
            else:
                _check(v, schema[k], f"{path}.{k}", errors)
        return
    if isinstance(value, bool) and schema in (int, _NUM, _ROTATION):
        errors.append(f"{path}: expected {_type_name(schema)}, got bool")
    elif not isinstance(value, schema):
        errors.append(f"{path}: expected {_type_name(schema)}, got {type(value).__name__}")


def validate(settings: dict) -> list[str]:
    errors: list[str] = []
    for k, v in (settings or {}).items():
        if k not in SCHEMA:
            hint = difflib.get_close_matches(k, list(SCHEMA), n=1)
            errors.append(f"{k}: unknown setting" + (f" (did you mean {hint[0]!r}?)" if hint else ""))
        else:
            _check(v, SCHEMA[k], k, errors)
    return errors
