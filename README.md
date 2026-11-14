# job-pipeline

Automated job application pipeline for software engineers. Finds jobs on the open web — no company list required — filters by H-1B sponsorship (JD-first), scores fit 0-100, and generates tailored LaTeX resumes per job.

Built for international students and H-1B candidates who need sponsorship-aware job search automation.

## What it does

```
Discover (web) → Scrape every board found → Sponsorship Filter → Fit Score → Resume Tailor → Dashboard
```

1. **Discover** — Searches the web for recent postings on every public ATS (`site:` queries, past week by default) and reads job feeds. Every company board found is saved and re-checked on later runs. A company that posts its first job today gets picked up without you naming it.
2. **Scrape** — Pulls each discovered board through its public API (Greenhouse, Ashby, Lever, SmartRecruiters, Workable, BambooHR), in parallel. Boards that keep failing retire themselves.
3. **Sponsorship filter** — JD-first: if the description doesn't say "no sponsorship," you can apply. H-1B filing data is supplementary info, not a blocker. Toggle on/off.
4. **Fit scoring** — Weighted composite: stack match (30%), sponsorship (25%), experience (15%), location (15%), role type (15%).
5. **Resume tailoring** — Analyzes each JD, reorders your bullet points, selects projects, and leads with the right language. Builds PDF via pdflatex.
6. **Dashboard** — Web UI at localhost:8050. Shows the latest run by default (same jobs as the CLI printed); switch to "All saved jobs" for history.
7. **Tracking** — SQLite database tracks every job and every run. Re-running never resets a job you've marked applied.

## Quick start

```bash
git clone https://github.com/amudhan23/job-pipeline.git
cd job-pipeline
python -m venv .venv
source .venv/bin/activate
pip install -e .

# Seed H-1B sponsorship data (93 companies)
jobpipe seed-h1b

# Run the full pipeline
jobpipe run

# Open the dashboard (http://localhost:8050)
jobpipe dashboard

# Or: watch every board continuously (new postings within minutes)
jobpipe watch
```

## CLI commands

| Command | Description |
|---------|-------------|
| `jobpipe run` | Full pipeline: discover, scrape, filter, score, rank |
| `jobpipe run --recency day` | Only jobs posted in the last 24h (also narrows web search) |
| `jobpipe run --days 3` | Only jobs posted in the last 3 days |
| `jobpipe run --no-discover` | Skip web search; just re-scrape boards already found |
| `jobpipe scrape` | Discover + scrape only (no scoring) |
| `jobpipe boards` | List every company board discovered so far |
| `jobpipe sync` | Pull the latest cloud run's results to this machine |
| `jobpipe state-key --save` | Make the key that encrypts cloud-run data |
| `jobpipe watch` | Crawl every known board continuously; new postings print as they open |
| `jobpipe serve` | Crawler + dashboard in one process (what runs in the cloud) |
| `jobpipe dashboard` | Web dashboard at localhost:8050 |
| `jobpipe apply --handoff -j ID` | Fill a form, then you finish and submit it in the browser |
| `jobpipe cloud-push URL` | Copy your details, resume and answers to your cloud dashboard |
| `jobpipe status` | Latest run's stats and top jobs (`--all` for every saved job) |
| `jobpipe export jobs.csv` | Export to CSV |
| `jobpipe resume -j JOB_ID` | Generate tailored resume for a job |
| `jobpipe seed-h1b` | Seed H-1B database from data/h1b_seed.json |
| `jobpipe probe SLUG` | Check which ATS a company uses |
| `jobpipe discover` | Run web discovery on its own and show new companies |
| `jobpipe discover --queries-only` | Print the search queries to run in a browser |
| `jobpipe discover --debug` | Save raw search pages to `data/search_debug/` |

Add `-v` before the command for verbose logging: `jobpipe -v run`

## Configuration

Edit `config/profile.yaml`:

