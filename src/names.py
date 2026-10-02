"""Give discovered boards real company names.

Discovery names every board after its URL slug ("amat", "sharkninjaoperatingllc"),
which reads badly in Discord and defeats cross-source matching ("amat" on
Workday vs "Applied Materials" on LinkedIn). Names come from, in order:

  1. the ATS itself, where it exposes one (Greenhouse board name,
     SmartRecruiters company name);
  2. curated lists (SimplifyJobs format): the company_name most often
     attached to URLs on that board;
  3. slugs with separators, title-cased ("magnet-forensics" → "Magnet Forensics").

Only slug-names are replaced; a hand-written name is never touched. The old
name is kept under `aliases`, and main.py renames stored rows on the next run.

    python -m src.names            # fill names in config/*.yaml
"""

from __future__ import annotations

import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

import requests

from . import discover, simplify_scraper

_ID_FIELD = {"workday": "tenant", "greenhouse": "token", "lever": "slug",
             "ashby": "slug", "smartrecruiters": "company",
             "oracle": "host", "icims": "host", "jibe": "host", "rippling": "slug"}
_SESSION = requests.Session()
_SESSION.headers["User-Agent"] = "Mozilla/5.0 (job-tracker-names)"


def board_id(source: str, company: dict) -> str:
    return str(company[_ID_FIELD[source]]).lower()


def is_slug_name(source: str, company: dict) -> bool:
    name = (company.get("name") or "").strip().lower()
    bid = board_id(source, company)
    # Host-keyed boards (Oracle, iCIMS) get the host's first label as a name.
    return not name or name in (bid, bid.split(".")[0])


def _curated_names(settings: dict) -> dict[tuple[str, str], str]:
    """(source, board id) → the company name curated lists use for it."""
    votes: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for entry in settings.get("curated_lists", {}).get("repos", []):
        try:
            items = _SESSION.get(simplify_scraper._RAW.format(repo=entry["repo"]),
                                 timeout=60).json()
        except (requests.RequestException, ValueError) as exc:
            print(f"  ! {entry['repo']}: {exc}")
            continue
        for item in items:
            url, name = item.get("url") or "", (item.get("company_name") or "").strip()
            if not url or not name:
                continue
            for spec in discover.ATS_SPECS:
                for cand in spec.extract(url):
                    votes[(spec.name, str(cand[_ID_FIELD[spec.name]]).lower())][name] += 1
    out = {}
    for key, counter in votes.items():
        name, n = counter.most_common(1)[0]
        if n / sum(counter.values()) >= 0.6:     # ambiguous tenants stay unnamed
            out[key] = name
    return out


def _api_name(source: str, company: dict) -> str | None:
    try:
        if source == "greenhouse":
            r = _SESSION.get(f"https://boards-api.greenhouse.io/v1/boards/"
                             f"{company['token']}", timeout=20)
            return (r.json().get("name") or "").strip() or None if r.ok else None
        if source == "smartrecruiters":
            r = _SESSION.get(f"https://api.smartrecruiters.com/v1/companies/"
                             f"{company['company']}/postings", params={"limit": 1}, timeout=20)
            content = r.json().get("content") or [] if r.ok else []
            return ((content[0].get("company") or {}).get("name") or "").strip() or None \
                if content else None
    except (requests.RequestException, ValueError):
        return None
    return None


def _pretty_slug(slug: str) -> str | None:
    if not any(sep in slug for sep in "-_"):
        return None     # "abcsupply": no safe way to split words
    return " ".join(w.capitalize() for w in slug.replace("_", "-").split("-") if w)


def main() -> int:
    from .main import _load_yaml, _SETTINGS
    settings = _load_yaml(_SETTINGS)
    print("Collecting names from curated lists ...")
    curated = _curated_names(settings)

    for spec in discover.ATS_SPECS:
        companies, _ = discover._load_existing(spec)
        todo = [c for c in companies if is_slug_name(spec.name, c)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            api = list(pool.map(lambda c: _api_name(spec.name, c), todo))
        filled = 0
        for c, from_api in zip(todo, api):
            bid = board_id(spec.name, c)
            name = from_api or curated.get((spec.name, bid)) or _pretty_slug(bid)
            if not name or name.lower() == (c.get("name") or "").lower():
                continue
            old = c.get("name") or bid
            c["aliases"] = sorted(set(c.get("aliases", [])) | {old})
            c["name"] = name
            filled += 1
        discover._save_config(spec, companies)
        print(f"  {spec.name}: named {filled} of {len(todo)} slug-named boards "
              f"({len(companies)} total)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
