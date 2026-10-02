"""Company H-1B sponsorship history, from USCIS's H-1B Employer Data Hub.

USCIS publishes, per fiscal year, every employer with an H-1B petition
decision (initial + continuing approvals). Summed over recent years, it's
the best public signal of whether a company sponsors visas: a company that
files H-1Bs also routinely hires F-1 students on OPT / STEM OPT, the usual
path into H-1B. (No public per-employer data exists for OPT or CPT; postings
that state it are read by enrich.parse_flags as opt_cpt.)

    python -m src.h1b --build 2021 2022 2023   # refresh config/h1b_employers.json.gz

The built file is committed, so CI never downloads government data.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import os
import re
import sys
from functools import lru_cache

import requests

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_PATH = os.path.join(_ROOT, "config", "h1b_employers.json.gz")
_URL = "https://www.uscis.gov/sites/default/files/document/data/h1b_datahubexport-{year}.csv"

_SUFFIXES = {"inc", "llc", "corp", "corporation", "co", "company", "ltd", "limited", "lp",
             "llp", "plc", "pc", "pllc", "na", "the", "incorporated", "holdings", "group",
             "and"}
ALIASES_PATH = os.path.join(_ROOT, "config", "h1b_aliases.yaml")
# A prefix match ("amazon" -> "amazon development center us") skips these
# industries, where an unrelated business often shares a famous name
# ("apple hospitality reit"): real estate, hospitality, arts, personal
# services, agriculture, construction, staffing.
_PREFIX_EXCLUDED_NAICS = {"53", "72", "71", "81", "11", "23", "56"}


def normalize(name: str) -> str:
    n = (name or "").lower().split(" dba ")[0]
    n = re.sub(r"\([^)]*\)", " ", n)          # "Susquehanna (SIG)", "PwC (PricewaterhouseCoopers)"
    n = n.replace("&", " and ").replace(".com", " com")
    words = re.sub(r"[^a-z0-9 ]+", " ", n).split()
    while words and words[-1] in _SUFFIXES:
        words.pop()
    while words and words[0] == "the":
        words.pop(0)
    return " ".join(words)


def build(years: list[int], path: str = DATA_PATH) -> int:
    totals: dict[str, list] = {}
    for year in years:
        resp = requests.get(_URL.format(year=year), timeout=120,
                            headers={"User-Agent": "Mozilla/5.0 (job-tracker)"})
        resp.raise_for_status()
        for row in csv.DictReader(io.StringIO(resp.content.decode("utf-8", "replace"))):
            norm = normalize(row.get("Employer") or "")
            if not norm:
                continue
            approvals = sum(int((row.get(k) or "0").replace(",", "") or 0)
                            for k in ("Initial Approval", "Continuing Approval"))
            entry = totals.setdefault(norm, [0, (row.get("NAICS") or "")[:2]])
            entry[0] += approvals
    payload = {"years": years, "employers": totals}
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"))
    return len(totals)


@lru_cache(maxsize=1)
def _data() -> tuple[dict, list, dict]:
    """(employers by normalized name, years, normalized names by their
    space-free form — "JP Morgan Chase" vs USCIS's "JPMORGAN CHASE")."""
    if not os.path.exists(DATA_PATH):
        return {}, [], {}
    with gzip.open(DATA_PATH, "rt", encoding="utf-8") as fh:
        d = json.load(fh)
    compact: dict[str, list] = {}
    for name in d["employers"]:
        compact.setdefault(name.replace(" ", ""), []).append(name)
    return d["employers"], d["years"], compact


@lru_cache(maxsize=1)
def _aliases() -> dict:
    if not os.path.exists(ALIASES_PATH):
        return {}
    import yaml
    with open(ALIASES_PATH, encoding="utf-8") as fh:
        raw = (yaml.safe_load(fh) or {}).get("aliases") or {}
    return {normalize(k): v for k, v in raw.items()}


def years_label() -> str:
    years = _data()[1]
    return f"FY{str(min(years))[2:]}–{str(max(years))[2:]}" if years else ""


@lru_cache(maxsize=4096)
def approvals(company: str) -> int | None:
    """H-1B approvals summed over the loaded years for every legal entity of
    `company` (exact name, or name followed by more words, e.g. "Amazon" ->
    "Amazon Development Center US"). None when no employer matches."""
    employers, _, compact = _data()
    norm = normalize(company)
    norm = normalize(_aliases().get(norm, company))
    if len(norm) < 3 or not employers:
        return None
    total, found = 0, False
    prefix = norm + " "
    for name, (count, naics) in employers.items():
        if name == norm or (name.startswith(prefix) and naics not in _PREFIX_EXCLUDED_NAICS):
            total += count
            found = True
    if not found:
        for name in compact.get(norm.replace(" ", ""), []):
            total += employers[name][0]
            found = True
    return total if found else None


def label(company: str) -> str:
    n = approvals(company)
    if n is None:
        return "not found"
    return f"{n:,} approvals ({years_label()})"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--build", nargs="+", type=int, metavar="YEAR", required=True)
    args = ap.parse_args()
    n = build(args.build)
    print(f"Wrote {n} employers for FY{args.build} to {DATA_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