- **profile** — Your name, email, visa status, locations
- **filters** — Role keywords, exclude keywords (senior/staff/intern), experience range
- **stack** — Languages, frameworks, databases, cloud tools for scoring
- **scoring** — Weight for each scoring dimension
- **resume** — Your bullet points, project descriptions, language order
- **discovery** — Search recency, feeds, query delay, scrape concurrency
- **ats** — Optional pinned companies (always checked); not required
- **sponsorship_filter_enabled** — Toggle sponsorship filter on/off
- **sponsorship_blocklist** — Companies confirmed to not sponsor

## Sponsorship logic

The filter is JD-first:

1. Read the job description
2. If it explicitly says "no sponsorship" / "cannot sponsor" / "must be authorized without sponsorship" → skip
3. If no blocking language found → **eligible to apply**
4. H-1B filing data (from the seed database) is shown as supplementary info only

Set `sponsorship_filter_enabled: false` to disable all sponsorship checks.

## Resume tailoring

The generator analyzes each JD and detects role signals (backend, security, infrastructure, TypeScript, Rust, etc.), then:

- **Reorders IBM bullets** — CI/CD bullet leads for infra roles, debugging leads for security roles
- **Reorders Sahara bullets** — Integration bullet leads for backend, shipping leads for fullstack
- **Selects 3 of 4 projects** — req-guard for Rust/security, AccessMint for TypeScript/blockchain
- **Leads with the right language** — Rust first for Rust jobs, TypeScript for frontend, etc.

Requires `pdflatex` (install via `apt install texlive-latex-base` or `brew install --cask mactex`).

## Architecture

```
src/pipeline/
  discovery/         # Board registry + job feeds (web-first discovery)
  scrapers/          # Per-ATS API clients + site: web search
  filters/           # JD-first sponsorship filter with H-1B cache
  scoring/           # Weighted fit scorer (stack, experience, location, role type)
  resume/            # LaTeX template engine with marker-based reordering
  tracker/           # SQLite application tracker
  crawler/           # Real-time crawler: frontier policy, polite fetcher, sources, loop
  dashboard/         # Web UI (stdlib HTTP server + static/ SPA, token auth for remote)
  apply/             # Form filler (Playwright), answers, submit + handoff
  ai/                # Claude API: fit check, drafts, picks, budget
  autoapply.py       # Auto-apply settings, job picking, background runs
  orchestrator.py    # Coordinates all stages
  cli.py             # Click CLI
  models.py          # Core dataclasses (Job, ResumeConfig, Profile)
  utils/             # Text normalization, config loader

config/profile.yaml  # Your profile, filters, stack, company lists
templates/generic.tex # LaTeX resume template with markers
data/h1b_seed.json   # H-1B filing data for 93 tech companies
```

## Real-time crawler (`jobpipe watch`)

Applying in the first hours matters, so the crawler watches each company's
own job board — the source, before LinkedIn or aggregators copy it — and
records the minute a posting appears.

```
 feeds (5 min, 304 when unchanged) ─┐        ┌─ web discovery (every 6 h)
                                    ▼        ▼
                          frontier (registry.db: one row per board,
                          next_due_at · interval · posting rate · ETag · hash)
                                    │  most overdue first
                                    ▼
                    fetcher: per-host limits, backoff on 429/503,
                    conditional GET, retries
                                    │
                                    ▼
          source: light listing → body unchanged? stop. Else parse,
          fetch descriptions only for postings new to us (Greenhouse)
                                    │
                                    ▼
          tracker.observe_board: new → first_seen_at + "new" event,
          vanished → closed, back → reopened; score + sponsorship on arrival
                                    │
                                    ▼
          reschedule from the board's posting rate · status file · auto-apply hook
```

