# Job Tracker V2

Tracks intern / new-grad tech roles across **~6,500 company job boards** spanning
eight applicant-tracking systems — **Workday, Greenhouse, Lever, Ashby,
SmartRecruiters, Oracle Recruiting Cloud, iCIMS** — plus curated
lists (SimplifyJobs format) and external job boards (**LinkedIn, Indeed**, and
Glassdoor/ZipRecruiter with a proxy, via JobSpy). New intern/new-grad US jobs
are announced to a **Discord channel** and published to a **Google Sheet**.

Every ATS scraper calls that platform's own public endpoint directly — no
browser. iCIMS, which has no JSON API, is read from its server-rendered
listing page. For example, every Workday career site exposes:

```
POST https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
```

which returns structured job rows (title, location, apply path, stable job id).
Greenhouse, Lever, Ashby and SmartRecruiters have equivalent public
endpoints.

## How it works

```
config/*.yaml ──▶ scrapers ──▶ dedupe ──▶ classifier ──▶ SQLite ──▶ enrich ──▶ Discord + Sheets
 (per-ATS         (6 ATS JSON   (drop        (keyword +    (new/      (parse desc:  (announce new
  company lists)   APIs +        cross-source  zero-shot    updated/    sponsorship,  intern/new_grad
                   JobSpy)       duplicates)   fallback)    removed)    clearance,    US jobs, once)
                                                                        grad year)
```

