"""PhD & research-track internships: detection and ranking.

A separate component on top of the main pipeline's data. It never changes
what's stored or announced; it reads jobs.db and produces the Sheet's
"PhD & Research" tab.

Track (stored per job as research_track):
  phd          aimed at PhD students: says PhD / doctoral in the title, or the
               description asks for current PhD students / candidates
  research_ms  research-track but open to MS students: a research-type title
               (Research Intern, Applied Scientist, Research Engineer, ...) or
               a description asking for MS or PhD students

Rank (0-100), two parts, shown with a plain-English "why":
  research strength (up to 60)   the org's research tier (config/research_orgs.yaml)
                                 and how research-heavy the title is
  freshness & competition (40)   days since posted, applicant count (LinkedIn),
                                 reposts penalized
"""

from __future__ import annotations

import os
import re
import sqlite3

import yaml

from . import dates, dedupe, geo

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ORGS_FILE = os.path.join(_ROOT, "config", "research_orgs.yaml")

_PHD_TITLE = re.compile(r"\bph\.?\s?d\b|\bdoctoral\b|\bdoctorate\b", re.I)
_PHD_DESC = re.compile(
    r"\b(?:current(?:ly)?|enrolled|pursuing|candidate|student)s?\b[^.]{0,60}"
    r"\b(?:ph\.?\s?d|doctoral|doctorate)\b|\bph\.?\s?d\.?\s+(?:student|candidate)s?\b", re.I)
_RESEARCH_TITLE = re.compile(
    r"\bresearch(?:er)?\b|\bapplied\s+scientist\b|\bresearch\s+(?:scientist|engineer)\b|"
    r"\bscientist\b|\br\s*&\s*d\b", re.I)
_MS_OR_PHD = re.compile(
    r"\b(?:m\.?s\.?|master'?s|msc)\b\s*(?:/|or|and|,)\s*(?:ph\.?\s?d)\b|"
    r"\bph\.?\s?d\.?\s*(?:/|or|and|,)\s*(?:m\.?s\.?|master'?s|msc)\b|"
    r"\bgraduate students?\b", re.I)


def track(title: str, description: str = "") -> str:
    """'phd' | 'research_ms' | '' for an internship's title and description."""
    if _PHD_TITLE.search(title or ""):
        return "phd"
    desc = description or ""
    if _PHD_DESC.search(desc) and not _MS_OR_PHD.search(desc):
        return "phd"
    if _RESEARCH_TITLE.search(title or "") or _MS_OR_PHD.search(desc):
        return "research_ms"
    return ""


def load_orgs(path: str = _ORGS_FILE) -> dict[str, int]:
    """Normalized org name → tier (1 strongest)."""
    if not os.path.exists(path):
        return {}
    data = yaml.safe_load(open(path, encoding="utf-8")) or {}
    out = {}
    for tier_name, names in (data.get("tiers") or {}).items():
        tier = int(str(tier_name).lstrip("tier"))
        for name in names or []:
            out[dedupe._norm_company(name)] = tier
    return out


def org_tier(company: str, orgs: dict[str, int]) -> int | None:
    """Tier for a company, matching whole-word prefixes: 'google' matches
    'Google LLC' and 'Google DeepMind' but 'meta' doesn't match 'metamaterial'."""
    norm = dedupe._norm_company(company or "")
    if not norm:
        return None
    best = None
    for name, tier in orgs.items():
        if norm == name or norm.startswith(name + " "):
            best = tier if best is None else min(best, tier)
    return best


_TIER_POINTS = {1: 35, 2: 22, 3: 12}


def score(job: dict, orgs: dict[str, int]) -> tuple[int, str]:
    """(0-100 score, why) for one PhD/research internship."""
    title = job.get("title") or ""
    why = []

    research = 0
    tier = org_tier(job.get("company") or "", orgs)
    if tier:
        research += _TIER_POINTS.get(tier, 0)
        why.append(f"tier-{tier} research org")
    if re.search(r"\bresearch\s+scientist|\bresearcher\b|\bresearch\s+intern", title, re.I):
        research += 15
        why.append("research scientist role")
    elif re.search(r"\bapplied\s+scientist|\bresearch\s+engineer|\bscientist\b", title, re.I):
        research += 10
        why.append("applied/research engineering")
    if job.get("research_track") == "phd":
        research += 10
        why.append("PhD-targeted")
    research = min(research, 60)

    fresh = 0
    age = dates.age_days(job.get("posted_on") or "")
    if age is not None:
        fresh += 22 if age <= 2 else 16 if age <= 7 else 9 if age <= 14 else 3 if age <= 30 else 0
        why.append("posted today" if age <= 0 else f"{age}d old")
    applicants = (job.get("applicants") or "").lower()
    count = re.search(r"\d+", applicants.replace(",", ""))
    if "first" in applicants or (count and int(count.group()) < 25):
        fresh += 18
        why.append("<25 applicants")
    elif count and int(count.group()) < 100:
        fresh += 10
        why.append(f"{count.group()} applicants")
    elif count:
        why.append(f"{count.group()}+ applicants")
    else:
        fresh += 7                      # unknown: most sources don't report it
    if job.get("repost"):
        fresh -= 6
        why.append("repost")
    fresh = max(0, min(fresh, 40))

    return research + fresh, " · ".join(why)


def ranked(conn: sqlite3.Connection, settings: dict, orgs: dict[str, int] | None = None) -> list[dict]:
    """Open PhD/research-track internships in the US, best first."""
    orgs = load_orgs() if orgs is None else orgs
    categories = set(settings.get("announce_categories") or ())
    conn.row_factory = sqlite3.Row
    jobs = [dict(r) for r in conn.execute(
        "SELECT * FROM jobs WHERE status = 'active' AND role_type = 'intern' "
        "AND research_track IN ('phd', 'research_ms')")]
    out = []
    for j in jobs:
        if not geo.is_us(j.get("location") or "") or geo.title_names_foreign_place(j["title"]):
            continue
        if categories and j.get("category") not in categories:
            continue
        j["score"], j["why"] = score(j, orgs)
        out.append(j)
    out.sort(key=lambda j: j.get("posted_on") or "", reverse=True)   # newest first...
    out.sort(key=lambda j: -j["score"])                               # ...within equal scores
    # One row per role: the same internship listed per city (or per source)
    # collapses onto its best-scoring copy, with the locations merged.
    best: dict[str, dict] = {}
    for j in out:
        key = dedupe.role_key(j.get("job_key") or "") or j["job_id"]
        if key not in best:
            best[key] = j
        elif j.get("location") and j["location"] not in (best[key].get("location") or ""):
            best[key]["location"] = "; ".join(filter(None, [best[key].get("location"),
                                                            j["location"]]))
    return list(best.values())