- **Revisit policy.** Each board keeps an estimate λ of relevant new postings
  per day (exponentially weighted). Its revisit interval is
  `10 min / √(λ + 0.02)`, clamped to 2 min – 3 h, ×0.6 during U.S. office hours
  and ×1.6 at night/weekends. Sublinear in λ is what minimises the age of
  missed postings (Cho & Garcia-Molina); quiet boards are still read about
  hourly. Workday/SmartRecruiters/BambooHR/Workable (a request per job) are
  never read more than every 30 min.
- **Cheap reads.** Conditional GETs (`If-None-Match`/`If-Modified-Since`) and a
  body hash mean an unchanged board costs one small request and no parsing.
  Greenhouse is polled without descriptions; a description is fetched once,
  when the posting first appears.
- **Politeness.** At most 4 requests in flight per host with a minimum gap;
  a 429/503 pauses that host (honouring `Retry-After`) and widens its gap.
- **Baseline.** Postings already on a board the first time it's read are
  saved but not announced as new. From the second read on, every new posting
  is an event with the minute it was seen; postings that disappear are closed,
  so the bot never tries a dead job.
- **Feeds speed it up.** A feed listing a fresh posting makes that company's
  board due immediately.
- Tuning lives under `crawler:` in `config/profile.yaml` (`feed_minutes`,
  `discover_hours`, `concurrency`, `per_host`, `policy: {base_minutes: …}`).

## Dashboard

`jobpipe dashboard` (or `jobpipe serve`) → http://localhost:8050

- **Live jobs** — open postings, freshest first, with “Just opened” cards for
  the last hour, minutes since each opened, score, AI fit and H-1B data.
  Optional browser notifications for new matches. Per job: **Fill form**
  (background, nothing sent), **Open and finish** (a browser opens with the
  form filled; you answer what's left and press Submit — your new answers are
  saved and the job is marked applied when the site confirms), Applied, Skip.
- **Needs you** — forms the bot prepared and what each still needs, plus every
  waiting question once per company. **Fill with AI** suggests answers from
  your resume; nothing is saved until you press Save.
- **Applied** — everything submitted, how (bot / you / marked), the tailored
  resume that was sent, a screenshot, and status (interview, offer, rejected).
- **Auto-apply** — switch on applying as new jobs open (prepare forms, or submit
  complete ones), with score, freshness, daily and per-company caps; run a
  batch now and watch its log.
- **Crawler** — boards watched, reads, unchanged share, new postings, what just
  opened or closed, and each board's estimated posting rate and revisit time.
- **My details** — your answers, resume, saved answers and AI settings.

## Run it in the cloud

The crawler only catches jobs while it runs, so put it on a small always-on
machine. `jobpipe serve` runs the crawler and the dashboard together; the
dashboard then needs an access token (it holds your private details), and a
visible browser isn't available there, so **Open and finish** stays a laptop
feature while auto-apply runs headless.

**Fly.io** (one 1 GB machine + 1 GB volume):

```bash
fly launch --no-deploy --copy-config      # pick an app name
fly volumes create jobpipe_data --size 1 --region ewr
fly secrets set JOBPIPE_DASHBOARD_TOKEN=$(openssl rand -hex 24)
fly deploy
jobpipe cloud-push https://<app>.fly.dev --with-ai-key   # your details, resume, answers
```

**Any VPS** (Docker): `echo "JOBPIPE_DASHBOARD_TOKEN=$(openssl rand -hex 24)" > .env && docker compose up -d --build`,
then reach it through an SSH tunnel or the Caddy service in `docker-compose.yml`.

The image installs Chromium and LaTeX (`--build-arg WITH_LATEX=0` for a
smaller image without tailored resumes) and keeps everything in `/app/data`.

## ATS coverage

Seven platforms are scraped directly through their public, no-login job APIs:

