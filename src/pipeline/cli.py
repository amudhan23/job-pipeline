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


_RECENCY = click.Choice(["day", "week", "month", "year"])


def _freshness_options(f):
    f = click.option("--days", type=int, default=None,
                     help="Only jobs posted in the last N days (overrides --recency window)")(f)
    f = click.option("--recency", type=_RECENCY, default=None,
                     help="Only jobs posted in the last day/week/month; also narrows web search")(f)
    f = click.option("--no-discover", is_flag=True,
                     help="Skip web discovery; only re-scrape known boards")(f)
    return click.option("--limit", "-n", default=20, help="Max jobs to show")(f)


@main.command()
@_freshness_options
@click.pass_context
def scrape(ctx: click.Context, limit: int, no_discover: bool, recency: str | None,
           days: int | None) -> None:
    """Discover boards from the web and scrape them (no scoring)."""
    pipeline = Pipeline(ctx.obj["config"])
    jobs = asyncio.run(pipeline.run_scrape(
        discover=not no_discover, recency=recency, max_age_days=days))
    _print_run_summary(pipeline)
    console.print(f"\n[bold green]Scraped {len(jobs)} matching jobs[/bold green]\n")
    if jobs and limit > 0:  # --limit 0: counts only (used by public CI logs)
        console.print(_jobs_table(jobs[:limit]))


@main.command()
@_freshness_options
@click.pass_context
def run(ctx: click.Context, limit: int, no_discover: bool, recency: str | None,
        days: int | None) -> None:
    """Full pipeline: discover from the web → scrape → filter → score → rank."""
    pipeline = Pipeline(ctx.obj["config"])
    jobs = asyncio.run(pipeline.run_full(
        discover=not no_discover, recency=recency, max_age_days=days))
    _print_run_summary(pipeline)
    console.print(f"\n[bold green]Pipeline complete: {len(jobs)} jobs processed[/bold green]\n")
    if jobs and limit > 0:  # --limit 0: counts only (used by public CI logs)
        console.print(_jobs_table(jobs[:limit]))


def _print_search_diagnostics(r) -> None:
    """One line saying exactly what web search did, so 0 results is never a mystery."""
    line = (f"[bold]Web search ({r.backend}):[/bold] {r.queries_run}/{r.queries_total} queries, "
            f"{r.result_links} result links, {r.ats_links} on job boards → {r.total()} companies")
    console.print(line)
    if r.rate_limited:
        console.print(f"  [yellow]{r.backend} started blocking after {r.queries_run} queries. "
                      "Try again later, raise discovery.search_delay, or set SERPER_API_KEY.[/yellow]")
    elif r.queries_run and not r.result_links:
        console.print("  [yellow]The search engine returned no links at all. Run "
                      "`jobpipe discover --debug` and share data/search_debug/ to diagnose.[/yellow]")
    elif r.result_links and not r.ats_links:
        console.print("  [yellow]Links came back but none pointed at a job board — "
                      "try --recency month for a wider net.[/yellow]")


def _print_run_summary(pipeline: Pipeline) -> None:
    s = pipeline.stats
    console.print()
    if s.search is not None:
        _print_search_diagnostics(s.search)
    if s.new_boards:
        found = ", ".join(f"{n} from {src}" for src, n in s.new_boards.items())
        console.print(f"[bold]New companies discovered:[/bold] {found}")
    scraped = sum(s.boards_scraped.values())
    if scraped:
        by_plat = ", ".join(f"{p} {n}" for p, n in s.boards_scraped.items())
        console.print(f"[bold]Boards scraped:[/bold] {scraped} ({by_plat})"
                      + (f", {s.boards_failed} failed" if s.boards_failed else ""))
    else:
        console.print("[bold]Boards scraped:[/bold] none yet — run without --no-discover "
                      "to find some")
    console.print(f"[bold]Jobs:[/bold] {s.jobs_from_boards} from boards, "
                  f"{s.jobs_from_feeds} from feeds, {s.duplicates_dropped} duplicates dropped"
                  + (f", {s.closed_dropped} closed (feed out of date)" if s.closed_dropped else ""))
    if s.max_age_days:
        console.print(f"[bold]Freshness (last {s.max_age_days}d):[/bold] "
                      f"{s.too_old_dropped} older jobs dropped"
                      + (f", {s.undated_dropped} with no posting date dropped"
                         if s.undated_dropped else ""))


