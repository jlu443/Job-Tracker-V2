"""Entry point: scrape every source, classify, persist, detect reposts, announce.

    python -m src.main                                  # everything, one process
    python -m src.main --scrape-part workday:0/2 --out p0.json.gz
    python -m src.main --scrape-part rest --out p2.json.gz
    python -m src.main --from-parts DIR                 # merge parts, then process

CI runs the scrape parts as parallel jobs (separate machines, so separate
IPs for Workday's per-IP rate limit) and one final job merges and processes.
The stages live in collect.py (scraping) and process.py (everything after).
"""

from __future__ import annotations

import argparse
import glob
import os
import sys
import time

from . import collect, config, db, maintenance, process

# Windows consoles default to cp1252; job titles are frequently Unicode.
# Never let a print() kill the run after the DB has already synced.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")

DB_PATH = os.environ.get("JOBS_DB_PATH") or os.path.join(config.ROOT, "data", "jobs.db")


def _apply_config_renames(conn) -> None:
    """Boards renamed by `python -m src.names` keep their old name under
    `aliases`; move stored rows over so the board's history stays attached."""
    renames = {}
    for source, config_file, _ in collect.ATS_SCRAPERS:
        for c in config.load_yaml(os.path.join(config.CONFIG_DIR, config_file)).get("companies") or []:
            for alias in c.get("aliases") or []:
                renames[(source, alias)] = collect.company_name(c)
    moved = maintenance.apply_renames(conn, renames)
    if moved:
        print(f"Renamed {moved} stored rows to their boards' company names.")


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="Job tracker run")
    ap.add_argument("--scrape-part", help='scrape only this part ("workday:0/2", "rest") '
                                          "and save it with --out; the DB isn't changed")
    ap.add_argument("--out", help="where --scrape-part writes its results (.json.gz)")
    ap.add_argument("--from-parts", help="directory of saved parts: merge them instead of "
                                         "scraping, then run the rest of the pipeline")
    args = ap.parse_args(argv)

    run_started = time.time()
    run_at = db._now()
    settings = config.load_settings()
    problems = config.validate(settings)
    if problems:
        print("config/settings.yaml has problems; not running:")
        for p in problems:
            print(f"  - {p}")
        return 2
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = db.connect(DB_PATH)
    _apply_config_renames(conn)

    if args.scrape_part:
        if not args.out:
            ap.error("--scrape-part needs --out")
        collected = collect.collect(settings, conn, args.scrape_part)
        dropped = collect.trim_for_handoff(collected, conn, settings)
        collect.save_part(args.out, collected)
        print(f"Saved {len(collected.postings)} postings for part {args.scrape_part!r} "
              f"({dropped} senior/mid postings not handed off)")
        conn.close()
        return 0

    if args.from_parts:
        paths = glob.glob(os.path.join(args.from_parts, "**", "*.json.gz"), recursive=True)
        if not paths:
            print(f"No scrape parts found in {args.from_parts}")
            return 1
        print(f"Merging {len(paths)} scrape parts")
        collected = collect.load_parts(paths)
    else:
        collected = collect.collect(settings, conn)

    process.process(conn, settings, collected, run_at, run_started)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
