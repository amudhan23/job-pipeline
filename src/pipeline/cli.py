"""CLI entry point for the job pipeline."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from pipeline.orchestrator import Pipeline
from pipeline.tracker import Tracker
from pipeline.utils.config import load_config

console = Console()


@click.group()
@click.option("--config", "-c", default=None, help="Path to config YAML")
@click.option("--verbose", "-v", is_flag=True, help="Verbose logging")
@click.pass_context
def main(ctx: click.Context, config: str | None, verbose: bool) -> None:
    """Job application pipeline — scrape, filter, score, and apply."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    ctx.ensure_object(dict)
    ctx.obj["config"] = load_config(config)


@main.command()
@click.option("--limit", "-n", default=20, help="Max jobs to show")
@click.pass_context
def scrape(ctx: click.Context, limit: int) -> None:
    """Scrape jobs from all configured ATS platforms."""
    config = ctx.obj["config"]
    pipeline = Pipeline(config)
    jobs = asyncio.run(pipeline.run_scrape())
    console.print(f"\n[bold green]Scraped {len(jobs)} matching jobs[/bold green]\n")

    if jobs:
        table = _jobs_table(jobs[:limit])
        console.print(table)


@main.command()
@click.option("--limit", "-n", default=20, help="Max jobs to show")
@click.pass_context
def run(ctx: click.Context, limit: int) -> None:
    """Run full pipeline: scrape → filter → score → rank."""
    config = ctx.obj["config"]
    pipeline = Pipeline(config)
    jobs = asyncio.run(pipeline.run_full())
    console.print(f"\n[bold green]Pipeline complete: {len(jobs)} jobs processed[/bold green]\n")

    if jobs:
        table = _jobs_table(jobs[:limit])
        console.print(table)


@main.command()
@click.option("--limit", "-n", default=20, help="Max jobs to show")
@click.option("--status", "-s", default=None, help="Filter by status")
@click.pass_context
def status(ctx: click.Context, limit: int, status: str | None) -> None:
    """Show tracked jobs and stats."""
    tracker = Tracker()

    # Show stats
    stats = tracker.get_stats()
    console.print("\n[bold]Pipeline Stats[/bold]")
    for key, val in stats.items():
        console.print(f"  {key}: {val}")

    # Show top jobs
    jobs = tracker.get_top_jobs(limit=limit, status=status)
    if jobs:
        console.print()
        table = Table(title="Top Jobs")
        table.add_column("Score", style="cyan", width=6)
        table.add_column("Company", style="green", width=20)
        table.add_column("Title", width=30)
        table.add_column("Location", width=15)
        table.add_column("H-1B", width=8)
        table.add_column("Status", style="yellow", width=12)

        for j in jobs:
            table.add_row(
                f"{j['fit_score']:.0f}",
                j["company"][:20],
                j["title"][:30],
                j["location"][:15],
                j["sponsorship"],
                j["status"],
            )
        console.print(table)


@main.command()
@click.argument("output", default="jobs_export.csv")
@click.pass_context
def export(ctx: click.Context, output: str) -> None:
    """Export tracked jobs to CSV."""
    tracker = Tracker()
    tracker.export_csv(Path(output))
    console.print(f"[green]Exported to {output}[/green]")


@main.command()
@click.option("--file", "-f", default=None, help="Path to JSON file (default: data/h1b_seed.json)")
@click.pass_context
def seed_h1b(ctx: click.Context, file: str | None) -> None:
    """Seed the H-1B database from a JSON file."""
    from pipeline.filters.sponsorship import SponsorshipFilter

    sf = SponsorshipFilter()
    seed_path = Path(file) if file else Path(__file__).resolve().parents[2] / "data" / "h1b_seed.json"
    if not seed_path.exists():
        console.print(f"[red]File not found: {seed_path}[/red]")
        return

    count = sf.import_from_json(seed_path)
    console.print(f"[bold green]Imported {count} H-1B records[/bold green]")


@main.command()
@click.option("--port", "-p", default=8050, help="Port to run on")
@click.pass_context
def dashboard(ctx: click.Context, port: int) -> None:
    """Open the web dashboard for browsing jobs."""
    from pipeline.dashboard.app import run_dashboard
    run_dashboard(port=port)


@main.command()
@click.option("--job-id", "-j", required=True, help="Job ID to generate resume for")
@click.option("--output-dir", "-o", default="output", help="Output directory for PDFs")
@click.pass_context
def resume(ctx: click.Context, job_id: str, output_dir: str) -> None:
    """Generate a tailored resume for a tracked job."""
    from pipeline.resume.generator import ResumeGenerator
    from pipeline.models import Job

    config = ctx.obj["config"]
    template_path = Path(__file__).resolve().parents[2] / "templates" / "generic.tex"

    if not template_path.exists():
        console.print(f"[red]Template not found: {template_path}[/red]")
        return

    # Get job from tracker
    tracker = Tracker()
    job_data = tracker.get_job(job_id)
    if not job_data:
        console.print(f"[red]Job {job_id} not found in tracker[/red]")
        return

    gen = ResumeGenerator(template_path, config)
    job = Job(**{k: v for k, v in job_data.items() if k in Job.__dataclass_fields__})
    result = gen.build(job, Path(output_dir))
    if result:
        console.print(f"[bold green]Resume generated: {result}[/bold green]")
    else:
        console.print("[red]Resume generation failed — is pdflatex installed?[/red]")


