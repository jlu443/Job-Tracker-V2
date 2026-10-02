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

from . import db, dedupe, h1b, phd

COLUMNS = ["Apply", "Company", "Title", "Role", "Category", "Location", "Posted",
           "Days ago", "First seen", "Visa sponsorship", "US citizenship", "Clearance",
           "Pay", "Grad year", "Applicants", "Repost", "Source", "Listing", "job_id"]

# Live "days since posted", computed by Sheets from the Posted cell to its
# left (any tab layout, any row), so it stays right between rebuilds.
_DAYS_AGO = ('=LET(p, INDIRECT("RC[-1]", FALSE), '
             'IF(p = "", "", TODAY() - IFERROR(DATEVALUE(p), p)))')

# Blank = description not read yet; "Not mentioned" = read, says nothing.
_SPONSORSHIP = {"yes": "Offered", "no": "Not offered"}
_CLEARANCE = {"yes": "Required", "none": "Not required"}


def _flag(value: str, labels: dict, checked: bool) -> str:
    return labels.get(value, value) if value else ("Not mentioned" if checked else "")
_REPOST = {"relisted": "re-listed", "linkedin": "LinkedIn repost", "stale": "old posting",
           "bumped": "re-dated"}
_SCRIPT_VERSION = 3


def _apply_cell(url: str) -> str:
    return f'=HYPERLINK("{url.replace(chr(34), "%22")}", "Apply")' if url else ""


def _row(j: dict) -> list:
    checked = bool(j.get("checked_at"))
    return [
        # The employer's own page when an aggregator revealed it.
        _apply_cell(j.get("direct_url") or j["apply_url"]), j["company"], j["title"],
        j["role_type"], j.get("category") or "", j.get("location") or "",
        j.get("posted_on") or "", _DAYS_AGO,
        (j.get("first_seen") or "")[:16].replace("T", " "),
        _flag(j.get("sponsorship") or "", _SPONSORSHIP, checked),
        _flag(j.get("citizenship") or "", {"required": "Required"}, checked),
        _flag(j.get("clearance") or "", _CLEARANCE, checked),
        j.get("pay") or "", j.get("grad_year") or "", j.get("applicants") or "",
        _REPOST.get(j.get("repost") or "", j.get("repost") or ""), j["source"],
        "open" if j.get("status") == "active" else "closed", j["job_id"],
    ]


def build_tabs(conn: sqlite3.Connection, include_all: bool,
               now: datetime | None = None) -> dict[str, list[list]]:
    now = now or datetime.now(timezone.utc)
    conn.row_factory = sqlite3.Row
    jobs = [dict(r) for r in conn.execute(
        "SELECT * FROM jobs WHERE role_type IN ('intern', 'new_grad')")]
    # Every tab: most recently posted at the top.
    jobs.sort(key=phd.posted_sort_key, reverse=True)
    # Today / This Week list what was announced, not everything first seen:
    # a newly added board's silently stored backlog isn't news.
    announced = [j for j in jobs if j["status"] == "active" and j.get("announced_at")]
    since = lambda h: (now - timedelta(hours=h)).isoformat(timespec="seconds")
    # One row per role on the short lists: a job posted in 12 cities is one
    # opening to apply to, with its locations merged.
    collapse = lambda js: [_row(j) for j in dedupe.collapse_roles(js)]
    tabs = {
        "Today": collapse([j for j in announced if j["announced_at"] >= since(24)]),
        "This Week": collapse([j for j in announced if j["announced_at"] >= since(24 * 7)]),
    }
    if include_all:
        # Open listings only: closed rows stay in the DB for repost history,
        # but would bury the open ones (57k rows vs ~25k open on 2026-10-01).
        tabs["All Open"] = [_row(j) for j in jobs if j["status"] == "active"]
    return tabs


PHD_TAB = "PhD & Research"
PHD_COLUMNS = ["Track", "Visa outlook", "H-1B history", "OPT/CPT"] + COLUMNS
_TRACK = {"phd": "PhD", "research_ms": "Research (MS/PhD)"}
_OPT = {"yes": "Accepted (per posting)", "no": "Not accepted (per posting)"}


def visa_outlook(job: dict, h1b_approvals: int | None) -> str:
    """One-glance verdict for an F-1 student. What the posting says always
    wins; otherwise the company's H-1B record (USCIS) is the best predictor,
    since companies that file H-1Bs routinely hire on OPT / STEM OPT."""
    if job.get("citizenship") == "required" or job.get("clearance") == "yes":
        return "🇺🇸 US citizens / clearance only"
    if job.get("sponsorship") == "no" or job.get("opt_cpt") == "no":
        return "❌ Posting rules out sponsorship"
    if job.get("sponsorship") == "yes" or job.get("opt_cpt") == "yes":
        return "✅ Posting offers sponsorship / OPT"
    if h1b_approvals is None:
        return "⚪ Unknown"
    if h1b_approvals >= 50:
        return "🟢 Likely: sponsors H-1Bs regularly"
    if h1b_approvals >= 1:
        return "🟡 Some H-1B history"
    return "⚪ No H-1B record"


def build_phd_tab(conn: sqlite3.Connection, settings: dict) -> list[list]:
    """Open PhD / research-track internships (src/phd.py), newest-posted
    first, with the company's H-1B record and the posting's OPT/CPT stance."""
    rows = []
    for j in phd.open_internships(conn, settings):
        n = h1b.approvals(j["company"])
        rows.append([_TRACK.get(j["research_track"], ""), visa_outlook(j, n),
                     h1b.label(j["company"]), _OPT.get(j.get("opt_cpt") or "", "")]
                    + _row(j))
    return rows


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
    if not isinstance(version, int) or version < 2:
        # A v1 deployment would append these rows into the wrong layout.
        print("  ! Google Sheets script is out of date: paste docs/apps_script.gs into "
              "the sheet's Apps Script and deploy a new version. Skipping the sheet.")
        return
    if version < _SCRIPT_VERSION:
        print(f"  ! Google Sheets script is v{version}; deploy docs/apps_script.gs "
              f"(v{_SCRIPT_VERSION}) to get closed-listing marks in My Applications.")

    refresh_hours = settings.get("sheets", {}).get("all_open_refresh_hours", 24)
    last = db._meta_get(conn, "sheets_all_open_at_v3")
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
            db._meta_set(conn, "sheets_all_open_at_v3", db._now())
            conn.commit()

    if version >= 3:
        # Jobs taken down, or aged out of the DB's window, get marked closed
        # in My Applications so a saved application never silently goes stale.
        try:
            saved = _post(webhook, {"action": "list_applications"}).get("ids") or []
            closed = closed_among(conn, saved)
            result = _post(webhook, {"action": "listing_status", "closed": closed})
            print(f"My Applications: {result.get('marked', 0)} saved jobs marked closed")
        except requests.RequestException as exc:
            print(f"  ! Google Sheets listing status failed: {exc}")


def closed_among(conn: sqlite3.Connection, job_ids: list[str]) -> list[str]:
    """The given saved jobs that are no longer open: taken down, expired out
    of the DB's window, or unknown to it."""
    open_ids = set()
    for i in range(0, len(job_ids), 500):
        chunk = job_ids[i:i + 500]
        open_ids |= {r[0] for r in conn.execute(
            f"SELECT job_id FROM jobs WHERE status = 'active' AND job_id IN "
            f"({','.join('?' * len(chunk))})", chunk)}
    return [j for j in job_ids if j not in open_ids]
