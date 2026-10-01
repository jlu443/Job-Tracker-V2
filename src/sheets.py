"""Publish the tracker to a Google Sheet as rebuilt tabs, via an Apps Script
webhook (docs/apps_script.gs).

    Today            jobs announced in the last 24 hours
    This Week        the same, last 7 days
    All Open         every stored intern/new-grad job (the DB's 60-day window),
                     refreshed about once a day
    PhD & Research   open PhD / research-track internships, ranked (src/phd.py)
    My Applications  owned by the sheet: rows the user gave a Status

Tabs are rebuilt from the DB each run, so they can never drift from it; the
Apps Script re-applies the user's Status marks by job_id. Set
GOOGLE_SHEETS_WEBHOOK_URL to enable; without it this is a no-op.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone

import requests

from . import db, phd

COLUMNS = ["Apply", "Company", "Title", "Role", "Category", "Location", "Posted",
           "First seen", "Sponsorship", "Clearance", "Grad year", "Applicants",
           "Repost", "Source", "Listing", "job_id"]
_REPOST = {"relisted": "re-listed", "linkedin": "LinkedIn repost", "stale": "old posting",
           "bumped": "re-dated"}
_SCRIPT_VERSION = 2


def _apply_cell(url: str) -> str:
    return f'=HYPERLINK("{url.replace(chr(34), "%22")}", "Apply")' if url else ""


def _row(j: dict) -> list:
    return [
        _apply_cell(j["apply_url"]), j["company"], j["title"], j["role_type"],
        j.get("category") or "", j.get("location") or "", j.get("posted_on") or "",
        (j.get("first_seen") or "")[:16].replace("T", " "),
        j.get("sponsorship") or "", "yes" if j.get("clearance") else "",
        j.get("grad_year") or "", j.get("applicants") or "",
        _REPOST.get(j.get("repost") or "", j.get("repost") or ""), j["source"],
        "open" if j.get("status") == "active" else "closed", j["job_id"],
    ]


def build_tabs(conn: sqlite3.Connection, include_all: bool,
               now: datetime | None = None) -> dict[str, list[list]]:
    now = now or datetime.now(timezone.utc)
    conn.row_factory = sqlite3.Row
    jobs = [dict(r) for r in conn.execute(
        "SELECT * FROM jobs WHERE role_type IN ('intern', 'new_grad') "
        "ORDER BY announced_at DESC, first_seen DESC, role_type")]
    # Today / This Week list what was announced, not everything first seen:
    # a newly added board's silently stored backlog isn't news.
    announced = [j for j in jobs if j["status"] == "active" and j.get("announced_at")]
    since = lambda h: (now - timedelta(hours=h)).isoformat(timespec="seconds")
    tabs = {
        "Today": [_row(j) for j in announced if j["announced_at"] >= since(24)],
        "This Week": [_row(j) for j in announced if j["announced_at"] >= since(24 * 7)],
    }
    if include_all:
        # Open listings only: closed rows stay in the DB for repost history,
        # but would bury the open ones (57k rows vs ~25k open on 2026-10-01).
        tabs["All Open"] = [_row(j) for j in jobs if j["status"] == "active"]
    return tabs


PHD_TAB = "PhD & Research"
PHD_COLUMNS = ["Score", "Why", "Track"] + COLUMNS
_TRACK = {"phd": "PhD", "research_ms": "Research (MS/PhD)"}


def build_phd_tab(conn: sqlite3.Connection, settings: dict) -> list[list]:
    """Ranked PhD / research-track internships (src/phd.py), best first."""
    return [[j["score"], j["why"], _TRACK.get(j["research_track"], "")] + _row(j)
            for j in phd.ranked(conn, settings)]


def _post(webhook: str, payload: dict) -> dict:
    resp = requests.post(webhook, json=payload, timeout=300)
    resp.raise_for_status()
    try:
        return resp.json()
    except ValueError:
        return {}


def publish(conn: sqlite3.Connection, settings: dict) -> None:
    webhook = os.environ.get("GOOGLE_SHEETS_WEBHOOK_URL")
    if not webhook:
        print("GOOGLE_SHEETS_WEBHOOK_URL not set — skipping Google Sheets.")
        return
    try:
        version = _post(webhook, {"action": "ping"}).get("version")
    except requests.RequestException as exc:
        print(f"  ! Google Sheets unreachable: {exc}")
        return
    if version != _SCRIPT_VERSION:
        # An old deployment would append these rows into the wrong layout.
        print("  ! Google Sheets script is out of date: paste docs/apps_script.gs into "
              "the sheet's Apps Script and deploy a new version. Skipping the sheet.")
        return

    refresh_hours = settings.get("sheets", {}).get("all_open_refresh_hours", 24)
    last = db._meta_get(conn, "sheets_all_open_at_v2")
    include_all = (not last or datetime.fromisoformat(last)
                   < datetime.now(timezone.utc) - timedelta(hours=refresh_hours))

    tabs = [(tab, COLUMNS, rows) for tab, rows in build_tabs(conn, include_all).items()]
    if settings.get("phd", {}).get("enabled", True):
        tabs.append((PHD_TAB, PHD_COLUMNS, build_phd_tab(conn, settings)))

    for tab, columns, rows in tabs:
        try:
            result = _post(webhook, {"action": "replace_tab", "tab": tab,
                                     "columns": columns, "rows": rows})
        except requests.RequestException as exc:
            print(f"  ! Google Sheets tab {tab!r} failed: {exc}")
            continue
        if not result.get("ok"):
            print(f"  ! Google Sheets tab {tab!r}: {result.get('error')}")
            continue
        print(f"Sheet tab {tab!r}: {len(rows)} rows")
        if tab == "All Open":
            db._meta_set(conn, "sheets_all_open_at_v2", db._now())
            conn.commit()
