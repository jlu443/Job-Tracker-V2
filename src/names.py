"""Give discovered boards real company names.

Discovery names every board after its URL slug ("amat", "sharkninjaoperatingllc"),
which reads badly in Discord and defeats cross-source matching ("amat" on
Workday vs "Applied Materials" on LinkedIn). Names come from, in order:

  1. the ATS itself, where it exposes one (Greenhouse board name,
     SmartRecruiters company name; Workday job pages' hiring entity, used
     only to spell the slug properly: "roberthalf" -> "Robert Half");
  2. curated lists (SimplifyJobs format): the company_name most often
     attached to URLs on that board;
  3. slugs with separators, title-cased ("magnet-forensics" → "Magnet Forensics").

Only slug-names are replaced; a hand-written name is never touched. The old
name is kept under `aliases`, and main.py renames stored rows on the next run.

    python -m src.names            # fill names in config/*.yaml
"""

from __future__ import annotations

import html
import logging
import re
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

import requests

from . import ats_specs, simplify_scraper, successfactors_scraper

log = logging.getLogger(__name__)

_ID_FIELD = {"workday": "tenant", "greenhouse": "token", "lever": "slug",
             "ashby": "slug", "smartrecruiters": "company",
             "oracle": "host", "icims": "host", "jibe": "host", "rippling": "slug",
             "eightfold": "tenant", "successfactors": "host"}
_SESSION = requests.Session()
_SESSION.headers["User-Agent"] = "Mozilla/5.0 (job-tracker-names)"


def board_id(source: str, company: dict) -> str:
    return str(company[_ID_FIELD[source]]).lower()


def is_slug_name(source: str, company: dict) -> bool:
    name = (company.get("name") or "").strip().lower()
    bid = board_id(source, company)
    # Host-keyed boards (Oracle, iCIMS) get the host's first label as a name,
    # or the host itself, title-cased ("Careers2 Quanta.icims.com").
    flat = lambda t: re.sub(r"[^a-z0-9]", "", t)
    return (not name or name in (bid, bid.split(".")[0])
            or ("." in name and flat(name) in (flat(bid), flat(bid.split(".")[0]))))


def _curated_names(settings: dict) -> dict[tuple[str, str], str]:
    """(source, board id) → the company name curated lists use for it."""
    votes: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for entry in settings.get("curated_lists", {}).get("repos", []):
        try:
            items = _SESSION.get(simplify_scraper._RAW.format(repo=entry["repo"]),
                                 timeout=60).json()
        except (requests.RequestException, ValueError) as exc:
            log.warning(f"  ! {entry['repo']}: {exc}")
            continue
        for item in items:
            url, name = item.get("url") or "", (item.get("company_name") or "").strip()
            if not url or not name:
                continue
            for spec in ats_specs.ATS_SPECS:
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
        if source == "workday":
            return spelled_name(company["tenant"], _workday_entities(company))
        if source in ("successfactors", "icims"):
            # Page titles ("Kiewit Jobs", "Careers - Barrios") spell the
            # company part of the host: kiewitcareers.kiewit.com -> "kiewit",
            # collegerecruitment-barrios.icims.com -> "barrios".
            host = company["host"]
            if source == "icims":
                key, url = host.split(".")[0].split("-")[-1], f"https://{host}/jobs/search?ss=1"
            else:
                key, url = successfactors_scraper.site_key(host), f"https://{host}/search/"
            page = _SESSION.get(url, timeout=20).text
            titles = re.findall(r"<title[^>]*>([^<]{2,200})</title>", page)
            titles += re.findall(r'og:(?:site_name|title)"\s+content="([^"]+)"', page)
            return spelled_name(key, [html.unescape(t) for t in titles])
    except (requests.RequestException, ValueError):
        return None
    return None


_MINOR_WORDS = {"of", "and", "&", "the", "for", "de"}


