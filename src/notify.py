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
    return {
        "title": job["title"][:256],
        "url": job["apply_url"],
        "color": _ROLE_COLORS.get(job["role_type"], 0x95A5A6),
        "fields": [
            {"name": "Company", "value": job["company"] or "—", "inline": True},
            {"name": "Role", "value": job["role_type"], "inline": True},
            {"name": "Location", "value": job["location"] or "—", "inline": True},
        ],
    }


def post_new_jobs(new_jobs: list[dict]) -> None:
    if not new_jobs:
        print("No new jobs to announce.")
        return

    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        print(f"DISCORD_WEBHOOK_URL not set — would announce {len(new_jobs)} jobs:")
        for j in new_jobs:
            print(f"  [{j['role_type']}] {j['company']}: {j['title']}")
        return

    for i in range(0, len(new_jobs), _MAX_EMBEDS):
        batch = new_jobs[i:i + _MAX_EMBEDS]
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

    print(f"Announced {len(new_jobs)} new jobs to Discord.")
