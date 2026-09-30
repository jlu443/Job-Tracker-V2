"""The one posting shape every source normalizes into."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class JobPosting:
    job_id: str          # globally unique, source-prefixed (gh_, lv_, wd_<tenant>_, ...)
    company: str
    title: str
    apply_url: str
    location: str
    posted_on: str       # ISO date (YYYY-MM-DD) or "" when unknown
    source: str
    description: str = ""     # only when the source hands it over for free
    direct_url: str = ""      # aggregator postings: the employer's own apply link
    category: str = ""        # job function, when the source already knows it
    sponsorship: str = ""     # 'yes' | 'no' | '' when the source already knows it
    role_hint: str = ""       # role_type asserted by a curated source; beats the title
    research_track: str = ""  # 'phd' | 'research_ms' | '' (src/phd.py)

    def __post_init__(self):
        # Sources embed newlines/tabs in titles ("Intern\nTorrance, CA").
        for name in ("company", "title", "location"):
            object.__setattr__(self, name, " ".join((getattr(self, name) or "").split()))
