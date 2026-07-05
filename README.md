# Job Tracker V2

Tracks intern / new-grad / tech roles across companies that use **Workday**, and
announces newly-posted jobs to a Discord channel.

It works by calling Workday's own JSON jobs API directly — no browser, no DOM
scraping, no "black box" link extraction. Every Workday career site exposes:

```
POST https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
```

which returns structured job rows (title, location, apply path, stable job id).

## How it works

```
config/companies.yaml ──▶ scraper ──▶ classifier ──▶ SQLite ──▶ Discord
   (validated tenants)     (JSON API)  (keyword +     (new/      (announce
                                        Haiku)         updated/    new jobs
                                                       removed)    once)
```

| Step | File | What it does |
|---|---|---|
| Scrape | [src/scraper.py](src/scraper.py) | Pages the Workday jobs API per company + search term |
| Classify | [src/classify.py](src/classify.py) | Title → `intern \| new_grad \| mid \| senior`. Keyword pass first; Claude Haiku only for ambiguous titles |
| Persist | [src/db.py](src/db.py) | SQLite upsert keyed on Workday job id; tracks `first_seen` / `last_seen` / `status` |
| Notify | [src/notify.py](src/notify.py) | Posts `first_seen == this run` jobs to a Discord webhook (each job once) |
| Discover | [src/discover.py](src/discover.py) | Harvests careers URLs from seeds, GitHub job lists, JobSpy postings, and the Common Crawl URL index; every source feeds every ATS (Workday, Greenhouse, Lever, Ashby, SmartRecruiters, Workable). Candidates are validated against each ATS's public API and merged into the per-ATS config files |

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # optional: add keys for local runs
```

## Run locally

```bash
python -m src.main        # scrape + classify + persist + announce
python -m src.discover    # find new Workday companies (slow; run occasionally)
```

Without `ANTHROPIC_API_KEY`, classification uses the keyword pass only (the
Haiku tiebreaker is skipped). Without `DISCORD_WEBHOOK_URL`, new jobs print to
stdout instead of posting.

## Configuration

- **`config/companies.yaml`** — the companies to scrape. Each needs `tenant`,
  `wd`, and `site`, all readable from a careers URL
  `https://{tenant}.{wd}.myworkdayjobs.com/{site}`. `discover.py` appends to it.
- **`config/settings.yaml`** — search terms, pagination caps, politeness delays,
  and the LLM-fallback toggle.
- **`config/seeds.txt`** — hand-curated careers URLs for the discovery step.

## Scheduling (GitHub Actions, $0 hosting)

[.github/workflows/scrape.yml](.github/workflows/scrape.yml) runs every 6 hours,
then commits the updated `data/jobs.db` back to the repo so state survives
between runs on ephemeral runners.

To enable:

1. Push this repo to GitHub.
2. In **Settings → Secrets and variables → Actions**, add:
   - `ANTHROPIC_API_KEY` (optional — for the Haiku classifier)
   - `DISCORD_WEBHOOK_URL` (optional — for announcements)
3. The workflow needs write permission to push the DB; it's already declared via
   `permissions: contents: write`. Confirm **Settings → Actions → General →
   Workflow permissions** allows read/write.

**Known tradeoffs of this hosting choice:**
- GitHub cron is best-effort; runs are often 5–30 min late.
- Scheduled workflows auto-disable after 60 days of no repo activity.
- The DB is committed to git each run (binary diffs grow history).

## Database schema

```sql
CREATE TABLE jobs (
    job_id      TEXT PRIMARY KEY,   -- Workday id, e.g. JR2016444
    company     TEXT NOT NULL,
    title       TEXT NOT NULL,
    apply_url   TEXT NOT NULL,
    location    TEXT,
    role_type   TEXT CHECK(role_type IN ('intern','new_grad','mid','senior')),
    first_seen  TEXT NOT NULL,      -- ISO-8601, set once
    last_seen   TEXT NOT NULL,      -- bumped every run the job is still live
    status      TEXT DEFAULT 'active'  -- 'removed' when it drops out
);
```