@main.command()
@click.option("--limit", "-n", default=20, help="Max jobs to show")
@click.option("--status", "-s", default=None, help="Filter by status")
@click.option("--all", "show_all", is_flag=True, help="Every saved job, not just the latest run")
@click.pass_context
def status(ctx: click.Context, limit: int, status: str | None, show_all: bool) -> None:
    """Show tracked jobs and stats (latest run by default)."""
    tracker = Tracker()
    run = tracker.latest_run()
    run_id = None if (show_all or not run) else run["id"]

    if run_id is not None:
        win = f"last {run['max_age_days']}d" if run["max_age_days"] else "any date"
        console.print(f"\n[bold]Latest run[/bold] {run['started_at']} · {win} · "
                      f"{run['job_count']} jobs  [dim](--all for every saved job)[/dim]")
    else:
        console.print("\n[bold]All saved jobs[/bold]")

    stats = tracker.get_stats(run_id)
    for key, val in stats.items():
        console.print(f"  {key}: {val}")

    # Show top jobs
    jobs = tracker.get_top_jobs(limit=limit, status=status, run_id=run_id)
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
@click.option("--recency", type=_RECENCY, default="week", help="How fresh postings must be")
@click.option("--queries-only", is_flag=True,
              help="Just print the search queries to run in a browser")
@click.option("--debug", is_flag=True,
              help="Save raw search pages to data/search_debug/ for inspection")
@click.option("--max-queries", type=int, default=None, help="Stop after N queries")
@click.pass_context
def discover(ctx: click.Context, platform: tuple[str, ...], no_location: bool,
             recency: str, queries_only: bool, debug: bool, max_queries: int | None) -> None:
    """Search the web for recent postings and record the companies found.

    `jobpipe run` already does this every time — use this command to run
    discovery on its own and see what turns up. New boards go straight into
    the board registry (see `jobpipe boards`).
    """
    from pipeline.discovery import BoardRegistry
    from pipeline.scrapers.search_discover import (
        generate_queries, discover as run_discover, ATS_DOMAINS,
    )

    config = ctx.obj["config"]
    platforms = list(platform) if platform else None
    include_location = not no_location

    if queries_only:
        scrapable = {a.name for a in ATS_DOMAINS if a.scrapable}
        for plat, query in generate_queries(config, platforms, include_location):
            tag = "" if plat in scrapable else " [dim](no scraper yet)[/dim]"
            console.print(f"[bold cyan]{plat}[/bold cyan]{tag}\n  {query}\n")
        console.print("[dim]Paste into Google/DuckDuckGo. The company slug is the path "
                      "after the domain, e.g. boards.greenhouse.io/[bold]stripe[/bold].[/dim]")
        return

    from pipeline.scrapers import search_discover
    if debug:
        debug_dir = Path(__file__).resolve().parents[2] / "data" / "search_debug"
        search_discover.DEBUG_DIR = debug_dir
        console.print(f"[dim]Saving raw search pages to {debug_dir}[/dim]")

    result = asyncio.run(run_discover(
        config, platforms, include_location, recency=recency,
        delay=config.get("discovery", {}).get("search_delay", 2.0),
        max_queries=max_queries,
    ))
    _print_search_diagnostics(result)
    registry = BoardRegistry()
    known = {(b.platform, b.slug) for b in registry.all()}

    if result.total() == 0:
        return

    new_total = 0
    for plat in sorted(result.by_platform):
        slugs = sorted(result.by_platform[plat])
        new = [s for s in slugs if (plat, s) not in known]
        new_total += len(new)
        registry.add_many({(plat, s) for s in slugs}, source="web")
        console.print(f"[bold cyan]{plat}[/bold cyan]: {len(slugs)} found, "
                      f"[green]{len(new)} new[/green]")
        if new:
            console.print(f"  {', '.join(new)}")
    console.print(f"\n[bold green]{new_total} new companies added to the registry[/bold green]")


@main.command()
@click.option("--platform", "-p", default=None, help="Only show one platform")
@click.option("--limit", "-n", default=50, help="Max rows to show (newest first)")
@click.pass_context
def boards(ctx: click.Context, platform: str | None, limit: int) -> None:
    """Show the company boards the pipeline has discovered."""
    from pipeline.discovery import BoardRegistry
    from pipeline.discovery.registry import MAX_FAILURES

    registry = BoardRegistry()
    rows = [b for b in registry.all() if not platform or b.platform == platform]
    counts = registry.counts()
    console.print(f"\n[bold]{sum(counts.values())} active boards[/bold] — "
                  + ", ".join(f"{p} {n}" for p, n in sorted(counts.items())))

    table = Table(title="Discovered boards (newest first)")
    table.add_column("Company", style="green")
    table.add_column("Platform", style="cyan")
    table.add_column("Slug", style="dim")
    table.add_column("Found via")
    table.add_column("First seen")
    table.add_column("Matching jobs", justify="right")
    table.add_column("Status")
    for b in rows[:limit]:
        status = "[red]retired[/red]" if b.fail_count >= MAX_FAILURES else (
            f"[yellow]{b.fail_count} fails[/yellow]" if b.fail_count else "ok")
        table.add_row(b.display_name, b.platform, b.slug, b.source, b.first_seen[:10],
                      str(b.last_job_count), status)
    console.print(table)


