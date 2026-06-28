"""Evaluate Workday discovery coverage.

Answers "are we missing companies?" by measuring the discovery funnel:

    Common Crawl unique tenants  ->  validated into companies.yaml

A large gap between "surfaced" and "validated" means tenants exist that we
failed to capture (usually the crawled URL pointed at a stale/closed site).
A small total "surfaced" count means Common Crawl itself is the bottleneck —
add more CC indexes or another discovery source.

    python -m src.coverage              # full re-harvest (slow), then report
    python -m src.coverage --quick      # report from companies.yaml only
"""

from __future__ import annotations

import argparse
import os
import sys

import yaml

from . import discover

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_COMPANIES = os.path.join(_ROOT, "config", "companies.yaml")


def _load_companies() -> list[dict]:
    data = yaml.safe_load(open(_COMPANIES, encoding="utf-8")) or {}
    return data.get("companies", [])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true",
                    help="skip the Common Crawl re-harvest; report companies.yaml stats only")
    args = ap.parse_args()

    companies = _load_companies()
    validated_tenants = {c["tenant"] for c in companies}
    validated_keys = {(c["tenant"], c["wd"], c["site"]) for c in companies}

    print("=== companies.yaml ===")
    print(f"  Entries (tenant+site combos): {len(companies)}")
    print(f"  Unique tenants              : {len(validated_tenants)}")
    print(f"  Unique tenant+wd+site keys  : {len(validated_keys)}")

    if args.quick:
        return 0

    print("\n=== Re-harvesting Common Crawl to measure the funnel ===")
    print("(this is slow; Ctrl-C is safe — progress is checkpointed)\n")
    raw = discover.harvest_from_common_crawl()
    raw += discover.harvest_from_seeds()

    surfaced_tenants = {c["tenant"] for c in raw}
    surfaced_keys = {(c["tenant"], c["wd"], c["site"]) for c in raw}

    missing_tenants = surfaced_tenants - validated_tenants
    missing_keys = surfaced_keys - validated_keys

    print("\n=== Coverage funnel ===")
    print(f"  Common Crawl surfaced tenants : {len(surfaced_tenants)}")
    print(f"  Of those, validated           : {len(surfaced_tenants & validated_tenants)}")
    print(f"  Surfaced but NOT in yaml       : {len(missing_tenants)}")
    print(f"  Surfaced site-combos not in yaml: {len(missing_keys)}")

    if missing_tenants:
        coverage_pct = 100 * len(surfaced_tenants & validated_tenants) / len(surfaced_tenants)
        print(f"\n  Tenant coverage: {coverage_pct:.1f}% of surfaced tenants are validated.")
        print(f"\n  Sample of {min(30, len(missing_tenants))} surfaced-but-missing tenants:")
        for t in sorted(missing_tenants)[:30]:
            print(f"    - {t}")
        print("\n  These either had no jobs at scrape time or their crawled site")
        print("  path was stale. Re-run `python -m src.discover` to retry them.")
    else:
        print("\n  Every surfaced tenant is in companies.yaml — Common Crawl is the ceiling.")
        print("  To find more, add newer CC indexes to _CC_INDEXES in discover.py.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
