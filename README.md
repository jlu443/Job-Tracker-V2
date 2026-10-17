# Job Tracker V2

Finds US **internships and new-grad roles** in tech: software, data/ML,
hardware, quant and product. It reads them straight from company job boards,
**~13,300 boards across 17 applicant-tracking systems** plus five big
employers' own career sites. It also takes in community-curated lists and
LinkedIn / Indeed. Every 30 minutes it announces what's new to a **Discord
channel** and refreshes a shared **Google Sheet** with status tracking.

It runs entirely on GitHub Actions (no server, $0 hosting) and checks its own
accuracy every day against hand-curated lists.

| At a glance (2026-10-15) | |
|---|---|
| Job boards watched | 13,258: BambooHR 4,728 · Ashby 2,834 · Workday 1,762 · Greenhouse 972 · Rippling 902 · Jobvite 410 · iCIMS 342 · Lever 337 · SmartRecruiters 294 · JazzHR 246 · Oracle 208 · Jibe 89 · SuccessFactors 84 · Avature 20 · Paylocity 15 · Taleo 5 · Eightfold 5 · TikTok, ByteDance, Amazon, Apple, Atlassian |
| Open US tech roles tracked | 17,595 (10,774 internships, 6,821 new-grad) at 3,268 companies |
| How often | Every 30 minutes; a new posting reaches Discord and the Sheet in about 30–45 minutes |
| Reliability | 399 of 404 runs succeeded since 2026-10-01 (98.8%); median run 11 min |
| Found vs curated lists | 96% on Workday, 98% Greenhouse, 99% Ashby, 96% Lever, 93% SuccessFactors, 90% iCIMS, 87% Oracle ([how it's measured](#accuracy-how-we-know-its-right)) |
| Post dates | 87% exact, 96% within 2 days; median 0 days from posting to first seen |
| Tests | 447, run on every scheduled run before the pipeline touches the database |

## What you get

**Discord**, after every run: one digest of the new jobs, grouped by job
function, one line per job (company · title · city · age).
- **⭐** marks jobs posted in the last 2 days that aren't reposts and don't have many applicants yet.
- Flags appear only when they apply: 🎓 new grad, ❌/✅ visa sponsorship, 🇺🇸 citizens only, 🔒 clearance, ♻️ repost, 🕰️ old posting, and pay.
- A separate alert goes out when a source breaks (see [Operations](#operations)).

**Google Sheet** (via an Apps Script webhook, [docs/apps_script.gs](docs/apps_script.gs)),
every tab sorted newest-posted first:

| Tab | Contents |
|---|---|
| Internships · Today / This Week | Announced in the last 24 hours / the 6 days before, one row per role (cities merged) |
| New Grad · Today / This Week | The same for new-grad roles |
| Internships · All Open, New Grad · All Open | Every open US tech job in the 60-day window, refreshed daily |
| PhD & Research | Open PhD and research-track (MS/PhD) internships |
| Visa Sponsors | Companies with open PhD/research internships: their H-1B record (USCIS, FY2021–23) and what their postings say about sponsorship and OPT/CPT |
| My Applications | Every job you give a Status (Interested → Offer); never trimmed. A job taken down is marked *closed* |
| Key | What each colour, column and tab means |

Columns read from the job description: Visa sponsorship, US citizenship,
Clearance, Pay, Grad year. A blank cell means the description hasn't been
read yet; *Not mentioned* means it was read and doesn't say. **Apply** links
go to the employer's own page when an aggregator reveals it.

## How it works

```
config/*.yaml ─▶ scrape ─▶ dedupe ─▶ classify ─▶ SQLite ─▶ triage ─▶ enrich ─▶ Discord + Sheet
13,258 boards   (22 scrapers  (exact id,  (role +    (new /     (new vs     (description:
                 + curated     then fuzzy  function)  updated /  backlog /   visa, clearance,
                 lists +       company/                removed)   repost)     pay, country)
                 LinkedIn,     title/city)
                 Indeed)
```

Every scraper calls the platform's own public endpoint directly; there's no
browser. For example, every Workday career site exposes
`POST https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs`.
iCIMS, SuccessFactors, JazzHR, Jobvite and Avature have no usable JSON API,
so their server-rendered listing pages are parsed instead.

| Step | Code | What it does |
|---|---|---|
| Scrape | [scraper.py](src/scraper.py) (Workday), [greenhouse](src/greenhouse_scraper.py), [lever](src/lever_scraper.py), [ashby](src/ashby_scraper.py), [smartrecruiters](src/smartrecruiters_scraper.py), [oracle](src/oracle_scraper.py), [eightfold](src/eightfold_scraper.py), [successfactors](src/successfactors_scraper.py), [icims](src/icims_scraper.py), [jibe](src/jibe_scraper.py), [rippling](src/rippling_scraper.py), [bamboohr](src/bamboohr_scraper.py), [jazzhr](src/jazzhr_scraper.py), [jobvite](src/jobvite_scraper.py), [avature](src/avature_scraper.py), [paylocity](src/paylocity_scraper.py), [taleo](src/taleo_scraper.py), [bigtech](src/bigtech_scrapers.py) | One module per platform; sources run in parallel, boards within a source concurrently. **Every Workday and iCIMS board gets a quick "what's new?" check every run.** The full keyword sweep, which also catches closed listings, rotates through boards: every 6 h for Workday, every 4 h for iCIMS. The thousands of small-company BambooHR, JazzHR and Rippling boards are read every run once they've had an entry-level job, otherwise every 6 h. A Workday facet pass reads each board's own intern / early-career filters |
| Curated lists | [simplify_scraper.py](src/simplify_scraper.py) | SimplifyJobs-format intern/new-grad lists (~8.8k active), covering employers no scraper reaches (Tesla, Google, Jane Street, ...) |
| Aggregators | [jobspy_scraper.py](src/jobspy_scraper.py) | LinkedIn and Indeed through JobSpy; Glassdoor/ZipRecruiter only with a residential proxy (`JOBSPY_PROXY`) |
| Dedupe | [dedupe.py](src/dedupe.py) | Any apply URL (curated list, LinkedIn, Indeed) is mapped to the job id our own scraper would give it, so copies collapse exactly; then a fuzzy company + title + city match. The company's own listing always wins |
| Classify | [classify.py](src/classify.py) | Title → `intern / new_grad / mid / senior` and a job function (`software, data_ml, hardware, quant, product, other`). Deterministic rules, checked against hand-labeled sets on every run (see below). A board's own level label (Greenhouse/Lever/Ashby level fields, Workday intern filters) and a curated list's label count too, unless the title states seniority ("Senior", "Manager") |
| Plain titles | [plain_titles.py](src/plain_titles.py) | About half of new-grad jobs are titled just "Software Engineer"; only the description says "new graduates" or "0–2 years". For the platforms whose listings carry no description (Workday, Oracle, iCIMS, SmartRecruiters, SuccessFactors, Eightfold, BambooHR, JazzHR, Jobvite, Avature, Paylocity, Taleo), each run reads up to 500 new US tech postings' descriptions, each only once |
| Persist | [db.py](src/db.py), [schema.py](src/schema.py) | SQLite upsert; numbered, tested migrations (22 so far) |
| Triage | [repost.py](src/repost.py) | Decides what's news. A new board's backlog, and jobs a rule change newly recognizes, are stored quietly. Reposts are labeled: re-listed roles; LinkedIn bumps (LinkedIn ids are sequential, so an old id with a new date is a repost); same id re-dated; post dates 30+ days old (not announced) |
| Enrich | [enrich.py](src/enrich.py) | Reads each announceable job's description: visa sponsorship, US citizenship, clearance, OPT/CPT, grad year, pay, PhD/research track. Workday job pages also give the **country** of jobs listed as a bare "Crawley" or "2 Locations" |
| Announce | [notify.py](src/notify.py), [sheets.py](src/sheets.py) | Announces only internships and new-grad roles in the US, in a tech function, once, with the same role in other cities merged |
| Check | [accuracy.py](src/accuracy.py), [health.py](src/health.py) | Daily accuracy against curated lists; per-run health checks with Discord alerts |
| Discover | [discover.py](src/discover.py), [names.py](src/names.py) | Daily: finds new boards from curated lists, job posts, seeds and the Common Crawl index, validates them against each platform's API, and adds them to `config/`; `names.py` replaces URL slugs with real company names |

## Accuracy: how we know it's right

**Coverage against an answer key.** The curated lists are human-vetted
intern/new-grad postings, so [accuracy.py](src/accuracy.py) uses them every
day as an answer key. Of the curated jobs on a board we scrape, it measures
how many our own scrapers found, and it puts each miss down to a cause: the
scraper missed it, the title rules dropped it, the board isn't configured, or
the posting is too old. That's how the gaps below were found and closed:

| Found by our own scrapers | 2026-10-01 | 2026-10-14 |
|---|---|---|
| Workday | 31% | 96% |
| Greenhouse | 83% | 98% |
| Ashby | 90% | 99% |
| Lever | 67% | 96% |
| iCIMS | 68% | 90% |
| Oracle | 78% | 87% |
| SmartRecruiters | 40% | 76% (most remaining misses are years-old "always open" ads) |
| Post date exact / within 2 days | 73% / 86% | 87% / 96% |

**Precision, checked by hand.** Curated lists hold only tech jobs, so they
can't show non-tech jobs being announced. Two hand-labeled regression sets
are run as tests:
- [tests/golden_titles.tsv](tests/golden_titles.tsv): 252 titles with their correct level, including every past classifier bug.
- [tests/golden_categories.tsv](tests/golden_categories.tsv): a random 240 of the announced titles from two weeks, labeled by job function. Announcement precision on it went from 69% to 98% (2026-10-11), and the test holds it at 97% or more.

**Safeguards that keep the feed honest:**
- **US only.** A location like "Milwaukee" or "Indianapolis" is matched on whole words, never as a substring, so "uk" and "india" inside them don't count. A job page's country decides when the location field can't. On the 2026-10-12 run, 98 of 200 Workday jobs that couldn't be placed turned out to be abroad.
- **Nothing is marked closed on a failed scrape.** A job is retired only when its board's scrape succeeded. For Workday and SuccessFactors, the job's own page must also say it's gone, because their listings page in an unstable order. Aggregator rows expire after 21 days unseen.
- **No old news.** The first scrape of a new board, and jobs newly recognized after a rule change, are stored without announcing. Listings older than 60 days are dropped, except upcoming-season titles ("Summer 2027") and curated listings.
- **Stated seniority wins** over a source's "entry level" label ("Manager", "Senior", "Vice President").

## Operations

**Schedule.** [.github/workflows/scrape.yml](.github/workflows/scrape.yml)
runs every 30 minutes (`run_interval_minutes: 30`). GitHub's own cron is
best-effort: hourly schedules arrived every 4–7 hours. So the trigger is
external: a [cron-job.org](https://cron-job.org) job POSTs to
`https://api.github.com/repos/<owner>/<repo>/actions/workflows/scrape.yml/dispatches`
at :00 and :30. It sends headers `Authorization: Bearer <fine-grained token,
Actions read/write on this repo only>` and `Accept:
application/vnd.github+json`, with body `{"ref":"main"}`, and expects a 204
response. The workflow's own schedule stays as an every-3-hours fallback.
**Renew the token before it expires**; if runs stop, check that first.

**One run** (median 11 min, p90 16 min):
1. Four **scrape** jobs run in parallel on separate machines: Workday in two halves, iCIMS, and everything else. Separate machines mean separate IPs for Workday's per-IP rate limit. Each saves its postings as an artifact.
2. The **process** job runs the test suite, then merges the parts and does classification, the DB sync, enrichment, Discord and the Sheet.
3. Runs never overlap (a concurrency group); a late one waits.

**State.** `jobs.db` (~70 MB) lives as an asset on the `db-latest` GitHub
release, not in git.
- Each run downloads it, runs an integrity check, and uploads it back. The upload happens even if a late step failed, so jobs aren't re-announced.
- A daily snapshot (`jobs-YYYY-MM-DD.db`, 7 kept) is uploaded alongside, and the download falls back to the newest snapshot if `jobs.db` is ever missing.
- To roll back: download a snapshot, rename it to `jobs.db`, and `gh release upload db-latest jobs.db --clobber`.

**Resilience:**
- One broken board never stops a run; errors are isolated per board.
- Boards that fail on rate limits are retried once, slower.
- Request rates are kept polite per platform. iCIMS is globally spaced and backs off on its bot check; Workday answers bursts with 429s.
- Rows from a source that's no longer scraped are retired after 21 days unseen.
- Scheduled workflows in public repos auto-disable after 60 days without commits, so the workflow pushes an empty keepalive commit when needed.

**Health checks** ([src/health.py](src/health.py)) send a Discord alert (to
`DISCORD_ALERT_WEBHOOK_URL` if set, else the jobs channel) at most once a day
per problem. The checks:
- a source returns far fewer postings than its recent median;
- more than half of a source's boards fail;
- a run nears the 30-minute CI limit.

**Daily discovery** ([.github/workflows/discover.yml](.github/workflows/discover.yml))
adds new boards found in the curated lists and commits the config, including
Greenhouse boards hidden behind company career pages (`?gh_jid=`).

## Setup

1. Push the repo to GitHub. In **Settings → Actions → General → Workflow permissions**, allow read/write (the workflow updates the release asset).
2. Add secrets in **Settings → Secrets and variables → Actions**. All are optional:
   - `DISCORD_WEBHOOK_URL`: the jobs channel.
   - `DISCORD_ALERT_WEBHOOK_URL`: a separate channel for health alerts.
   - `GOOGLE_SHEETS_WEBHOOK_URL`: the Apps Script web-app URL. Setup is in the header of [docs/apps_script.gs](docs/apps_script.gs): paste it into the sheet's Apps Script and deploy it as a web app with **Execute as: Me** and **Who has access: Anyone**. Use the `/exec` URL. With any other access setting, calls fail with 401.
   - `GOOGLE_SHEET_URL`: the sheet's browser link, shown in Discord digests.
   - `JOBSPY_PROXY`: a residential proxy for Glassdoor/ZipRecruiter, which block GitHub's IPs. LinkedIn and Indeed work without it.
3. Set up the external trigger (see [Operations](#operations)).

### Run locally

```bash
pip install -r requirements.txt
gh release download db-latest -p jobs.db -D data --clobber   # the live DB

python -m src.main            # one full run: scrape, classify, store, announce
python -m src.accuracy        # coverage / classification report vs curated lists
python -m src.discover        # find new boards (slow); --seeds-only for a quick test
python -m src.coverage --quick
python -m pytest -q tests
```

Local runs write only to `data/jobs.db` (or `JOBS_DB_PATH`); CI never reads
it. Webhooks come from environment variables (nothing loads `.env`
automatically). Without them, announcements print to the console and the
Sheet is skipped, which is the safe way to test against a copy of the live
DB.

## Configuration

- **Board lists**, one file per platform in [config/](config/): [companies.yaml](config/companies.yaml) is Workday, plus `greenhouse.yaml`, `ashby.yaml`, and so on. Discovery appends to them and never removes entries.
- **[config/settings.yaml](config/settings.yaml)**, each setting commented in place. The main ones:
  - `search_terms`: keyword sweeps; `{next_year}` becomes "2027".
  - `run_interval_minutes` and `long_tail_rotation`: cadence and full-sweep rotation (expressed in runs).
  - `recency_check`: the every-run new-postings check.
  - `announce_categories`: which job functions are announced.
  - `store_roles`: only intern/new_grad rows are kept.
  - `max_listing_age_days` and `age_limit_exempt_sources`: the 60-day window and its exceptions.
  - `reposts`: annotate or suppress, and the thresholds.
  - `enrich_*`, `plain_title_reads_per_run`, `country_lookups_per_run`: per-run budgets.
  - `discord.digest_hours` and `sheets.all_open_refresh_hours`.
  - `profile`: personal filters. Off, because the feed is shared.
- **[config/seeds.txt](config/seeds.txt)**: hand-picked careers URLs for discovery.
- **H-1B data**: `config/h1b_employers.json.gz`, refreshed with `python -m src.h1b --build 2021 2022 2023`.

## Database

```sql
CREATE TABLE jobs (
    job_id       TEXT PRIMARY KEY,  -- source-prefixed: wd_<tenant>_, gh_, lv_, ash_, sr_, orc_, icims_, sf_, ef_, bhr_, jazz_, rip_, li_, indeed_...
    company, title, apply_url, location,
    role_type    TEXT,              -- intern | new_grad (mid/senior aren't stored)
    category     TEXT,              -- software | data_ml | hardware | quant | product | other
    source       TEXT,              -- which scraper / list it came from
    posted_on    TEXT,              -- the source's post date ('' if it gives none)
    first_seen, last_seen, status,  -- status 'removed' once confirmed closed
    announced_at TEXT,              -- when it went to Discord / Today ('' = stored quietly)
    country      TEXT,              -- ISO code from the job page, when the location can't say
    job_key      TEXT,              -- fuzzy company|title|city identity
    repost, repost_of, relisted_on, bump_count,   -- repost evidence
    sponsorship, citizenship, clearance, opt_cpt, grad_year, pay,  -- from the description
    research_track TEXT,            -- phd | research_ms
    applicants, direct_url, checked_at, enrich_tries
);
-- also: boards (first scrape per board), purged (aged-out ids, so they aren't
-- re-announced), runs (postings per source per run), alerts, accuracy_history,
-- described (plain-titled postings already read), meta (schema version, cursors)
```

## Deliberate choices

- **US only, internships and new-grad only, tech functions only** for announcements and the Sheet. The DB keeps other functions for measurement.
- **No logged-in scraping.** LinkedIn shows "Reposted" only to logged-in users; reposts are inferred from id age instead, so no personal account is at risk.
- **No bot-protection bypass, and robots.txt is respected for every platform added since 2026-10-17.** Tesla and Citadel block non-browser clients; Meta prohibits automated collection; Google, Intuit and some Avature sites (Koch, ...) disallow it in robots.txt. Their jobs still arrive through the curated lists.
- **Workable was removed.** It blocks GitHub Actions IPs for 15+ hours; its listings still arrive through the curated lists.
- **No per-person filters by default.** The Discord channel and Sheet are shared.