def _one_per_application(jobs: list[dict]) -> list[dict]:
    """The same posting can arrive twice (its board and a feed, with slightly
    different titles). Keep the first — they're sorted best score first — so
    one application is never filled twice."""
    import re as _re
    from pipeline.apply.filler import application_url
    seen: set = set()
    out = []
    for j in jobs:
        title = _re.sub(r"[^a-z0-9]+", " ", (j.get("title") or "").lower()).strip()
        keys = {("url", application_url(j)), ("title", (j.get("company") or "").lower(), title)}
        if keys & seen:
            continue
        seen |= keys
        out.append(j)
    return out


@main.command()
@click.option("--job-id", "-j", multiple=True, help="Specific job(s) to apply to")
@click.option("--top", "-n", default=5, help="Otherwise: best N jobs from the latest run")
@click.option("--min-score", default=70.0, help="Only jobs scoring at least this")
@click.option("--headed", is_flag=True, help="Show the browser so you can watch")
@click.option("--submit", is_flag=True, help="Actually submit (not available yet)")
def apply(job_id: tuple[str, ...], top: int, min_score: float, headed: bool, submit: bool) -> None:
    """Fill job applications from your saved details (practice mode: never submits).

    For each job it opens the real application form, fills every question it
    has an answer for, attaches your resume, takes a screenshot, and lists
    anything that needs you. Reports: data/private/applications/<job>/.
    """
    import re as _re
    from pipeline import applicant
    from pipeline.apply.filler import application_url, practice_apply

    if submit:
        raise click.ClickException(
            "Submitting isn't switched on yet — run practice mode, check the screenshots, "
            "and we'll enable it once the forms come out right.")
    try:
        import playwright  # noqa: F401
    except ImportError:
        raise click.ClickException(
            "Auto-apply needs a browser: pip install -e '.[apply]' && playwright install --with-deps chromium")

    details = applicant.load()
    missing = applicant.missing_required(details)
    if missing:
        raise click.ClickException(
            f"Fill these on the dashboard's My details page first: {', '.join(missing)}")
    resume = applicant.PRIVATE_DIR / applicant.RESUME_FILE
    if not resume.exists():
        raise click.ClickException("Upload your resume on the dashboard's My details page first.")

    tracker = Tracker()
    if job_id:
        jobs = [j for j in (tracker.get_job(i) for i in job_id) if j]
    else:
        run = tracker.latest_run()
        candidates = tracker.get_jobs(run["id"] if run else None)
        jobs = [j for j in candidates
                if j["fit_score"] >= min_score and application_url(j)
                and j["status"] not in ("applied", "interview", "offer", "rejected", "skipped")]
        jobs = _one_per_application(jobs)[:top]
    if not jobs:
        raise click.ClickException("No matching jobs (auto-apply supports Greenhouse, Lever, Ashby).")

    out_root = applicant.PRIVATE_DIR / "applications"
    saved = applicant.load_answers()
    console.print(f"\n[bold]Practice mode[/bold] — filling {len(jobs)} application(s); "
                  "nothing will be submitted.\n")
    new_questions = 0
    for job in jobs:
        console.print(f"[cyan]{job['company']}[/cyan] — {job['title']}  [dim]({job['fit_score']:.0f})[/dim]")
        folder = out_root / _re.sub(r"[^A-Za-z0-9_.-]", "_", job["id"])
        report = practice_apply(job, details, resume, folder, headless=not headed, saved=saved)
        if report.closed:
            # Don't offer it again
            from pipeline.models import ApplicationStatus
            tracker.update_status(job["id"], ApplicationStatus.SKIPPED, notes="posting closed")
        if report.error:
            console.print(f"  [red]✗ {report.error}[/red]")
            if report.screenshot:
                console.print(f"    screenshot: {report.screenshot}")
            console.print()
            continue
        console.print(f"  [green]✓ filled {len(report.filled)} field(s)[/green]")
        if report.resume_attached:
            console.print("  [green]📎 resume attached[/green]")
        else:
            console.print("  [yellow]! resume NOT attached — no resume upload field was recognised[/yellow]")
        pending = []
        for f in report.needs_attention:
            console.print(f"  [yellow]! needs you:[/yellow] {f.label[:80]}  [dim]({f.source})[/dim]")
            # Only questions you can answer once on My details — not consent
            # boxes, uploads, or answers the form refused to keep
            if f.source.startswith(("no saved answer", "no option matching")) and f.kind != "file":
                pending.append({"question": applicant.normalize_question(f.label, job["company"]),
                                "label": f.label, "options": f.options, "company": job["company"]})
        applicant.add_pending(pending)
        new_questions += len(pending)
        if report.captcha:
            console.print("  [dim]this site runs a bot check (CAPTCHA) on submit — "
                          "submitting may need you to tick a box[/dim]")
        verdict = "[green]ready to submit[/green]" if report.ready_to_submit \
            else "[yellow]not ready — see above[/yellow]"
        console.print(f"  {verdict} · screenshot: {report.screenshot}\n")
    if new_questions:
        console.print("[bold]Answer the open questions once[/bold] on the dashboard → My details → "
                      "“Questions waiting for you”, then run `jobpipe apply` again.")


