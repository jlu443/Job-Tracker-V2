"""Post newly-discovered jobs to a Discord channel via webhook.

Only `first_seen == this run` jobs are passed in, so each job is announced once.
Set DISCORD_WEBHOOK_URL to enable; if unset, this is a no-op (prints instead).
"""

from __future__ import annotations

import os
import time

import requests

from . import geo

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
        {"name": "Source", "value": job.get("source", "—"), "inline": True},
        {"name": "Location", "value": job["location"] or "—", "inline": True},
    ]
    if job.get("posted_on"):
        fields.append({"name": "Posted", "value": job["posted_on"], "inline": True})
    # Flags parsed from the job description by enrich.py.
    sponsorship = job.get("sponsorship")
    if sponsorship == "no":
        fields.append({"name": "Sponsorship", "value": "❌ Not offered", "inline": True})
    elif sponsorship == "yes":
        fields.append({"name": "Sponsorship", "value": "✅ Offered", "inline": True})
    if job.get("clearance"):
        fields.append({"name": "Clearance", "value": "🔒 Required", "inline": True})
    if job.get("grad_year"):
        fields.append({"name": "Grad year", "value": job["grad_year"], "inline": True})
    if job.get("applicants"):
        fields.append({"name": "Applicants", "value": job["applicants"], "inline": True})
    if job.get("repost"):
        fields.append({"name": _REPOST_LABEL.get(job["repost"], "♻️ Repost"),
                       "value": job.get("repost_detail") or "—", "inline": False})
    return {
        "title": job["title"][:256],
        "url": job["apply_url"],
        "color": _ROLE_COLORS.get(job["role_type"], 0x95A5A6),
        "fields": fields,
    }


_ANNOUNCE_ROLES = {"intern", "new_grad"}
_REPOST_LABEL = {"relisted": "♻️ Re-listed", "linkedin": "♻️ LinkedIn repost",
                 "stale": "🕰️ Old posting"}


def announceable(jobs: list[dict], settings: dict) -> list[dict]:
    """Jobs worth a notification: entry-level, US, in a wanted job function."""
    categories = set(settings.get("announce_categories") or ())
    return [j for j in jobs
            if j.get("role_type") in _ANNOUNCE_ROLES
            and geo.is_us(j.get("location", ""))
            and not geo.title_names_foreign_place(j.get("title", ""))
            and (not categories or j.get("category") in categories)]


def post_new_jobs(jobs_to_post: list[dict]) -> None:
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
