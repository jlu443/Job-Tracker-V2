"""Measure pipeline accuracy against hand-curated ground truth.

SimplifyJobs-format lists are human-vetted intern/new-grad postings with a
role (which list they're in), a job category and a post date. That makes
them an answer key for the parts of the pipeline we'd otherwise judge by
eyeballing:

  coverage     Of curated jobs hosted on an ATS we scrape, how many did our
               own scrapers find? Misses split into "board not configured"
               (discovery gap) and "board configured, job missing" (scraper
               gap: search terms, paging, filters).
  role         Does the title classifier call known intern/new-grad jobs
               entry-level, and the right one of the two?
  category     Agreement with the curated job-function label.
  dates        Our post date vs the curated one, and how long after posting
               we first saw the job.

    python -m src.accuracy                       # against data/jobs.db
    python -m src.accuracy --db path/to/jobs.db --json out.json
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from statistics import median

import requests

from . import classify, dates, dedupe, discover, simplify_scraper

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ID_SOURCE = {"wd": "workday", "gh": "greenhouse", "lv": "lever", "ash": "ashby",
              "sr": "smartrecruiters", "wk": "workable", "orc": "oracle", "icims": "icims",
              "jibe": "jibe", "rip": "rippling", "tt": "tiktok", "amzn": "amazon",
              "apple": "apple"}


def load_truth(settings: dict) -> list[dict]:
    """Active curated listings, each tagged with the role of its list."""
    truth, seen = [], set()
    for entry in settings.get("curated_lists", {}).get("repos", []):
        try:
            items = requests.get(simplify_scraper._RAW.format(repo=entry["repo"]),
                                 timeout=60).json()
        except (requests.RequestException, ValueError) as exc:
            print(f"  ! {entry['repo']}: {exc}")
            continue
        for item in items:
            if not item.get("active") or not item.get("is_visible", True):
                continue
            key = item.get("url") or item.get("id")
            if key in seen:
                continue
            seen.add(key)
            truth.append({**item, "role": entry["role"]})
    return truth


def _configured_boards() -> dict[str, dict]:
    """ATS name → {board key: company name} for boards in config."""
    out = {}
    for spec in discover.ATS_SPECS:
        companies, _ = discover._load_existing(spec)
        names = {}
        for c in companies:
            names.setdefault(spec.key(c), c.get("name") or "")
        out[spec.name] = names
    return out


def _board_key(url: str):
    """(ats, board key) for a URL, when discovery can recognize the board."""
    for spec in discover.ATS_SPECS:
        cands = spec.extract(url)
        if cands:
            return spec.name, spec.key(cands[0])
    return None


def measure(truth: list[dict], conn: sqlite3.Connection, max_age: int | None = None) -> dict:
    conn.row_factory = sqlite3.Row
    first_party = {r["job_id"]: dict(r) for r in conn.execute(
        "SELECT job_id, source, company, posted_on, first_seen FROM jobs "
        "WHERE source NOT IN ('simplify','linkedin','indeed','glassdoor','zip_recruiter')")}
    purged = {r[0] for r in conn.execute("SELECT job_id FROM purged")} \
        if conn.execute("SELECT name FROM sqlite_master WHERE name='purged'").fetchone() else set()
    boards = _configured_boards()
    board_start = {(r[0], r[1]): r[2] for r in conn.execute(
        "SELECT source, company, first_scraped FROM boards")} \
        if conn.execute("SELECT name FROM sqlite_master WHERE name='boards'").fetchone() else {}
    scraped = set(board_start)

    # --- coverage ------------------------------------------------------------
    cov = defaultdict(Counter)
    missed_examples = defaultdict(list)
    lag_days, date_err = [], []
    for item in truth:
        jid = dedupe.canonical_job_id(item.get("url", ""))
        if not jid:
            cov["unsupported"]["listings"] += 1
            continue
        ats = _ID_SOURCE.get(jid.split("_", 1)[0], "other")
        c = cov[ats]
        c["listings"] += 1
        if jid in first_party:
            c["found"] += 1
            row = first_party[jid]
            posted = dates.epoch_to_iso(item.get("date_posted"))
            if posted and row["posted_on"]:
                date_err.append(abs((date.fromisoformat(row["posted_on"][:10])
                                     - date.fromisoformat(posted)).days))
            # Detection speed only means something for boards we were
            # already watching; a new board's backlog is "seen" late by design.
            watched_since = board_start.get((row["source"], row["company"]))
            if posted and watched_since and row["first_seen"] > watched_since:
                seen = datetime.fromisoformat(row["first_seen"]).date()
                if date.fromisoformat(posted) >= datetime.fromisoformat(watched_since).date():
                    lag_days.append((seen - date.fromisoformat(posted)).days)
            continue
        age = dates.age_days(dates.epoch_to_iso(item.get("date_posted")))
        if jid in purged or (max_age and age is not None and age > max_age):
            c["older_than_retention"] += 1     # skipped by max_listing_age_days, by design
            continue
        board = _board_key(item["url"])
        configured = boards.get(board[0], {}) if board else {}
        if not board or board[1] not in configured:
            c["board_not_configured"] += 1
        elif (board[0], configured[board[1]]) not in scraped:
            c["board_not_scraped_yet"] += 1      # long-tail rotation hasn't reached it
        elif classify.classify_by_keyword(item.get("title", "")) not in ("intern", "new_grad"):
            c["dropped_by_title_rules"] += 1     # not stored: title doesn't look entry-level
        else:
            c["scraper_missed"] += 1             # search terms / paging / id mismatch
            if len(missed_examples[ats]) < 8:
                missed_examples[ats].append(f"{item.get('company_name')}: {item.get('title')}")

    # --- role classifier -----------------------------------------------------
    role = Counter()
    role_misses = []
    for item in truth:
        got = classify.classify_by_keyword(item.get("title", ""))
        role[(item["role"], got or "none")] += 1
        if got not in ("intern", "new_grad") and len(role_misses) < 15:
            role_misses.append(f"[{item['role']}→{got or 'none'}] {item.get('title')}")
    n = len(truth)
    entry = sum(v for (want, got), v in role.items() if got in ("intern", "new_grad"))
    exact = sum(v for (want, got), v in role.items() if got == want)

    # --- category ------------------------------------------------------------
    cat = Counter()
    for item in truth:
        want = simplify_scraper._CATEGORY.get((item.get("category") or "").lower())
        if want:
            cat[(want, classify.categorize(item.get("title", "")))] += 1
    cat_total = sum(cat.values())
    cat_agree = sum(v for (w, g), v in cat.items() if w == g)
    cat_dropped = sum(v for (w, g), v in cat.items() if g == "other")

    in_scope = {ats: c["found"] + c["scraper_missed"] + c["dropped_by_title_rules"]
                for ats, c in cov.items() if ats != "unsupported"}
    return {
        "truth_listings": n,
        "in_scope_recall": {ats: round(cov[ats]["found"] / s, 3)
                            for ats, s in in_scope.items() if s},
        "coverage": {k: dict(v) for k, v in sorted(cov.items())},
        "missed_examples": dict(missed_examples),
        "role": {
            "entry_level_recall": round(entry / n, 3) if n else None,
            "exact_role_accuracy": round(exact / n, 3) if n else None,
            "confusion": {f"{w}->{g}": v for (w, g), v in role.most_common()},
            "misses": role_misses,
        },
        "category": {
            "agreement": round(cat_agree / cat_total, 3) if cat_total else None,
            "wrongly_other": cat_dropped,
            "confusion": {f"{w}->{g}": v for (w, g), v in cat.most_common(12)},
        },
        "dates": {
            "compared": len(date_err),
            "exact_share": round(sum(e == 0 for e in date_err) / len(date_err), 3)
            if date_err else None,
            "within_2_days_share": round(sum(e <= 2 for e in date_err) / len(date_err), 3)
            if date_err else None,
            "median_days_from_post_to_first_seen": median(lag_days) if lag_days else None,
            "detection_samples": len(lag_days),
        },
    }


def record_daily(conn: sqlite3.Connection, settings: dict) -> dict | None:
    """Run the measurement at most once a day and keep its headline numbers
    in the DB (table accuracy_history) so trends are visible over time."""
    conn.execute("CREATE TABLE IF NOT EXISTS accuracy_history "
                 "(measured_at TEXT PRIMARY KEY, report TEXT NOT NULL)")
    last = conn.execute("SELECT MAX(measured_at) FROM accuracy_history").fetchone()[0]
    now = datetime.now().astimezone()
    if last and (now - datetime.fromisoformat(last)).total_seconds() < 23 * 3600:
        return None
    report = measure(load_truth(settings), conn, settings.get("max_listing_age_days"))
    headline = {k: report[k] for k in ("truth_listings", "in_scope_recall")}
    headline["entry_level_recall"] = report["role"]["entry_level_recall"]
    headline["category_agreement"] = report["category"]["agreement"]
    headline["dates"] = report["dates"]
    conn.execute("INSERT INTO accuracy_history VALUES (?, ?)",
                 (now.isoformat(timespec="seconds"), json.dumps(headline)))
    conn.commit()
    return report


def _print(report: dict) -> None:
    print(f"\nGround truth: {report['truth_listings']} curated intern/new-grad listings\n")
    print("Coverage (our first-party scrapers vs curated jobs on each ATS):")
    print("  in-scope = found + scraper-miss + title-rules (jobs we should have)")
    print(f"  {'ats':<16}{'listings':>9}{'found':>7}{'in-scope':>9}{'recall':>8}  {'scraper-miss':>12}"
          f"  {'title-rules':>11}  {'not-scraped':>11}  {'no-board':>8}  {'too-old':>7}")
    for ats, c in report["coverage"].items():
        if ats == "unsupported":
            continue
        listings = c.get("listings", 0)
        found = c.get("found", 0)
        scope = found + c.get('scraper_missed', 0) + c.get('dropped_by_title_rules', 0)
        recall = f"{found / scope:.0%}" if scope else "-"
        print(f"  {ats:<16}{listings:>9}{found:>7}{scope:>9}{recall:>8}  "
              f"{c.get('scraper_missed', 0):>12}  {c.get('dropped_by_title_rules', 0):>11}  "
              f"{c.get('board_not_scraped_yet', 0):>11}  {c.get('board_not_configured', 0):>8}  "
              f"{c.get('older_than_retention', 0):>7}")
    print(f"  (on ATSes we don't scrape: {report['coverage'].get('unsupported', {}).get('listings', 0)})")
    r = report["role"]
    print(f"\nRole classifier: entry-level recall {r['entry_level_recall']:.1%}, "
          f"exact role {r['exact_role_accuracy']:.1%}")
    print("  " + ", ".join(f"{k}: {v}" for k, v in list(r["confusion"].items())[:8]))
    c = report["category"]
    print(f"\nCategory agreement {c['agreement']:.1%} "
          f"({c['wrongly_other']} curated tech jobs we'd label 'other' and not announce)")
    d = report["dates"]
    if d["compared"]:
        print(f"\nDates ({d['compared']} compared): exact {d['exact_share']:.0%}, within 2 days "
              f"{d['within_2_days_share']:.0%}; median {d['median_days_from_post_to_first_seen']}"
              f" days from posting to first seen ({d['detection_samples']} jobs on boards"
              " already watched when posted)")
    for ats, ex in report["missed_examples"].items():
        print(f"\nScraper missed on {ats} (board scraped, title entry-level), e.g.:")
        for e in ex:
            print(f"  - {e}")
    print("\nRole misses, e.g.:")
    for e in r["misses"]:
        print(f"  - {e}")


def main() -> int:
    from .main import _load_yaml, _SETTINGS
    ap = argparse.ArgumentParser(description="Pipeline accuracy vs curated lists")
    ap.add_argument("--db", default=os.path.join(_ROOT, "data", "jobs.db"))
    ap.add_argument("--json", help="also write the report as JSON here")
    args = ap.parse_args()
    settings = _load_yaml(_SETTINGS)
    truth = load_truth(settings)
    report = measure(truth, sqlite3.connect(args.db), settings.get("max_listing_age_days"))
    _print(report)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
