"""Evaluate discovery coverage across every supported ATS.

Answers "are we missing companies?" by measuring the discovery funnel per ATS:

    candidates surfaced (seeds + Common Crawl)  ->  validated into config

A large gap between "surfaced" and "validated" means boards exist that we
failed to capture (usually stale/closed at validation time). A small total
"surfaced" count means the harvest itself is the bottleneck — raise
--cc-max-pages / --cc-indexes or add another discovery source.

    python -m src.coverage              # full re-harvest (slow), then report
    python -m src.coverage --quick      # report from config files only
"""

from __future__ import annotations

import argparse
import sys

from .discover import (ATS_SPECS, _dedupe, _load_existing,
                       harvest_from_common_crawl, harvest_from_seeds)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true",
                    help="skip the Common Crawl re-harvest; report config stats only")
    ap.add_argument("--cc-max-pages", type=int, default=40)
    ap.add_argument("--cc-indexes", type=int, default=3)
    args = ap.parse_args()

    print("=== Configured companies per ATS ===")
    for spec in ATS_SPECS:
        companies, keys = _load_existing(spec)
        print(f"  {spec.name:<16} {len(companies):>6} entries "
              f"({len(keys)} unique keys)")

    if args.quick:
        return 0

    print("\n=== Re-harvesting (seeds + Common Crawl) to measure the funnel ===")
    print("(slow; Ctrl-C is safe — CC progress is checkpointed)\n")
    found = harvest_from_seeds()
    for ats, cands in harvest_from_common_crawl(
            max_pages=args.cc_max_pages, num_indexes=args.cc_indexes).items():
        found.setdefault(ats, []).extend(cands)

    print("\n=== Coverage funnel per ATS ===")
    for spec in ATS_SPECS:
        surfaced = {spec.key(c) for c in _dedupe(spec, found.get(spec.name, []))}
        _, validated = _load_existing(spec)
        missing = surfaced - validated
        pct = 100 * len(surfaced & validated) / len(surfaced) if surfaced else 0.0
        print(f"\n  {spec.name}")
        print(f"    surfaced          : {len(surfaced)}")
        print(f"    validated in yaml : {len(surfaced & validated)} ({pct:.1f}%)")
        print(f"    surfaced-not-saved: {len(missing)}")
        if missing:
            sample = sorted(str(k) for k in missing)[:15]
            for k in sample:
                print(f"      - {k}")
            print("    (had no jobs at validation time, or stale URL — "
                  "re-run `python -m src.discover` to retry)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
