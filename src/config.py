"""Validate config/settings.yaml before a run uses it.

Settings are read with .get(key, default), so a typo ("digest_hour") or a
wrong type ("max_pages_per_term: ten") would otherwise be silently ignored or
crash mid-run. validate() checks every key against SCHEMA and returns the
problems; main() refuses to start when there are any.
"""

from __future__ import annotations

import difflib
import logging
import os
import sys
from collections import Counter

import yaml

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
    "retry_incomplete": ("each", {"delay": _NUM, "workers": int}),
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


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "config")
SETTINGS_PATH = os.path.join(CONFIG_DIR, "settings.yaml")


def load_yaml(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_settings() -> dict:
    return load_yaml(SETTINGS_PATH)


# Warnings logged this run, by module: the run summary reports them.
WARNINGS: Counter = Counter()


class _CountWarnings(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        WARNINGS[record.name.rsplit(".", 1)[-1]] += 1


def setup_logging(level: int = logging.INFO) -> None:
    """Plain messages on stdout (what CI shows), warnings counted."""
    logging.basicConfig(level=level, format="%(message)s", stream=sys.stdout, force=True)
    counter = _CountWarnings(level=logging.WARNING)
    logging.getLogger().addHandler(counter)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