| Step | File | What it does |
|---|---|---|
| Scrape | [src/scraper.py](src/scraper.py) (Workday), [greenhouse_scraper.py](src/greenhouse_scraper.py), [lever_scraper.py](src/lever_scraper.py), [ashby_scraper.py](src/ashby_scraper.py), [smartrecruiters_scraper.py](src/smartrecruiters_scraper.py), [oracle_scraper.py](src/oracle_scraper.py), [icims_scraper.py](src/icims_scraper.py) | One module per ATS, each hitting that platform's public jobs endpoint. All ATS sources run **in parallel** (each is its own set of hosts with its own politeness limits), and each source's boards are scraped concurrently (`scrape_workers_by_source`). Greenhouse, Lever and Ashby also read job descriptions, so plain-titled new-grad roles ("Software Engineer" with "recent graduates, 0–2 years") are recognized. iCIMS requests are globally spaced and back off on its bot challenge. **Every Workday and iCIMS board is checked every run** for new postings (newest first, any title, classified locally, paging until nothing from the last `window_days`); the full keyword sweep, which is what detects closed listings, runs on the `long_tail_rotation` cadence. Workday 429s are retried with backoff. Scraper sessions refuse cookies (a jar shared across ~1,700 tenants made each request quadratic in boards scraped) |
| Big employers' own sites | [src/bigtech_scrapers.py](src/bigtech_scrapers.py) (TikTok, Amazon, Apple), [jibe_scraper.py](src/jibe_scraper.py) (Jibe `careers.X.com` sites: AMD, Johns Hopkins APL, Garmin, Keysight, Rivian, KPMG, PNNL, ...), [rippling_scraper.py](src/rippling_scraper.py) (Rippling ATS boards) | Public JSON search on each site; Jibe and Rippling boards are found by `discover.py` from curated-list links. Apple uses its Students team (internships + university roles; Apple Store roles excluded) and is treated as partial because its result counts vary between calls. Not scrapable, so left to the curated lists: ByteDance (signed requests), Tesla (bot protection), Eightfold sites such as Qualcomm/Microsoft (API refuses), Google |
| External boards | [src/jobspy_scraper.py](src/jobspy_scraper.py) | Indeed / Glassdoor / ZipRecruiter via JobSpy, scraped per-site and normalized into the same posting shape (descriptions kept for enrichment). Sites that block datacenter IPs go through `JOBSPY_PROXY` in CI; Indeed always goes direct |
| Curated lists | [src/simplify_scraper.py](src/simplify_scraper.py) | Ingests community-curated intern/new-grad lists (SimplifyJobs `listings.json` format; ~7.8k active postings incl. ATSes we don't scrape). Listings pointing at a supported ATS take that ATS's job id, so they collapse onto the first-party row |
| Dedupe | [src/dedupe.py](src/dedupe.py) | Exact first: any apply URL (curated list, LinkedIn/Indeed direct link) is mapped to the job id our own ATS scraper would assign, so copies collapse precisely. Then fuzzy (company, title, city) across sources. First-party ATS copy wins |
| Reposts | [src/repost.py](src/repost.py) | Tags announceable jobs as `relisted` (same role tracked before under another id), `linkedin` (LinkedIn ids are sequential, so an id far older than its post date means a bumped listing — the public API hides LinkedIn's own "Reposted" label), or `stale` (source post date ≥30 days old). Tagged in Discord; `reposts.announce: suppress` hides them. Separately, every run compares each tracked job's list date with the earliest one seen: the same id re-dated forward is a confirmed repost (LinkedIn's "Reposted", Indeed/Workday refreshes), recorded as `repost='bumped'`, `relisted_on`, `bump_count` |
| Classify | [src/classify.py](src/classify.py) | Title → `intern \| new_grad \| mid \| senior`, plus a job function (`software`, `data_ml`, `hardware`, `quant`, `product`, `other`) used by `announce_categories`. Deterministic keyword/regex pass first; an optional local zero-shot model (`facebook/bart-large-mnli`) handles ambiguous titles when `use_llm_fallback` is on. Only genuinely new postings are classified, in one batched pass |
| Persist | [src/db.py](src/db.py) | SQLite upsert keyed on job id; tracks `first_seen` / `last_seen` / `status` / `source` |
| Enrich | [src/enrich.py](src/enrich.py) | Fetches the full description of each new intern/new_grad posting from the ATS's detail API and parses it into flags: visa sponsorship (`no`/`yes`), security clearance, graduation-year window. Flags land in the DB, the Discord embed, and the Sheet |
| Notify | [src/notify.py](src/notify.py) | Posts each run's new **intern/new_grad US** jobs to Discord as one compact list per job category: one line per job with company, linked title, location, age and flags (visa, clearance, applicants, repost). ⭐ marks jobs posted in the last 2 days that aren't reposts and aren't swamped with applicants. Once a day, a one-line digest links to the Sheet's Today tab (`GOOGLE_SHEET_URL`) |
| Sheet | [src/sheets.py](src/sheets.py) | Publishes rebuilt tabs, every one sorted by posted date (newest first), via an Apps Script webhook ([docs/apps_script.gs](docs/apps_script.gs)): **Today** (24 h), **This Week**, **All Open** (every stored intern/new-grad job in the 60-day window, refreshed daily). A **Status** dropdown on every tab copies the job into **My Applications**, which is never trimmed; statuses survive every rebuild. Every tab carries **Visa sponsorship** (Offered / Not offered), **US citizenship** (Required) and **Clearance** (Required / Not required) read from the job description; *Not mentioned* means the description was read and says nothing, blank means not read yet. Descriptions come free with Greenhouse/Lever/Ashby/aggregator listings, and each run reads up to `enrich_backlog_per_run` more (PhD/research first). Also: **Pay** (Ashby/Lever structured pay, else parsed from the description, e.g. `$45–55/hr`), a live **Days ago** formula, **Apply** links that go to the employer's own page when an aggregator reveals it, and in *My Applications* a **Listing** column that turns *closed* (struck through) when a saved job is taken down (script v3) |
| Profile | [src/profile.py](src/profile.py) | Optional personal filters in `settings.yaml → profile` (grad year, needs sponsorship, US citizen, locations) for Discord, Today / This Week and the PhD tab; unknown values never filter |
| PhD & Research | [src/phd.py](src/phd.py) | A separate component reading the same DB: tags internships as `phd` (PhD in the title, or the description asks for PhD students) or `research_ms` (research-type title, or "MS or PhD" in the description), and lists the open US ones as the Sheet's **PhD & Research** tab with a Track column and the usual Status dropdown. Doesn't change what's announced |
| Accuracy | [src/accuracy.py](src/accuracy.py) | Scores the pipeline against the curated lists (hand-labeled intern/new-grad jobs): coverage per ATS with each miss attributed (scraper missed / title rules / board not scraped yet / board not configured / older than retention), role and category accuracy, post-date accuracy. Runs daily inside the pipeline (history in `accuracy_history`); `python -m src.accuracy` for a full report |
| Discover | [src/discover.py](src/discover.py) | Harvests careers URLs from seeds, GitHub job lists, JobSpy postings, and the Common Crawl URL index; every source feeds every ATS. Candidates are validated against each ATS's public API and merged into the per-ATS config files |
| Coverage | [src/coverage.py](src/coverage.py) | Reports the discovery funnel per ATS (candidates surfaced → validated into config) to answer "are we missing companies?" |

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # optional: add webhook URLs for local runs
# The live DB is a release asset, not in git. Grab a copy to work with:
gh release download db-latest -p jobs.db -D data --clobber
```

Local runs write only to your local `data/jobs.db`; CI never reads it.

## Run locally

```bash
python -m src.main                    # scrape + classify + persist + announce
python -m src.discover                # find new company boards (slow; run occasionally)
python -m src.discover --seeds-only   # fast smoke test of discovery
python -m src.names                   # give slug-named boards real company names
python -m src.accuracy                # score coverage/classification vs curated lists
python -m src.coverage --quick        # per-ATS config stats without re-harvesting
python -m pytest -q tests             # parser / dedupe / repost / DB tests (also run in CI)
```

Without `DISCORD_WEBHOOK_URL`, new jobs print to stdout instead of posting.
Without `GOOGLE_SHEETS_WEBHOOK_URL`, the sheet sync is skipped.

## Configuration

- **Per-ATS company lists** — [config/companies.yaml](config/companies.yaml)
  (Workday), [greenhouse.yaml](config/greenhouse.yaml),
  [lever.yaml](config/lever.yaml), [ashby.yaml](config/ashby.yaml),
  [smartrecruiters.yaml](config/smartrecruiters.yaml),
  [oracle.yaml](config/oracle.yaml), [icims.yaml](config/icims.yaml), [jibe.yaml](config/jibe.yaml), [rippling.yaml](config/rippling.yaml). `discover.py` appends validated boards
  to these; existing entries are always preserved.
- **[config/settings.yaml](config/settings.yaml)** — search terms, pagination
  caps, politeness delays, scrape concurrency, the zero-shot-fallback toggle,
  JobSpy settings (sites, search terms, location, recency window), and the
  enrichment toggles (`enrich_descriptions`, plus `exclude_no_sponsorship` to
  drop jobs that explicitly rule out visa sponsorship from announcements).
  Also: `announce_categories` (job functions to announce), `reposts`
  (annotate vs suppress, thresholds), `curated_lists` (SimplifyJobs-format
  repos), `scrape_workers_by_source`, `enrich_max_per_run`,
  `aggregator_ttl_days`, `store_roles` (only intern/new_grad rows are kept by
  default, which keeps the DB ~20 MB), and `long_tail_rotation`
  (boards that have never listed an entry-level job are checked every Nth
  run; boards with open entry-level jobs can also be set to every Nth run,
  e.g. `workday: {hot: 2, tail: 6}`, keeping CI at ~15 min with ~6,500 boards).
- **New boards are backfilled silently.** The first successful scrape of any
  board (a newly discovered company, or a new source like LinkedIn) is stored
  without announcing, so adding 1,000 boards doesn't post 10,000 old jobs.
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

[.github/workflows/scrape.yml](.github/workflows/scrape.yml) runs **hourly**.
Each run has two stages: four **scrape** jobs in parallel (Workday split in
two halves, iCIMS, and everything else; separate machines, so separate IPs for
Workday's per-IP rate limit), each saving its postings as an artifact; then
one **process** job merges them (`python -m src.main --from-parts parts`) and
does classification, the DB sync, enrichment, Discord and the Sheet. Locally,
`python -m src.main` still does everything in one process.
State survives between runs on ephemeral runners by keeping `jobs.db` as an
asset on the `db-latest` GitHub release: each run downloads it, scrapes, runs
an integrity check, and uploads it back (even if a late step failed, so
postings aren't re-announced). A daily snapshot (`jobs-YYYY-MM-DD.db`, last 7
kept) is uploaded alongside for rollback, and the download falls back to the
newest snapshot if `jobs.db` is ever missing. A concurrency group prevents two
runs from racing on the DB.

**Health checks** ([src/health.py](src/health.py)): every run records postings
per source. A source far below its recent median, more than half of a source's
boards failing, or a run nearing the 30-minute CI limit sends a Discord alert
(to `DISCORD_ALERT_WEBHOOK_URL` if set, else the jobs channel), at most once a
day per problem.

To restore an older DB: download a snapshot asset and re-upload it as
`jobs.db` (`gh release upload db-latest <file> --clobber` after renaming).

To enable:

1. Push this repo to GitHub.
2. In **Settings → Secrets and variables → Actions**, add (all optional):
   - `DISCORD_WEBHOOK_URL` — for Discord announcements
   - `GOOGLE_SHEETS_WEBHOOK_URL` — the Apps Script web-app URL (see
     [docs/apps_script.gs](docs/apps_script.gs) for setup). The scraper checks
     the script's version first and skips the sheet, with a warning, until
     the v2 script is deployed.
   - `GOOGLE_SHEET_URL` — the sheet's normal browser link, for the daily
     Discord digest
   - `DISCORD_ALERT_WEBHOOK_URL` — optional separate channel for health alerts
   - `JOBSPY_PROXY` — residential proxy; without it Glassdoor/ZipRecruiter are
     skipped in CI (they block GitHub's datacenter IPs). Indeed and LinkedIn
     work without it.
3. The workflow needs write permission to update the release asset; it's
   already declared via `permissions: contents: write`. Confirm **Settings →
   Actions → General → Workflow permissions** allows read/write.

**Known tradeoffs of this hosting choice:**
- GitHub's own cron is best-effort (hourly runs arrived every 4–7 hours), so
  the hourly trigger is external: a cron-job.org job POSTs to
  `https://api.github.com/repos/<owner>/<repo>/actions/workflows/scrape.yml/dispatches`
  at :05 with headers `Authorization: Bearer <fine-grained token, Actions:
  read/write on this repo only>`, `Accept: application/vnd.github+json`, and
  body `{"ref":"main"}` (expects 204). The workflow's own schedule remains as
  an every-3-hours fallback. **Renew the token before it expires.**
- Scheduled workflows in public repos auto-disable after 60 days without
  commits; the workflow pushes an empty keepalive commit if the last commit is
  45+ days old.
- The DB (and its snapshots) are public release assets, as the committed DB
  was before.

## Database schema

```sql
CREATE TABLE jobs (
    job_id      TEXT PRIMARY KEY,   -- source-prefixed: gh_, lv_, ash_, sr_, wk_, wd_<tenant>_, li_, sim_, indeed_<hash>
    company     TEXT NOT NULL,
    title       TEXT NOT NULL,
    apply_url   TEXT NOT NULL,
    location    TEXT,
    role_type   TEXT CHECK(role_type IN ('intern','new_grad','mid','senior')),
    posted_on   TEXT NOT NULL DEFAULT '',       -- posting date when the source provides one
    source      TEXT NOT NULL DEFAULT 'workday', -- which ATS/board it came from
    sponsorship TEXT NOT NULL DEFAULT '',       -- 'no' | 'yes' | '' (parsed from description)
    clearance   TEXT NOT NULL DEFAULT '',       -- 'yes' when a clearance is required
    grad_year   TEXT NOT NULL DEFAULT '',       -- e.g. '2026' or '2026, 2027'
    first_seen  TEXT NOT NULL,      -- ISO-8601, set once
    last_seen   TEXT NOT NULL,      -- bumped every run the job is still live
    status      TEXT NOT NULL DEFAULT 'active', -- 'removed' when it drops out of a
                                                --  successful scrape (aggregators: after 21 days unseen)
    job_key     TEXT NOT NULL DEFAULT '',       -- fuzzy company|title|city identity
    category    TEXT NOT NULL DEFAULT '',       -- software | data_ml | hardware | quant | product | other
    repost      TEXT NOT NULL DEFAULT '',       -- '' | relisted | linkedin | stale
    repost_of   TEXT NOT NULL DEFAULT '',       -- job_id of the earlier listing (relisted)
    applicants  TEXT NOT NULL DEFAULT '',       -- LinkedIn applicant count text
    relisted_on TEXT NOT NULL DEFAULT '',       -- latest date the source re-dated this same id
    bump_count  INTEGER NOT NULL DEFAULT 0      -- how many times it was re-dated
);
```

**Removed:** Workable (it answers GitHub Actions' IPs with a 15-hour block);
its listings still arrive through the curated lists.

**Post-date cutoff exceptions:** `age_limit_exempt_sources` (Apple) keeps
months-old postings that are still listed; they're deleted once unseen for 14
days instead. Sources with years-old "always open" ads (some SmartRecruiters
boards date theirs 2015) shouldn't be added.

**Visa sponsorship (PhD & Research tab).** `Visa outlook` combines what the
posting says (sponsorship, US-citizen/clearance requirements, and a new
`OPT/CPT` flag read from the description) with the company's H-1B record:
`H-1B history` = USCIS H-1B Employer Data Hub approvals, FY2021–23, summed
over the company's legal entities ([src/h1b.py](src/h1b.py); data committed as
`config/h1b_employers.json.gz`, refresh with `python -m src.h1b --build 2021
2022 2023`; abbreviations in `config/h1b_aliases.yaml`). No public per-employer
OPT or CPT data exists, so those come only from the posting itself.

**Daily discovery** ([.github/workflows/discover.yml](.github/workflows/discover.yml))
adds new boards found in the curated lists, including Greenhouse boards hidden
behind company career pages (`?gh_jid=`) or embeds, and commits the config.
