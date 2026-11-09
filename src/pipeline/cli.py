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
