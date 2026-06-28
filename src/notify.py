"""Post newly-discovered jobs to a Discord channel via webhook.

Only `first_seen == this run` jobs are passed in, so each job is announced once.
Set DISCORD_WEBHOOK_URL to enable; if unset, this is a no-op (prints instead).
"""

from __future__ import annotations

import os
import time

import requests

# Discord allows max 10 embeds per message.
_MAX_EMBEDS = 10

_ROLE_COLORS = {
    "intern": 0x2ECC71,    # green
    "new_grad": 0x3498DB,  # blue
    "mid": 0xF1C40F,       # yellow
    "senior": 0xE74C3C,    # red
}


def _embed(job: dict) -> dict:
    fields = [
        {"name": "Company", "value": job["company"] or "—", "inline": True},
        {"name": "Role", "value": job["role_type"], "inline": True},
        {"name": "Location", "value": job["location"] or "—", "inline": True},
    ]
    if job.get("posted_on"):
        fields.append({"name": "Posted", "value": job["posted_on"], "inline": True})
    return {
        "title": job["title"][:256],
        "url": job["apply_url"],
        "color": _ROLE_COLORS.get(job["role_type"], 0x95A5A6),
        "fields": fields,
    }


_ANNOUNCE_ROLES = {"intern", "new_grad"}

# US state abbreviations and country keywords used to detect non-US locations.
_US_STATES = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA",
    "KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ",
    "NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT",
    "VA","WA","WV","WI","WY","DC",
}
_NON_US_KEYWORDS = {
    "canada","uk","united kingdom","germany","france","india","china","japan",
    "australia","singapore","ireland","netherlands","spain","italy","poland",
    "brazil","mexico","israel","sweden","switzerland","remote - non us",
}


def _is_us_location(location: str) -> bool:
    """Return True if the location appears to be in the US or is unspecified."""
    if not location:
        return True  # no location info — let it through
    loc = location.lower()
    # Reject if a known non-US keyword appears
    for kw in _NON_US_KEYWORDS:
        if kw in loc:
            return False
    # Accept if a US state abbreviation appears (e.g. "Austin, TX" or "New York, NY")
    parts = [p.strip().upper() for p in loc.replace(",", " ").split()]
    if any(p in _US_STATES for p in parts):
        return True
    # Accept common US-only strings
    if "united states" in loc or "usa" in loc or "u.s." in loc or "remote" in loc:
        return True
    # Ambiguous — let it through rather than silently drop
    return True


def post_new_jobs(new_jobs: list[dict]) -> None:
    jobs_to_post = [
        j for j in new_jobs
        if j.get("role_type") in _ANNOUNCE_ROLES and _is_us_location(j.get("location", ""))
    ]
    skipped = len(new_jobs) - len(jobs_to_post)
    if skipped:
        print(f"Filtered out {skipped} non-intern/new_grad or non-US jobs from announcement.")
    if not jobs_to_post:
        print("No new intern/new_grad US jobs to announce.")
        return

    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        print(f"DISCORD_WEBHOOK_URL not set — would announce {len(jobs_to_post)} jobs:")
        for j in jobs_to_post:
            print(f"  [{j['role_type']}] {j['company']}: {j['title']}")
        return

    for i in range(0, len(jobs_to_post), _MAX_EMBEDS):
        batch = jobs_to_post[i:i + _MAX_EMBEDS]
        payload = {"embeds": [_embed(j) for j in batch]}
        try:
            resp = requests.post(webhook, json=payload, timeout=30)
            # Discord returns 429 with retry_after when rate limited.
            if resp.status_code == 429:
                retry = resp.json().get("retry_after", 1)
                time.sleep(float(retry) + 0.5)
                requests.post(webhook, json=payload, timeout=30).raise_for_status()
            else:
                resp.raise_for_status()
        except requests.RequestException as exc:
            print(f"  ! Discord post failed: {exc}")
        time.sleep(0.5)  # stay under the webhook rate limit

    print(f"Announced {len(jobs_to_post)} new jobs to Discord.")
