# Job Tracker V2

Tracks intern / new-grad tech roles across **~2,800 company job boards** spanning
six applicant-tracking systems — **Workday, Greenhouse, Lever, Ashby,
SmartRecruiters, Workable** — plus external job boards (**Indeed, Glassdoor,
ZipRecruiter** via JobSpy), and announces newly-posted intern/new-grad US jobs
to a **Discord channel** and a **Google Sheet**.

Every ATS scraper calls that platform's own public JSON API directly — no
browser, no DOM scraping. For example, every Workday career site exposes:

```
POST https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
```

which returns structured job rows (title, location, apply path, stable job id).
Greenhouse, Lever, Ashby, SmartRecruiters, and Workable have equivalent public
endpoints.

## How it works

```
config/*.yaml ──▶ scrapers ──▶ classifier ──▶ SQLite ──▶ Discord + Google Sheets
 (per-ATS         (6 ATS JSON   (keyword +     (new/       (announce new
  company lists)   APIs +        zero-shot      updated/     intern/new_grad
                   JobSpy)       fallback)      removed)     US jobs, once)
```

| Step | File | What it does |
|---|---|---|
| Scrape | [src/scraper.py](src/scraper.py) (Workday), [greenhouse_scraper.py](src/greenhouse_scraper.py), [lever_scraper.py](src/lever_scraper.py), [ashby_scraper.py](src/ashby_scraper.py), [smartrecruiters_scraper.py](src/smartrecruiters_scraper.py), [workable_scraper.py](src/workable_scraper.py) | One module per ATS, each hitting that platform's public jobs API. Companies are scraped concurrently (`scrape_workers` threads) over pooled HTTP connections ([src/http_pool.py](src/http_pool.py)) |
| External boards | [src/jobspy_scraper.py](src/jobspy_scraper.py) | Indeed / Glassdoor / ZipRecruiter via JobSpy, normalized into the same posting shape. LinkedIn is excluded (blocks datacenter IPs) |
| Classify | [src/classify.py](src/classify.py) | Title → `intern \| new_grad \| mid \| senior`. Deterministic keyword/regex pass first; an optional local zero-shot model (`facebook/bart-large-mnli`) handles ambiguous titles when `use_llm_fallback` is on. Only genuinely new postings are classified, in one batched pass |
| Persist | [src/db.py](src/db.py) | SQLite upsert keyed on job id; tracks `first_seen` / `last_seen` / `status` / `source` |
| Notify | [src/notify.py](src/notify.py) | Posts `first_seen == this run` jobs to a Discord webhook — filtered to **intern/new_grad roles in the US** — each job announced once |
| Sheet sync | [src/sheets.py](src/sheets.py) | Appends the same new jobs to a Google Sheet via an Apps Script webhook ([docs/apps_script.gs](docs/apps_script.gs)) |
| Discover | [src/discover.py](src/discover.py) | Harvests careers URLs from seeds, GitHub job lists, JobSpy postings, and the Common Crawl URL index; every source feeds every ATS. Candidates are validated against each ATS's public API and merged into the per-ATS config files |
| Coverage | [src/coverage.py](src/coverage.py) | Reports the discovery funnel per ATS (candidates surfaced → validated into config) to answer "are we missing companies?" |

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # optional: add webhook URLs for local runs
```

## Run locally

```bash
python -m src.main                    # scrape + classify + persist + announce
python -m src.discover                # find new company boards (slow; run occasionally)
python -m src.discover --seeds-only   # fast smoke test of discovery
python -m src.coverage --quick        # per-ATS config stats without re-harvesting
```

Without `DISCORD_WEBHOOK_URL`, new jobs print to stdout instead of posting.
Without `GOOGLE_SHEETS_WEBHOOK_URL`, the sheet sync is skipped.

## Configuration

- **Per-ATS company lists** — [config/companies.yaml](config/companies.yaml)
  (Workday), [greenhouse.yaml](config/greenhouse.yaml),
  [lever.yaml](config/lever.yaml), [ashby.yaml](config/ashby.yaml),
  [smartrecruiters.yaml](config/smartrecruiters.yaml),
  [workable.yaml](config/workable.yaml). `discover.py` appends validated boards
  to these; existing entries are always preserved.
- **[config/settings.yaml](config/settings.yaml)** — search terms, pagination
  caps, politeness delays, scrape concurrency, the zero-shot-fallback toggle,
  and JobSpy settings (sites, search terms, location, recency window).
- **[config/seeds.txt](config/seeds.txt)** — hand-curated careers URLs for the
  discovery step.

### Classification fallback

The keyword pass is always on and free. The zero-shot fallback
(`use_llm_fallback: true`) runs `facebook/bart-large-mnli` locally — no API
key, but a ~1.6 GB one-time model download, and it needs `transformers` +
`torch` (commented out in [requirements.txt](requirements.txt)). It's **off in
CI**: classifying thousands of ambiguous titles on a CPU runner takes hours.
Inconclusive titles default to `mid`, which is never announced anyway.

## Scheduling (GitHub Actions, $0 hosting)

[.github/workflows/scrape.yml](.github/workflows/scrape.yml) runs **hourly**,
then commits the updated `data/jobs.db` back to the repo so state survives
between runs on ephemeral runners. A concurrency group prevents two runs from
racing on the committed DB, and the DB is committed even if a late step fails
so postings aren't re-announced next run.

To enable:

1. Push this repo to GitHub.
2. In **Settings → Secrets and variables → Actions**, add (all optional):
   - `DISCORD_WEBHOOK_URL` — for Discord announcements
   - `GOOGLE_SHEETS_WEBHOOK_URL` — for the Google Sheet sync (see
     [docs/apps_script.gs](docs/apps_script.gs) for the one-time setup)
   - `JOBSPY_PROXY` — residential proxy; without it only Indeed works from
     GitHub's datacenter IPs (Glassdoor/ZipRecruiter block them)
3. The workflow needs write permission to push the DB; it's already declared
   via `permissions: contents: write`. Confirm **Settings → Actions → General →
   Workflow permissions** allows read/write.

**Known tradeoffs of this hosting choice:**
- GitHub cron is best-effort; runs are often 5–30 min late.
- Scheduled workflows auto-disable after 60 days of no repo activity.
- The DB is committed to git each run (binary diffs grow history).

## Database schema

```sql
CREATE TABLE jobs (
    job_id      TEXT PRIMARY KEY,   -- ATS-native id, or a hash for external boards
    company     TEXT NOT NULL,
    title       TEXT NOT NULL,
    apply_url   TEXT NOT NULL,
    location    TEXT,
    role_type   TEXT CHECK(role_type IN ('intern','new_grad','mid','senior')),
    posted_on   TEXT NOT NULL DEFAULT '',       -- posting date when the source provides one
    source      TEXT NOT NULL DEFAULT 'workday', -- which ATS/board it came from
    first_seen  TEXT NOT NULL,      -- ISO-8601, set once
    last_seen   TEXT NOT NULL,      -- bumped every run the job is still live
    status      TEXT NOT NULL DEFAULT 'active'  -- 'removed' when it drops out
);
```
