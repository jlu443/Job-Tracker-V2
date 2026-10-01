#!/usr/bin/env bash
# Fetch jobs.db from the $DB_RELEASE release into data/. Falls back to the
# newest daily snapshot if the main asset is missing (e.g. an upload died
# mid-way). Never starts from an empty DB: that would silently discard history.
set -euo pipefail
mkdir -p data
if gh release download "$DB_RELEASE" -p jobs.db -D data --clobber; then
  echo "Downloaded jobs.db"
  exit 0
fi
snap=$(gh release view "$DB_RELEASE" --json assets --jq '.assets[].name' \
       | grep -E '^jobs-[0-9-]+\.db$' | sort | tail -n 1 || true)
if [ -z "$snap" ]; then
  echo "::error::No jobs.db or snapshot on release $DB_RELEASE"
  exit 1
fi
echo "::warning::jobs.db missing; restoring snapshot $snap"
gh release download "$DB_RELEASE" -p "$snap" -D data --clobber
mv "data/$snap" data/jobs.db
