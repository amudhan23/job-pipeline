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
6. **Dashboard** — Web UI at localhost:8050 for browsing, filtering, and applying.
7. **Tracking** — SQLite database tracks every job through the pipeline.

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

# Open the dashboard
jobpipe dashboard
```

## CLI commands

| Command | Description |
|---------|-------------|
| `jobpipe run` | Full pipeline: discover, scrape, filter, score, rank |
| `jobpipe run --recency day` | Only search the web for postings from the last 24h |
| `jobpipe run --no-discover` | Skip web search; just re-scrape boards already found |
| `jobpipe scrape` | Discover + scrape only (no scoring) |
| `jobpipe boards` | List every company board discovered so far |
| `jobpipe dashboard` | Web dashboard at localhost:8050 |
| `jobpipe status` | Show pipeline stats and top jobs |
| `jobpipe export jobs.csv` | Export to CSV |
| `jobpipe resume -j JOB_ID` | Generate tailored resume for a job |
| `jobpipe seed-h1b` | Seed H-1B database from data/h1b_seed.json |
| `jobpipe probe SLUG` | Check which ATS a company uses |
| `jobpipe discover` | Run web discovery on its own and show new companies |
| `jobpipe discover --queries-only` | Print the search queries to run in a browser |

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
  dashboard/         # Web UI (zero dependencies, stdlib HTTP server)
  orchestrator.py    # Coordinates all stages
  cli.py             # Click CLI
  models.py          # Core dataclasses (Job, ResumeConfig, Profile)
  utils/             # Text normalization, config loader

config/profile.yaml  # Your profile, filters, stack, company lists
templates/generic.tex # LaTeX resume template with markers
data/h1b_seed.json   # H-1B filing data for 93 tech companies
```

## ATS coverage

Six public, no-login ATS platforms are scraped directly:

| Platform | API | Notes |
|----------|-----|-------|
| Greenhouse | `boards-api.greenhouse.io` | Full descriptions in list call |
| Ashby | `api.ashbyhq.com` | Full descriptions in list call |
| Lever | `api.lever.co` | Full descriptions in list call |
| SmartRecruiters | `api.smartrecruiters.com` | Paginated; detail call per job; feed is tier-dependent |
| Workable | `apply.workable.com` | Full descriptions with `details=true` |
| BambooHR | `{slug}.bamboohr.com` | Subdomain slug; detail call per job |

Login/key-gated platforms (iCIMS, Jobvite, Jazz, Workday, LinkedIn) are
discovery-only for now — `discover` will find companies on them, but there's
no scraper yet.

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
   a month). Feed jobs are kept directly — including Workday and iCIMS roles we
   can't scrape — and every feed link that points at a scrapable board
   registers that company's whole board.
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

## Requirements

- Python 3.11+
- pdflatex (for resume generation)
- Internet access (to hit ATS public APIs)

## License

MIT
