"""Append newly-discovered jobs to a Google Sheet via an Apps Script webhook.

Setup (one time):
  1. Open your Google Sheet → Extensions → Apps Script.
  2. Paste the script in docs/apps_script.gs and Deploy → New deployment →
     type "Web app" → execute as "Me", access "Anyone".
  3. Copy the /exec URL and set it as GOOGLE_SHEETS_WEBHOOK_URL (env var / secret).

If the env var is unset this is a no-op, so local runs without it are fine.
"""

from __future__ import annotations

import os

import requests

# The columns we send, in order. The Apps Script writes these as a header row
# once and appends a row per job. (Sheets created before the enrichment
# columns existed keep their old header; add the three names manually.)
_COLUMNS = ["first_seen", "company", "title", "role_type", "location",
            "posted_on", "source", "apply_url", "job_id",
            "sponsorship", "clearance", "grad_year"]


def _row(job: dict) -> list:
    return [job.get(c, "") for c in _COLUMNS]


def post_new_jobs(new_jobs: list[dict]) -> None:
    if not new_jobs:
        return

    webhook = os.environ.get("GOOGLE_SHEETS_WEBHOOK_URL")
    if not webhook:
        print("GOOGLE_SHEETS_WEBHOOK_URL not set — skipping Google Sheets.")
        return

    payload = {"columns": _COLUMNS, "rows": [_row(j) for j in new_jobs]}
    try:
        resp = requests.post(webhook, json=payload, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"  ! Google Sheets post failed: {exc}")
        return

    print(f"Appended {len(new_jobs)} jobs to Google Sheet.")
