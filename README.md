# job-pipeline

Automated job application pipeline for software engineers. Scrapes jobs from Greenhouse, Ashby, and Lever APIs, filters by H-1B sponsorship (JD-first), scores fit 0-100, and generates tailored LaTeX resumes per job.

Built for international students and H-1B candidates who need sponsorship-aware job search automation.

## What it does

```
Scrape (44 companies) → Sponsorship Filter → Fit Score 0-100 → Resume Tailor → Dashboard
```

1. **Scrape** — Hits public ATS APIs (Greenhouse, Ashby, Lever, SmartRecruiters, Workable, BambooHR). No auth needed.
2. **Sponsorship filter** — JD-first: if the description doesn't say "no sponsorship," you can apply. H-1B filing data is supplementary info, not a blocker. Toggle on/off.
3. **Fit scoring** — Weighted composite: stack match (30%), sponsorship (25%), experience (15%), location (15%), role type (15%).
4. **Resume tailoring** — Analyzes each JD, reorders your bullet points, selects projects, and leads with the right language. Builds PDF via pdflatex.
5. **Dashboard** — Web UI at localhost:8050 for browsing, filtering, and applying.
6. **Tracking** — SQLite database tracks every job through the pipeline.

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
| `jobpipe run` | Full pipeline: scrape, filter, score, rank |
| `jobpipe scrape` | Scrape only (no scoring) |
| `jobpipe dashboard` | Web dashboard at localhost:8050 |
| `jobpipe status` | Show pipeline stats and top jobs |
| `jobpipe export jobs.csv` | Export to CSV |
| `jobpipe resume -j JOB_ID` | Generate tailored resume for a job |
| `jobpipe seed-h1b` | Seed H-1B database from data/h1b_seed.json |
| `jobpipe probe SLUG` | Check which ATS a company uses |
| `jobpipe discover` | Find new company slugs via `site:` search (free, DuckDuckGo) |
| `jobpipe discover --add` | Discover and auto-add slugs to your config |
| `jobpipe discover --queries-only` | Print the search queries to run in a browser |

Add `-v` before the command for verbose logging: `jobpipe -v run`

## Configuration

Edit `config/profile.yaml`:

- **profile** — Your name, email, visa status, locations
- **filters** — Role keywords, exclude keywords (senior/staff/intern), experience range
- **stack** — Languages, frameworks, databases, cloud tools for scoring
- **scoring** — Weight for each scoring dimension
- **resume** — Your bullet points, project descriptions, language order
- **ats** — Company slugs for each ATS platform
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
  scrapers/          # Greenhouse, Ashby, Lever API clients + ATS discovery
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

## Finding more companies

The pipeline ships with a starter set of slugs. To find every company on a
platform, use search-based discovery (the a16z `site:` technique):

```bash
# Free — uses DuckDuckGo, no API key
jobpipe discover

# Discover and add new slugs straight into config/profile.yaml
jobpipe discover --add

# Just print the queries to run in Google yourself
jobpipe discover --queries-only

# Limit to specific platforms
jobpipe discover -p greenhouse -p ashby
```

For higher-quality Google results, set `SERPER_API_KEY` ([serper.dev](https://serper.dev),
free tier) or `SERPAPI_KEY` — discovery uses it automatically when present.

You can also check one company directly:

```bash
jobpipe probe <company-name>
```

## Requirements

- Python 3.11+
- pdflatex (for resume generation)
- Internet access (to hit ATS public APIs)

## License

MIT
