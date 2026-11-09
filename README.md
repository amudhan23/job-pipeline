# Job Pipeline

Automated job application pipeline with H-1B sponsorship filtering and LaTeX resume generation.

## Stages

1. **Scraper** — Pull jobs from Greenhouse, Ashby, and Lever public APIs
2. **Sponsorship Filter** — Cross-reference companies against H-1B filing databases
3. **Fit Scorer** — Score jobs 0–100 against your profile (stack, experience, location, role type)
4. **Resume Generator** — Auto-tailor LaTeX resume per job description
5. **Tracker** — SQLite database tracking all scraped jobs, scores, and application status

## Quick Start

```bash
# Install
pip install -e ".[dev]"

# Run full pipeline
jobpipe run

# Just scrape
jobpipe scrape

# Check a company's ATS
jobpipe probe stripe

# View tracked jobs
jobpipe status

# Export to CSV
jobpipe export jobs.csv
```

## Configuration

Edit `config/profile.yaml` to customize:
- Your tech stack and preferences
- Role keywords and location filters
- Scoring weights
- Company lists per ATS platform
- Resume bullet ordering rules

## Project Structure

```
src/pipeline/
├── cli.py              # CLI entry point
├── orchestrator.py     # Pipeline coordinator
├── models.py           # Data models
├── scrapers/
│   ├── greenhouse.py   # Greenhouse API scraper
│   ├── ashby.py        # Ashby API scraper
│   ├── lever.py        # Lever API scraper
│   └── discover.py     # ATS detection utilities
├── filters/
│   └── sponsorship.py  # H-1B sponsorship filter
├── scoring/
│   └── scorer.py       # Job fit scorer
├── resume/
│   └── generator.py    # LaTeX resume tailoring
├── tracker/
│   └── db.py           # SQLite application tracker
└── utils/
    ├── config.py       # YAML config loader
    └── text.py         # Text normalization
```

## Adding Companies

The pipeline ships with known company slugs for each ATS. To add more:

```bash
# Check which ATS a company uses
jobpipe probe <company-name>

# Then add the slug to config/profile.yaml under the right ATS section
```

## H-1B Data

Import H-1B filing data as JSON:
```python
from pipeline.filters import SponsorshipFilter
f = SponsorshipFilter()
f.import_from_json("h1b_data.json")
```

Expected format: `[{"company_name": "...", "total_filings": N, "recent_filings": N, ...}]`

## Requirements

- Python 3.11+
- pdflatex (for resume generation)
- Your LaTeX resume template (place as `templates/generic.tex`)
