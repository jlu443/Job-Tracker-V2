"""Personal filters: skip jobs that can't work for you.

Settings (config/settings.yaml, `profile:`), all optional:

    enabled: true
    grad_year: 2027            # hide jobs whose description names only other years
    needs_sponsorship: true    # hide "no sponsorship" / US-citizens-only jobs
    us_citizen: false          # hide citizenship-required and clearance-required jobs
    locations: [CA, Seattle, New York, Remote]   # any match; empty = anywhere

Applied to Discord announcements, the Sheet's Today / This Week tabs (which
list announced jobs) and the PhD & Research tab. All Open stays unfiltered.
Unknown values never filter: a job whose description wasn't read, or says
nothing, is kept.
"""

from __future__ import annotations

import re


def _location_match(location: str, wanted: list[str]) -> bool:
    loc = (location or "").lower()
    if not loc:
        return True                       # unknown location: keep
    parts = {p.strip().lower() for p in re.split(r"[,;/|]", location)}
    for w in wanted:
        w_low = w.strip().lower()
        if len(w_low) == 2 and w_low.isalpha():          # state code: whole part
            if w_low in parts or re.search(rf"\b{w_low}\b(?=\s*(?:,|;|$|\s+us\b))", loc):
                return True
        elif re.search(rf"\b{re.escape(w_low)}\b", loc):
            return True
    return False


def reasons_to_skip(job: dict, profile: dict) -> list[str]:
    """Why a job doesn't fit the profile; [] when it does (or profile is off)."""
    if not profile or not profile.get("enabled"):
        return []
    why = []
    if profile.get("needs_sponsorship") and (job.get("sponsorship") == "no"
                                             or job.get("citizenship") == "required"):
        why.append("no visa sponsorship")
    if profile.get("us_citizen") is False and (job.get("citizenship") == "required"
                                               or job.get("clearance") == "yes"):
        why.append("US citizens / clearance only")
    year = str(profile.get("grad_year") or "")
    years = [y.strip() for y in (job.get("grad_year") or "").split(",") if y.strip()]
    if year and years and year not in years:
        why.append(f"grad year {', '.join(years)}")
    wanted = profile.get("locations") or []
    if wanted and not _location_match(job.get("location") or "", wanted):
        why.append("location")
    return why


def fits(job: dict, settings: dict) -> bool:
    return not reasons_to_skip(job, settings.get("profile") or {})