| Platform | API | Notes |
|----------|-----|-------|
| Greenhouse | `boards-api.greenhouse.io` | Full descriptions in list call |
| Ashby | `api.ashbyhq.com` | Full descriptions in list call |
| Lever | `api.lever.co` | Full descriptions in list call |
| SmartRecruiters | `api.smartrecruiters.com` | Paginated; detail call per job; feed is tier-dependent |
| Workable | `apply.workable.com` | Full descriptions with `details=true` |
| BambooHR | `{slug}.bamboohr.com` | Subdomain slug; detail call per job |
| Workday | `{tenant}.wdN.myworkdayjobs.com/wday/cxs/…` | Searches per role keyword; undocumented API, parsed defensively |

iCIMS, Jobvite, Jazz and LinkedIn have no public job API — discovery finds
companies on them and the feed supplies their roles, but they aren't scraped.

## How discovery works

There is no company list. Every `jobpipe run` starts from the web:

1. **Web search** — one `site:` query per (ATS domain × role keyword), e.g.
   `site:jobs.ashbyhq.com "software engineer" ("New York" OR "San Francisco")`,
   restricted to the past week. The company slug is pulled out of each result
   URL (`jobs.ashbyhq.com/`**`newco`**`/...`). Free via DuckDuckGo; set
   `SERPER_API_KEY` ([serper.dev](https://serper.dev), free tier) or
   `SERPAPI_KEY` for Google results. If the search engine starts throttling,
   discovery stops early and the run carries on with boards already known.
2. **Job feeds** — the SimplifyJobs New-Grad list (updated daily, ~500 companies
   a month). Feed jobs are kept directly — including iCIMS and custom-site
   roles we can't scrape — and every feed link that points at a scrapable board
   (including Workday) registers that company's whole board.
3. **Board registry** (`data/registry.db`) — every board found is saved and
   scraped on every later run, so a company found once keeps being checked.
   A board that fails 3 runs in a row (company left that ATS) is retired.

```bash
jobpipe boards                      # what's been found, newest first
jobpipe discover                    # run web discovery alone, show new companies
jobpipe discover --queries-only     # print the queries to run in a browser
jobpipe probe <company-name>        # check one company's ATS directly
```

To make sure a specific company is always checked, add it under `ats:` in
`config/profile.yaml` (optional).

Every run prints a one-line web search report, e.g.
`Web search (duckduckgo): 63/63 queries, 410 result links, 88 on job boards → 31 companies`,
plus a warning explaining why if it found nothing (blocked, no results, or
results that weren't job boards).

## Auto-apply

```bash
pip install -e '.[apply]'
playwright install --with-deps chromium      # one-time browser download (may ask for sudo)
jobpipe apply                                # practice: best 5 jobs scoring ≥ 70, sends nothing
jobpipe apply -j <job-id> --headed           # one job, with the browser visible
jobpipe apply --submit --headed              # submit complete forms, asking "Submit?" for each
jobpipe apply --submit --yes                 # fully automatic: submit every complete form
```

Fill in **My details** on the dashboard first (and upload your resume). For
each job the bot opens the real Greenhouse/Lever/Ashby form, finds questions
by their visible wording, fills what your details answer, attaches your
resume, and saves a screenshot + report to `data/private/applications/<job>/`.

It never ticks legal consent boxes and never guesses: questions it has no
answer for (essays, a blank salary) are listed as **needs you** and wait for
you on My details.

**Submitting** (`--submit`) only happens when a final check of the page finds
every required question answered, and — unless you pass `--yes` — after you
say yes while the filled form is open. With `--headed` you can also finish an
incomplete form by hand and type `s` to submit it. After clicking Submit it
waits for the site's confirmation; only confirmed applications are marked
**applied** (with any board/feed twin), so nothing is sent twice. A CAPTCHA
challenge or an emailed security code is handed to you in the open browser —
never worked around; without `--headed` that application stops unsent.

## AI drafts (Claude API, your credits)

```bash
pip install -e '.[ai]'
jobpipe ai key                 # paste your key (or set ANTHROPIC_API_KEY, or use My details → AI)
jobpipe ai status --check      # proves the key works with one tiny call
jobpipe ai budget 5            # monthly cap in USD (default $5); calls stop when it's reached
jobpipe ai model sonnet        # sonnet (default) · haiku (cheapest) · opus (strongest)
jobpipe ai draft -j <job-id> "Why this company?"
```

With a key saved, `jobpipe apply` drafts answers to **required open questions**
("Why Ande?", "What exceptional work have you done?") from your resume and the
job description: only facts in your resume, short and plain. It never writes
legal, eligibility, salary or demographic answers. Drafts are cached per company
and question (re-runs are free), shown in the terminal, and wait on My details
for you to edit — once saved there they're your own answer. `--submit --yes`
won't auto-send a form containing AI drafts unless you add `--trust-ai`.

**Fit check** (`jobpipe fit`, and automatically in `jobpipe apply`): Haiku reads
each job description against your resume and details for dealbreakers —
clearance, U.S. citizenship / "U.S. person", no sponsorship, years or degree
far above yours, seniority. A "skip" needs a quoted line from the JD; those
jobs are skipped and marked so, and apply moves on to the next one. Verdicts
show in the dashboard's **AI fit** column. A fraction of a cent per job.

**Closed questions** nobody answered (dropdowns, Yes/No, years of X): Claude
answers only when your resume or details settle it, citing the line it used
("not in your resume" for a skill you don't list); otherwise it's left for
you. Never legal, eligibility, demographic, referral or company-history
questions.

**Tailored resume per job** (`jobpipe resume -j <id>`, and automatically in
apply): Claude arranges your resume for the JD — which IBM / Sahara AI
bullets lead, which 3 projects, which languages first — choosing only from
`config/profile.yaml → resume` and never rewriting a bullet. The PDF is built
with pdflatex as `<First>_<Last>_Resume.pdf` and uploaded instead of your
saved resume (`--no-tailor` to skip). Needs `sudo apt install
texlive-latex-extra texlive-fonts-recommended`.

Every call is logged with its tokens and cost (`data/private/ai_usage.jsonl`);
spend is on My details and in `jobpipe ai status`. A Sonnet answer costs about
a cent.

## Scheduled runs (GitHub Actions)

`.github/workflows/pipeline.yml` runs the pipeline in the cloud — every 3
hours it re-checks every known board; twice a day it also searches the web
for new companies. Your laptop can be off.

Between runs the databases are saved as **one encrypted file** on the
`pipeline-data` branch (force-pushed, so it never grows). The repo is public,
so the workflow refuses to run without the encryption key, and its logs print
counts only — no job list.

One-time setup:

```bash
jobpipe state-key --save     # prints a key and saves it on this machine
```

1. GitHub → repo → **Settings → Secrets and variables → Actions → New
   repository secret**: name `JOBPIPE_STATE_KEY`, value = the printed key.
2. **Actions** tab → enable workflows if asked → **job-pipeline → Run workflow**.
3. When it finishes: `jobpipe sync` pulls the results to your laptop, then
   `jobpipe dashboard`.

Cloud runs use the `config/profile.yaml` on GitHub, so push config changes.
Optional secret: `SERPER_API_KEY` for better web search (datacenter IPs are
often blocked by DuckDuckGo; the run carries on with known boards if so).

## Freshness

`--recency day|week|month` (or `--days N`) keeps only jobs **posted** in that
window — dates come from each ATS (Greenhouse `first_published`, Ashby
`publishedAt`, Lever `createdAt`, SmartRecruiters `releasedDate`, Workable
`published_on`, BambooHR detail, feed `date_posted`). Jobs with no posting
date are dropped when a window is set. Set `filters.max_job_age_days` to make
a window the default. The dashboard has a matching "Posted" column and filter.

## Requirements

- Python 3.11+
- pdflatex (for resume generation)
- Internet access (to hit ATS public APIs)

## License

MIT