@main.command("state-key")
@click.option("--save", is_flag=True, help="Also save it to ~/.config/jobpipe/state_key")
def state_key(save: bool) -> None:
    """Make the key that encrypts the job data stored by cloud runs."""
    from pipeline.state import new_key, save_key
    key = new_key()
    console.print(f"\n[bold]{key}[/bold]\n")
    if save:
        console.print(f"Saved to {save_key(key)}")
    console.print("Add it in GitHub → your repo → Settings → Secrets and variables → Actions "
                  "→ New repository secret, named [bold]JOBPIPE_STATE_KEY[/bold].")
    console.print("[dim]Keep it private: anyone with this key can read your saved job data.[/dim]")


@main.group()
def state() -> None:
    """Export/import the encrypted job databases (used by cloud runs)."""


@state.command("export")
@click.option("--out", "out_path", required=True, type=click.Path(dir_okay=False))
def state_export(out_path: str) -> None:
    from pipeline.state import StateError, export_state, load_key
    try:
        Path(out_path).write_bytes(export_state(load_key()))
    except StateError as e:
        raise click.ClickException(str(e))
    console.print(f"Encrypted state written to {out_path}")


@state.command("import")
@click.option("--in", "in_path", required=True, type=click.Path(exists=True, dir_okay=False))
def state_import(in_path: str) -> None:
    from pipeline.state import StateError, import_state, load_key
    try:
        restored = import_state(Path(in_path).read_bytes(), load_key())
    except StateError as e:
        raise click.ClickException(str(e))
    console.print(f"Restored: {', '.join(restored) or 'nothing'}")


@main.command()
def sync() -> None:
    """Pull the latest cloud run's results into this machine (for the dashboard)."""
    import subprocess
    from pipeline.state import REPO_ROOT, StateError, import_state, load_key

    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(REPO_ROOT), *args], capture_output=True)

    try:
        key = load_key()
    except StateError as e:
        raise click.ClickException(str(e))
    fetch = git("fetch", "--depth=1", "origin", "pipeline-data")
    if fetch.returncode != 0:
        raise click.ClickException(
            "No cloud results yet (no pipeline-data branch). Has the GitHub Action run? "
            + fetch.stderr.decode().strip())
    blob = git("show", "FETCH_HEAD:state.bin")
    if blob.returncode != 0:
        raise click.ClickException("pipeline-data branch has no state.bin")
    try:
        restored = import_state(blob.stdout, key)
    except StateError as e:
        raise click.ClickException(str(e))
    stamp = git("log", "-1", "--format=%cd", "--date=local", "FETCH_HEAD").stdout.decode().strip()
    console.print(f"[green]Synced {', '.join(restored)} from the cloud run of {stamp}.[/green]")
    console.print("[dim]Your previous local databases are in data/backup-*/. "
                  "Run `jobpipe dashboard` to browse.[/dim]")


def _jobs_table(jobs: list) -> Table:
    """Create a rich table for jobs display."""
    table = Table(title="Matched Jobs")
    table.add_column("Score", style="cyan", width=6)
    table.add_column("Posted", style="magenta", width=7, justify="right")
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
            _format_age(j),
            j.company[:20],
            j.title[:35],
            j.location[:15],
            h1b_info,
            j.platform.value,
        )
    return table


def _format_age(job) -> str:
    from pipeline.orchestrator import job_age
    age = job_age(job)
    if age is None:
        return "—"
    hours = age.total_seconds() / 3600
    if hours < 1:
        return "now"
    if hours < 24:
        return f"{int(hours)}h"
    days = int(hours // 24)
    return f"{days}d" if days < 60 else f"{days // 30}mo"


if __name__ == "__main__":
    main()
