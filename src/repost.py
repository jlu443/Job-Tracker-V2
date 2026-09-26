"""Tell genuinely new postings apart from reposts and old listings.

Signals, strongest first. Each sets job["repost"] to a reason code and
job["repost_detail"] to a human-readable note:

  relisted  The same company/title was listed on a first-party board before,
            closed, and re-opened under a new id — often a req id rotated to
            bump it to the top of search results. repost_of points at the
            prior listing.
  linkedin  LinkedIn's public API hides its "Reposted" label, but its job ids
            are allocated sequentially (~460k/day). A listing whose id is far
            older than its displayed date was bumped, not created, recently.
  stale     The source's own post date is much older than when we first saw
            it: an old listing re-surfaced (or a newly discovered board).
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import date, timedelta
from statistics import median

from . import dates, dedupe

# Measured from public search results: id 4253075532 was listed 2025-06-16,
# id 4471636508 on 2026-09-26 → ~468k ids/day. Only relative offsets matter,
# so a ±20% error moves a 30-day threshold by ~6 days.
LINKEDIN_IDS_PER_DAY = 460_000

_LI_ID = re.compile(r"linkedin\.com/jobs/view/(?:[\w-]*-)?(\d{8,})")


def linkedin_numeric_id(url: str) -> int | None:
    m = _LI_ID.search(url or "")
    return int(m.group(1)) if m else None


def linkedin_frontier(observations: list[tuple[int, str]]) -> tuple[int, date] | None:
    """(id, list date) of the newest-created listings in a batch.

    The highest ids are by construction the most recently created jobs, so
    their list dates are real creation dates, never repost dates. Using the
    median over the top slice guards against a single odd card.
    """
    obs = sorted((i, d) for i, d in observations if i and d)
    if len(obs) < 5:
        return None
    top = obs[-max(5, len(obs) // 20):]
    return (int(median(i for i, _ in top)),
            date.fromisoformat(sorted(d for _, d in top)[len(top) // 2][:10]))


def linkedin_repost_age(li_id: int, list_date: str,
                        frontier: tuple[int, date] | None,
                        ids_per_day: int = LINKEDIN_IDS_PER_DAY) -> int | None:
    """Days between when the id was created (estimated) and its list date."""
    if not (li_id and list_date and frontier):
        return None
    frontier_id, frontier_date = frontier
    created = frontier_date - timedelta(days=(frontier_id - li_id) / ids_per_day)
    return (date.fromisoformat(list_date[:10]) - created).days


def is_bump(old: str, new: str, min_days: int = 3) -> bool:
    """Same job id, list date moved forward: the listing was re-dated.

    Ignores dates ~30 days back: Workday's open-ended "Posted 30+ Days Ago"
    resolves to today-30, which drifts forward daily without a real repost.
    """
    if not old or not new or new <= old:
        return False
    gap = (date.fromisoformat(new[:10]) - date.fromisoformat(old[:10])).days
    age = dates.age_days(new)
    return gap >= min_days and age is not None and age < 28


def triage(new_jobs: list[dict], history: dict, new_boards: set[tuple[str, str]],
           board_of, aggregator_sources: set[str], recent_days: int = 60
           ) -> tuple[list[dict], dict[str, tuple[str, str]], Counter]:
    """Decide which of this run's new rows are news.

    Returns (candidates, relisted_from, skipped-reason counts). Skipped:
      bootstrap        first scrape of a newly added board/source — its whole
                       backlog arrives at once and would flood the channel
      same_run_twin    one role listed per city; merged into the first copy's
                       location instead of announced N times
      already_tracked  the role is live under another id, or an aggregator
                       listed it recently (we announced it then; aggregator
                       rows were historically expired unreliably)
    A role whose earlier first-party listing had closed is a candidate, noted
    in relisted_from so annotate() can label it.
    """
    cutoff = (dates.today() - timedelta(days=recent_days)).isoformat()
    skipped: Counter = Counter()
    first_of_role: dict[str, dict] = {}
    candidates: list[dict] = []
    relisted_from: dict[str, tuple[str, str]] = {}

    for j in new_jobs:
        if board_of(j) in new_boards:
            skipped["bootstrap"] += 1
            continue
        rk = dedupe.role_key(j.get("job_key", ""))
        twin = first_of_role.get(rk) if rk else None
        if twin is not None:
            loc = j.get("location") or ""
            if loc and loc not in twin["location"]:
                twin["location"] = f"{twin['location']}; {loc}" if twin["location"] else loc
            skipped["same_run_twin"] += 1
            continue
        h = history.get(rk) if rk else None
        if h and (h.active or (h.source in aggregator_sources and h.last_seen >= cutoff)):
            skipped["already_tracked"] += 1
            continue
        if h:
            relisted_from[j["job_id"]] = (h.job_id, h.last_seen)
        if rk:
            first_of_role[rk] = j
        candidates.append(j)
    return candidates, relisted_from, skipped


def annotate(new_jobs: list[dict], relisted_from: dict[str, tuple[str, str]],
             li_frontier: tuple[int, date] | None, settings: dict) -> None:
    """Set repost/repost_of/repost_detail on each new job dict in place.

    relisted_from maps a new job_id → (job_id, last_seen) of the closed
    first-party listing of the same role it re-opens.
    """
    cfg = settings.get("reposts", {})
    li_days = cfg.get("linkedin_min_age_days", 30)
    stale_days = cfg.get("stale_after_days", 30)

    for j in new_jobs:
        j.setdefault("repost", "")
        j.setdefault("repost_of", "")
        j.setdefault("repost_detail", "")

        prior = relisted_from.get(j["job_id"])
        if prior:
            j.update(repost="relisted", repost_of=prior[0],
                     repost_detail=f"same role was listed until {prior[1][:10]}")
            continue

        if j.get("source") == "linkedin":
            age = linkedin_repost_age(linkedin_numeric_id(j.get("apply_url", "")),
                                      j.get("posted_on", ""), li_frontier)
            if age is not None and age >= li_days:
                j.update(repost="linkedin",
                         repost_detail=f"LinkedIn listing ~{age} days older than "
                                       "its post date (bumped/reposted)")
                continue

        age = dates.age_days(j.get("posted_on", ""))
        if age is not None and age >= stale_days:
            j.update(repost="stale", repost_detail=f"posted {j['posted_on']}")