def spelled_name(slug: str, candidates: list[str]) -> str | None:
    """The slug's proper spelling, taken from names that spell it out:
    "roberthalf" in "Robert Half" -> "Robert Half", "sifive" in "SiFive India
    Private" -> "SiFive"; or whose initials it is ("becu", "Boeing Employees'
    Credit Union" -> "BECU"). A name that doesn't spell the slug is ignored:
    Workday's hiring entity is often a subsidiary (McKesson's "JSC SCRI
    Holdings", Medtronic's "COV Covidien")."""
    key = re.sub(r"[^a-z0-9]", "", slug.lower())
    if len(key) < 2:
        return None
    votes: Counter = Counter()
    for cand in candidates:
        tokens = [t for t in re.split(r"\s+", cand or "") if t]
        flat = [re.sub(r"[^a-z0-9]", "", t.lower()) for t in tokens]
        found = None
        for i in range(len(tokens)):
            if not flat[i]:         # "Careers - Barrios": don't start at the "-"
                continue
            joined = ""
            for j in range(i, len(tokens)):
                joined += flat[j]
                if joined == key:
                    found = " ".join(tokens[i:j + 1]).strip(" ,.;:()")
                if len(joined) >= len(key):
                    break
            if found:
                break
        if not found:
            words = re.sub(r"\([^)]*\)", " ", cand or "").split()
            initials = "".join(w[0].lower() for w in words
                               if w.lower() not in _MINOR_WORDS and w[0].isalnum())
            if initials == key and len(key) >= 3:
                found = slug.upper()
        if found:
            votes[found] += 1
    return votes.most_common(1)[0][0] if votes else None


def _workday_entities(company: dict, jobs: int = 3) -> list[str]:
    """Hiring entities on a few of the board's newest job pages."""
    base = f"https://{company['tenant']}.{company['wd']}.myworkdayjobs.com"
    cxs = f"{base}/wday/cxs/{company['tenant']}/{company['site']}"
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    try:
        listing = _SESSION.post(f"{cxs}/jobs", json={"appliedFacets": {}, "limit": jobs,
                                                     "offset": 0, "searchText": ""},
                                headers=headers, timeout=20).json()
        out = []
        for p in (listing.get("jobPostings") or [])[:jobs]:
            detail = _SESSION.get(f"{cxs}{p['externalPath']}", headers=headers,
                                  timeout=20).json()
            out.append(((detail.get("hiringOrganization") or {}).get("name") or "").strip())
        return out
    except (requests.RequestException, ValueError, KeyError):
        return []


def _pretty_slug(slug: str) -> str | None:
    if not any(sep in slug for sep in "-_"):
        return None     # "abcsupply": no safe way to split words
    return " ".join(w.capitalize() for w in slug.replace("_", "-").split("-") if w)


def main() -> int:
    from .config import setup_logging
    setup_logging()
    from .config import load_settings
    settings = load_settings()
    log.info("Collecting names from curated lists ...")
    curated = _curated_names(settings)

    for spec in ats_specs.ATS_SPECS:
        companies, _ = ats_specs.load_existing(spec)
        todo = [c for c in companies if is_slug_name(spec.name, c)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            api = list(pool.map(lambda c: _api_name(spec.name, c), todo))
        filled = 0
        for c, from_api in zip(todo, api):
            bid = board_id(spec.name, c)
            if spec.name in ("successfactors", "workday"):    # curated names first
                name = curated.get((spec.name, bid)) or from_api
            else:
                name = from_api or curated.get((spec.name, bid)) or _pretty_slug(bid)
            # Exact: a case-only fix ("salesforce" -> "Salesforce") is the point.
            if not name or name == (c.get("name") or ""):
                continue
            old = c.get("name") or bid
            c["aliases"] = sorted(set(c.get("aliases", [])) | {old})
            c["name"] = name
            filled += 1
        ats_specs.save_config(spec, companies)
        log.info(f"  {spec.name}: named {filled} of {len(todo)} slug-named boards "
              f"({len(companies)} total)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