@main.command()
@click.argument("company_slug")
@click.pass_context
def probe(ctx: click.Context, company_slug: str) -> None:
    """Probe which ATS a company uses."""
    from pipeline.scrapers.discover import detect_ats
    result = asyncio.run(detect_ats(company_slug))
    if result:
        console.print(f"[green]{company_slug}[/green] uses [bold]{result}[/bold]")
    else:
        console.print(f"[yellow]{company_slug}[/yellow] — no Greenhouse/Ashby/Lever board found")


@main.command()
@click.option("--platform", "-p", multiple=True,
              help="Limit to platforms (greenhouse/ashby/lever/smartrecruiters/...)")
@click.option("--no-location", is_flag=True, help="Drop location filter from queries")
@click.option("--queries-only", is_flag=True,
              help="Just print the search queries (no API call)")
@click.option("--add", is_flag=True, help="Add discovered slugs to config/profile.yaml")
@click.pass_context
def discover(ctx: click.Context, platform: tuple[str, ...], no_location: bool,
             queries_only: bool, add: bool) -> None:
    """Discover new company slugs via search-engine `site:` queries.

    With no search API key set, prints the boolean queries to paste into
    Google yourself. With SERPER_API_KEY or SERPAPI_KEY set, runs them and
    extracts company slugs automatically.
    """
    from pipeline.scrapers.search_discover import (
        generate_queries, discover as run_discover, _get_search_backend, ATS_DOMAINS,
    )

    config = ctx.obj["config"]
    platforms = list(platform) if platform else None
    include_location = not no_location

    # Manual mode: print queries
    if queries_only or not _get_search_backend():
        if not queries_only:
            console.print(
                "[yellow]No SERPER_API_KEY / SERPAPI_KEY found — "
                "printing queries to run manually.[/yellow]\n"
            )
        queries = generate_queries(config, platforms, include_location)
        scrapable = {a.name for a in ATS_DOMAINS if a.scrapable}
        for plat, query in queries:
            tag = "" if plat in scrapable else " [dim](no scraper yet)[/dim]"
            console.print(f"[bold cyan]{plat}[/bold cyan]{tag}")
            console.print(f"  {query}\n")
        console.print(
            "[dim]Paste these into Google. Company slug is the path after the "
            "domain, e.g. boards.greenhouse.io/[bold]stripe[/bold].[/dim]"
        )
        return

    # Automated mode: run searches
    console.print("[bold]Discovering slugs via search API...[/bold]\n")
    result = asyncio.run(run_discover(config, platforms, include_location))

    if result.total() == 0:
        console.print("[yellow]No slugs discovered.[/yellow]")
        return

    for plat in sorted(result.by_platform):
        slugs = sorted(result.by_platform[plat])
        console.print(f"[bold cyan]{plat}[/bold cyan] ({len(slugs)} found):")
        console.print(f"  {', '.join(slugs)}\n")

    console.print(f"[bold green]Discovered {result.total()} slugs total[/bold green]")

    if add:
        _merge_slugs_into_config(result)
        console.print("[green]Added new slugs to config/profile.yaml[/green]")


def _merge_slugs_into_config(result) -> None:
    """Merge discovered slugs into the config YAML (scrapable platforms only)."""
    import yaml
    from pipeline.scrapers.search_discover import ATS_DOMAINS

    config_path = Path(__file__).resolve().parents[2] / "config" / "profile.yaml"
    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    scrapable = {a.name for a in ATS_DOMAINS if a.scrapable}
    cfg.setdefault("ats", {})
    for plat, slugs in result.by_platform.items():
        if plat not in scrapable:
            continue
        existing = set(cfg["ats"].get(plat) or [])
        merged = sorted(existing | slugs)
        cfg["ats"][plat] = merged

    with open(config_path, "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False, default_flow_style=False)


def _jobs_table(jobs: list) -> Table:
    """Create a rich table for jobs display."""
    table = Table(title="Matched Jobs")
    table.add_column("Score", style="cyan", width=6)
    table.add_column("Company", style="green", width=20)
    table.add_column("Title", width=35)
    table.add_column("Location", width=15)
    table.add_column("H-1B", width=8)
    table.add_column("Platform", width=10)

    for j in jobs:
        # Show JD block status alongside H-1B filing data
        h1b_info = j.sponsorship.value
        if j.jd_blocks_sponsorship:
            h1b_info = "⛔ NO"
        table.add_row(
            f"{j.fit_score:.0f}",
            j.company[:20],
            j.title[:35],
            j.location[:15],
            h1b_info,
            j.platform.value,
        )
    return table


if __name__ == "__main__":
    main()
