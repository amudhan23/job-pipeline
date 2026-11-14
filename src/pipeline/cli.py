"""CLI entry point for the job pipeline."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import click
from datetime import datetime
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
@click.option("--host", default="127.0.0.1",
              help="Address to listen on. Anything but this machine needs JOBPIPE_DASHBOARD_TOKEN set.")
@click.pass_context
def dashboard(ctx: click.Context, port: int, host: str) -> None:
    """Open the web dashboard (http://localhost:8050)."""
    from pipeline.dashboard.app import run_dashboard
    run_dashboard(host=host, port=port)


def _make_crawler(config: dict, auto: bool = True):
    """The crawler plus what happens after each pass: print new postings,
    AI fit-check them in the background, and start auto-apply runs."""
    from concurrent.futures import ThreadPoolExecutor
    from pipeline import autoapply
    from pipeline.crawler.worker import Crawler
    pool = ThreadPoolExecutor(max_workers=1)

    def fit_check(ids: list[str]) -> None:
        from pipeline import applicant
        from pipeline.ai import client as ai_client
        from pipeline.ai.context import load_resume_text
        from pipeline.ai.fitcheck import ensure_checked
        settings = autoapply.load_settings()
        if not settings.get("fit_check") or not ai_client.status()[0]:
            return
        resume_text = load_resume_text()
        if not resume_text.strip():
            return
        tracker, details = Tracker(), applicant.load()
        for i in ids:
            job = tracker.get_job(i)
            if job and job["fit_score"] >= settings["min_score"]:
                ensure_checked(job, tracker, details, resume_text)

    def after_tick(new_ids: list[str]) -> None:
        if new_ids:
            tracker = Tracker()
            for i in new_ids:
                j = tracker.get_job(i)
                if j:
                    console.print(f"[bold magenta]NEW[/bold magenta] {datetime.now():%H:%M} "
                                  f"{j['company']} — {j['title']}  [dim]({j['fit_score']:.0f}, {j['location']})[/dim]")
            pool.submit(fit_check, new_ids)
        if auto:
            meta = autoapply.maybe_autoapply(Tracker())
            if meta:
                console.print(f"[cyan]Auto-apply started:[/cyan] {meta['label']}")

    return Crawler(config, after_tick=after_tick)


@main.command()
@click.option("--once", is_flag=True, help="One pass over the boards that are due, then exit")
@click.option("--no-discover", is_flag=True, help="Don't search the web for new boards")
@click.option("--no-auto", is_flag=True, help="Never start auto-apply runs")
@click.pass_context
def watch(ctx: click.Context, once: bool, no_discover: bool, no_auto: bool) -> None:
    """Watch every known job board continuously — new postings show up
    within minutes of going live (Ctrl+C to stop)."""
    import asyncio
    import signal
    config = ctx.obj["config"]
    if no_discover:
        config.setdefault("discovery", {})["enabled"] = False
    crawler = _make_crawler(config, auto=not no_auto)
    stats = crawler.registry.frontier_stats()
    console.print(f"[bold]Watching {stats['boards']} job boards[/bold] — new postings print here; "
                  "the dashboard shows them live. Ctrl+C stops.")

    async def main_loop() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):
                pass
        await crawler.run(stop, max_ticks=1 if once else None)

    asyncio.run(main_loop())
    console.print("Stopped.")


@main.command()
@click.option("--port", "-p", default=8050, help="Dashboard port")
@click.option("--host", default="127.0.0.1",
              help="Dashboard address (0.0.0.0 on a server — then JOBPIPE_DASHBOARD_TOKEN is required)")
@click.option("--no-auto", is_flag=True, help="Never start auto-apply runs")
@click.pass_context
def serve(ctx: click.Context, port: int, host: str, no_auto: bool) -> None:
    """Crawler and dashboard together — what runs in the cloud."""
    import asyncio
    import threading
    from pipeline.dashboard.app import run_dashboard
    crawler = _make_crawler(ctx.obj["config"], auto=not no_auto)
    loop = asyncio.new_event_loop()
    stop = asyncio.Event()

    def crawl() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(crawler.run(stop))

    t = threading.Thread(target=crawl, name="crawler", daemon=True)
    t.start()
    try:
        run_dashboard(host=host, port=port)
    finally:
        loop.call_soon_threadsafe(stop.set)
        t.join(timeout=15)


@main.command()
@click.option("--job-id", "-j", required=True, help="Job ID to generate resume for")
@click.option("--output-dir", "-o", default=None, help="Where to write it (default: the job's application folder)")
@click.option("--ai/--no-ai", "use_ai", default=True, help="Let Claude arrange it (default: on with a key)")
@click.pass_context
def resume(ctx: click.Context, job_id: str, output_dir: str | None, use_ai: bool) -> None:
    """Build a resume arranged for one job (bullets, projects, languages).
    Only reorders and selects what's in your resume — nothing is rewritten."""
    import json as _json
    import re as _re
    from pipeline import applicant
    from pipeline.resume.tailor import tailored_resume
    job = Tracker().get_job(job_id)
    if not job:
        raise click.ClickException(f"Job {job_id} not found")
    folder = Path(output_dir) if output_dir else \
        applicant.PRIVATE_DIR / "applications" / _re.sub(r"[^A-Za-z0-9_.-]", "_", job_id)
    for stale in (folder / "tailor.json",):          # an explicit request always rebuilds
        stale.unlink(missing_ok=True)
    pdf, note = tailored_resume(job, ctx.obj["config"], applicant.load(), folder, use_ai=use_ai)
    if not pdf:
        raise click.ClickException(f"Resume not built: {note}")
    info = _json.loads((folder / "tailor.json").read_text())
    console.print(f"[bold green]Built {pdf}[/bold green]  ({'Claude' if info['by'] == 'ai' else 'keyword rules'})")
    console.print(f"  {note}")
    console.print(f"  [dim]projects: {', '.join(info['projects'])} · languages: {', '.join(info['languages'][:4])}…[/dim]")


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


def _application_keys(j: dict) -> set:
    import re as _re
    from pipeline.apply.filler import application_url
    title = _re.sub(r"[^a-z0-9]+", " ", (j.get("title") or "").lower()).strip()
    return {("url", application_url(j)), ("title", (j.get("company") or "").lower(), title)}


def _one_per_application(jobs: list[dict]) -> list[dict]:
    """The same posting can arrive twice (its board and a feed, with slightly
    different titles). Keep the first — they're sorted best score first — so
    one application is never filled twice."""
    seen: set = set()
    out = []
    for j in jobs:
        keys = _application_keys(j)
        if keys & seen:
            continue
        seen |= keys
        out.append(j)
    return out


@main.command()
@click.option("--job-id", "-j", multiple=True, help="Specific job(s) to apply to")
@click.option("--top", "-n", default=5, help="Otherwise: best N jobs from the latest run")
@click.option("--min-score", default=70.0, help="Only jobs scoring at least this")
@click.option("--headed", is_flag=True, help="Show the browser so you can watch (and step in)")
@click.option("--submit", is_flag=True, help="Submit applications whose every required question is answered")
@click.option("--yes", "-y", is_flag=True, help="With --submit: don't ask before each one (fully automatic)")
@click.option("--ai/--no-ai", "use_ai", default=True,
              help="Draft answers to open questions with Claude (default: on when a key is saved)")
@click.option("--trust-ai", is_flag=True,
              help="With --submit --yes: also auto-submit forms that contain AI answers")
@click.option("--fit/--no-fit", "use_fit", default=True,
              help="AI fit check first; skip jobs with a dealbreaker (default: on with AI)")
@click.option("--tailor/--no-tailor", "use_tailor", default=True,
              help="Upload a resume arranged for each job (default: on when pdflatex is installed)")
@click.option("--handoff", is_flag=True,
              help="Fill what it can, then you finish in the browser and press Submit yourself; "
                   "it saves your new answers and marks the job applied when the site confirms")
@click.option("--handoff-minutes", default=30, help="How long --handoff waits for you")
def apply(job_id: tuple[str, ...], top: int, min_score: float, headed: bool, submit: bool,
          yes: bool, use_ai: bool, trust_ai: bool, use_fit: bool, use_tailor: bool,
          handoff: bool, handoff_minutes: int) -> None:
    """Fill job applications from your saved details.

    Without --submit (practice): fills every question it can, attaches your
    resume, screenshots the form, lists what needs you — and sends nothing.

    With --submit: same, then for each form with every required question
    answered it asks "Submit?" while the filled form is open (use --headed
    to look at it first), clicks Submit and waits for the site's
    confirmation. Confirmed applications are marked applied. --yes skips the
    question. Reports: data/private/applications/<job>/.
    """
    import re as _re
    from datetime import datetime as _dt
    from pipeline import applicant
    from pipeline.apply.filler import application_url, practice_apply
    from pipeline.models import ApplicationStatus

    if yes and not submit:
        raise click.ClickException("--yes only makes sense with --submit.")
    if handoff:
        if submit:
            raise click.ClickException("--handoff means you press Submit yourself; drop --submit.")
        headed = True
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
    candidates: list[dict] = []
    if job_id:
        jobs = [j for j in (tracker.get_job(i) for i in job_id) if j]
        jobs = [j for j in jobs if not (submit and j["status"] == "applied")]
    else:
        # Open postings (crawler or latest run), best score first
        candidates = sorted(tracker.open_jobs(), key=lambda j: j["fit_score"], reverse=True)
        jobs = [j for j in candidates
                if j["fit_score"] >= min_score and application_url(j)
                and j["status"] not in ("applied", "interview", "offer", "rejected", "skipped")]
        jobs = _one_per_application(jobs)      # walked until `top` are filled (fit skips don't count)
    if not jobs:
        raise click.ClickException("No matching jobs (auto-apply supports Greenhouse, Lever, Ashby).")

    out_root = applicant.PRIVATE_DIR / "applications"
    saved = applicant.load_answers()
    want = len(jobs) if job_id else min(top, len(jobs))
    if submit:
        how = "without asking (--yes)" if yes else "asking you before each one"
        console.print(f"\n[bold]Submit mode[/bold] — up to {want} application(s); complete forms are "
                      f"submitted {how}.\n")
    else:
        console.print(f"\n[bold]Practice mode[/bold] — filling up to {want} application(s); "
                      "nothing will be submitted.\n")

    totals = {"questions": 0, "submitted": 0, "ai": 0}
    if use_ai:
        from pipeline.ai import client as ai_client
        ready, why = ai_client.status()
        if ready:
            s = ai_client.spend_summary()
            console.print(f"[dim]AI drafts on ({ai_client.load_settings()['model']}) — "
                          f"${s['month_spent']:.2f} of ${s['monthly_budget']:.2f} used this month[/dim]\n")
        else:
            console.print(f"[dim]AI drafts off: {why}[/dim]\n")
            use_ai = False
    use_fit = use_fit and use_ai
    resume_text = ""
    if use_ai:
        from pipeline.ai.context import load_resume_text
        resume_text = load_resume_text()
    if use_tailor:
        from pipeline.resume.tailor import can_build
        ok, why = can_build()
        if not ok:
            console.print(f"[dim]Tailored resumes off: {why} — uploading your saved resume[/dim]\n")
            use_tailor = False
    config = click.get_current_context().obj.get("config", {}) if click.get_current_context().obj else {}

    def show(report, job) -> None:
        """Print what was filled and what needs you; remember open questions."""
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
                                "label": f.label, "options": f.options, "company": job["company"],
                                "multi": f.kind == "checkboxes", "job_id": job["id"]})
        for f in report.filled:
            if f.label in report.ai_written:
                console.print(f"  [magenta]✎ {f.source}:[/magenta] {f.label[:70]}")
                console.print(f"    [dim]{f.value[:300]}[/dim]")
                pending.append({"question": applicant.normalize_question(f.label, job["company"]),
                                "label": f.label, "options": [], "company": job["company"],
                                "draft": f.value, "job_id": job["id"]})
        totals["ai"] += len(report.ai_written)
        applicant.add_pending(pending)
        totals["questions"] += len(pending) - len(report.ai_written)
        if report.captcha:
            console.print("  [dim]this site runs a bot check (CAPTCHA) on submit — "
                          "it may ask you to solve one[/dim]")

    def ask_human(message: str) -> bool:
        if not headed:
            return False                  # nobody can see the browser
        console.print(f"  [bold yellow]→ {message}[/bold yellow]")
        click.prompt("    Press Enter here when you're done", default="", show_default=False)
        return True

    filled_count = 0
    for job in jobs:
        if filled_count >= want:
            break
        console.print(f"[cyan]{job['company']}[/cyan] — {job['title']}  [dim]({job['fit_score']:.0f})[/dim]")
        folder = out_root / _re.sub(r"[^A-Za-z0-9_.-]", "_", job["id"])
        shown = {"done": False}

        # 1. AI fit check: a dealbreaker in the JD → skip, and don't offer it again
        if use_fit:
            from pipeline.ai.fitcheck import ensure_checked
            fit = ensure_checked(job, tracker, details, resume_text)
            if fit and fit["verdict"] == "skip":
                why_skip = "; ".join(f"{b['rule']}: “{b['quote']}”" for b in fit["dealbreakers"])
                console.print(f"  [red]✗ skipped by fit check[/red] — {why_skip[:220]}\n")
                tracker.update_status(job["id"], ApplicationStatus.SKIPPED, notes=f"AI fit: {why_skip[:400]}")
                continue
            if fit and fit["verdict"] == "maybe":
                console.print(f"  [yellow]? fit check:[/yellow] {fit['summary'][:200]}")
            elif fit:
                console.print(f"  [green]✓ fit check:[/green] [dim]{fit['summary'][:200]}[/dim]")
        filled_count += 1

        # 2. This job's resume
        upload = resume
        if use_tailor:
            from pipeline.resume.tailor import tailored_resume
            pdf, note = tailored_resume(job, config, details, folder, use_ai=use_ai)
            if pdf:
                upload = pdf
                console.print(f"  [green]📄 tailored resume:[/green] {pdf.name} [dim]— {note[:150]}[/dim]")
            else:
                console.print(f"  [yellow]! tailored resume failed ({note}) — using your saved resume[/yellow]")

        def on_filled(report, job=job, shown=shown) -> bool:
            show(report, job)
            shown["done"] = True
            if handoff:
                console.print("  [bold yellow]→ Your turn: finish the form in the browser and press Submit "
                              "there. I'll watch for the confirmation and save your new answers.[/bold yellow]")
                return False
            if not submit:
                return False
            if report.ready_to_submit:
                if yes and report.ai_written and not trust_ai:
                    console.print("  [yellow]has AI-written answers — not auto-submitting. Review them "
                                  "(run without --yes) or pass --trust-ai.[/yellow]")
                    return False
                if yes:
                    return True
                where = "in the browser window" if headed else f"in {report.screenshot}"
                return click.confirm(f"  Everything is filled (check it {where}). Submit this application?",
                                     default=False)
            if headed and not yes:
                # Let you finish the rest by hand right there
                answer = click.prompt("  Not complete. Fill the rest in the browser and type 's' to submit, "
                                      "or press Enter to skip", default="", show_default=False)
                return answer.strip().lower() == "s"
            return False

        writer = None
        if use_ai:
            from pipeline.ai.writer import make_writer
            writer = make_writer(job, details)
        report = practice_apply(job, details, upload, folder, headless=not headed, saved=saved,
                                on_filled=on_filled, ask_human=ask_human, writer=writer,
                                handoff_s=handoff_minutes * 60 if handoff else None)
        if report.captured:
            # Your answers to the questions the bot didn't know — kept for this company
            keep = [{"question": applicant.normalize_question(label, job["company"]), "answer": value,
                     "company": job["company"], "everywhere": False}
                    for label, value in report.captured.items() if value]
            applicant.save_answers(keep)
            saved = applicant.load_answers()
            console.print(f"  [green]✓ saved {len(keep)} answer(s) you typed, for next time[/green]")
        if report.closed:
            tracker.update_status(job["id"], ApplicationStatus.SKIPPED, notes="posting closed")
        if report.error:
            console.print(f"  [red]✗ {report.error}[/red]")
            if report.screenshot:
                console.print(f"    screenshot: {report.screenshot}")
            console.print()
            continue
        if not shown["done"]:
            show(report, job)
        if report.submitted:
            totals["submitted"] += 1
            note = f"submitted by jobpipe {_dt.now():%Y-%m-%d %H:%M}"
            keys = _application_keys(job)
            twins = {j["id"] for j in candidates if _application_keys(j) & keys} | {job["id"]}
            for jid in twins:              # its feed/board twin too, so it's never re-sent
                tracker.update_status(jid, ApplicationStatus.APPLIED, notes=note)
            console.print(f"  [bold green]✓ submitted — {report.submit_note}[/bold green] · "
                          f"screenshot: {report.screenshot}\n")
        elif report.submit_note:
            console.print(f"  [yellow]not submitted — {report.submit_note}[/yellow] · "
                          f"screenshot: {report.screenshot}\n")
        else:
            verdict = "[green]ready to submit[/green]" if report.ready_to_submit \
                else "[yellow]not ready — see above[/yellow]"
            console.print(f"  {verdict} · screenshot: {report.screenshot}\n")
    if totals["submitted"]:
        console.print(f"[bold green]Submitted {totals['submitted']} application(s).[/bold green] "
                      "They're marked applied on the dashboard; watch your email for confirmations.")
    if totals["ai"]:
        console.print(f"[magenta]{totals['ai']} answer(s) drafted by Claude[/magenta] — edit or approve them on "
                      "the dashboard → My details; saved ones are used as your own from then on.")
    if totals["questions"]:
        console.print("[bold]Answer the open questions once[/bold] on the dashboard → My details → "
                      "“Questions waiting for you”, then run `jobpipe apply` again.")


@main.group()
def ai() -> None:
    """Claude API: your key, model, monthly budget and spend."""


@ai.command("key")
def ai_key() -> None:
    """Save your Anthropic API key (kept in data/private, only on this machine)."""
    from pipeline.ai import client as ai_client
    key = click.prompt("Paste your Anthropic API key (input hidden)", hide_input=True)
    try:
        ai_client.save_key(key)
    except ValueError as e:
        raise click.ClickException(str(e))
    console.print(f"[green]Saved[/green] {ai_client.masked_key()}  — run `jobpipe ai status` to check it.")


@ai.command("forget-key")
def ai_forget_key() -> None:
    """Delete the saved API key."""
    from pipeline.ai import client as ai_client
    ai_client.forget_key()
    console.print("Key deleted.")


@ai.command("status")
@click.option("--check", is_flag=True, help="Also make one tiny API call to prove the key works (~$0.0001)")
def ai_status(check: bool) -> None:
    """Show whether AI is ready, the model, and this month's spend."""
    from pipeline.ai import client as ai_client
    ready, why = ai_client.status()
    st, s = ai_client.load_settings(), ai_client.spend_summary()
    console.print(f"Key: {ai_client.masked_key() or '[red]none[/red]'}   Model: {st['model']} "
                  f"({ai_client.MODELS[st['model']]})")
    console.print(f"This month: ${s['month_spent']:.4f} of ${s['monthly_budget']:.2f} "
                  f"({s['month_calls']} calls) · all time ${s['total_spent']:.4f}")
    console.print("[green]Ready[/green]" if ready else f"[yellow]Not ready:[/yellow] {why}")
    if check and ready:
        try:
            r = ai_client.complete("Reply with the single word OK.", "Say OK.", purpose="key check",
                                   max_tokens=5)
            console.print(f"[green]API call worked[/green] ({r.model}, ${r.cost:.6f})")
        except Exception as e:
            raise click.ClickException(f"API call failed: {e}")


@ai.command("budget")
@click.argument("usd", type=float)
def ai_budget(usd: float) -> None:
    """Set the monthly spending cap in USD (calls stop when it's reached)."""
    from pipeline.ai import client as ai_client
    try:
        st = ai_client.save_settings({"monthly_budget": usd})
    except ValueError as e:
        raise click.ClickException(str(e))
    console.print(f"Monthly AI budget: ${st['monthly_budget']:.2f}")


@ai.command("model")
@click.argument("name", type=click.Choice(["sonnet", "haiku", "opus"]))
def ai_model(name: str) -> None:
    """Pick the model: sonnet (default), haiku (cheapest), opus (strongest)."""
    from pipeline.ai import client as ai_client
    ai_client.save_settings({"model": name})
    console.print(f"Model: {name} ({ai_client.MODELS[name]})")


@ai.command("draft")
@click.option("--job-id", "-j", required=True, help="Job to write for")
@click.argument("question")
def ai_draft(job_id: str, question: str) -> None:
    """Draft an answer to one question for one job (cached; shown, not sent)."""
    from pipeline import applicant
    from pipeline.ai import client as ai_client
    from pipeline.ai.writer import draft_answer
    job = Tracker().get_job(job_id)
    if not job:
        raise click.ClickException(f"No job {job_id}")
    resume = applicant.PRIVATE_DIR / applicant.RESUME_FILE
    if not resume.exists():
        raise click.ClickException("Upload your resume on My details first — drafts are built from it.")
    try:
        text = draft_answer(question, job, applicant.load(), applicant.resume_text(resume.read_bytes()))
    except ai_client.AIUnavailable as e:
        raise click.ClickException(str(e))
    console.print(f"\n{text}\n")


@main.command()
@click.option("--job-id", "-j", multiple=True, help="Specific job(s)")
@click.option("--top", "-n", default=30, help="Otherwise: best N unchecked jobs from the latest run")
@click.option("--min-score", default=50.0, help="Only jobs scoring at least this")
@click.option("--recheck", is_flag=True, help="Check again even if already checked")
def fit(job_id: tuple[str, ...], top: int, min_score: float, recheck: bool) -> None:
    """AI fit check: read job descriptions for dealbreakers (clearance,
    citizenship, no sponsorship, years required…). Uses Haiku — a fraction
    of a cent per job. Results show on the dashboard; `jobpipe apply` skips
    jobs marked skip."""
    import json as _json
    from rich.table import Table
    from pipeline import applicant
    from pipeline.ai import client as ai_client
    from pipeline.ai.context import load_resume_text
    from pipeline.ai.fitcheck import check_job, stored
    ready, why = ai_client.status()
    if not ready:
        raise click.ClickException(why)
    resume_text = load_resume_text()
    if not resume_text.strip():
        raise click.ClickException("Upload your resume on My details first — the check compares against it.")
    tracker = Tracker()
    if job_id:
        jobs = [j for j in (tracker.get_job(i) for i in job_id) if j]
    else:
        run = tracker.latest_run()
        jobs = [j for j in tracker.get_jobs(run["id"] if run else None)
                if j["fit_score"] >= min_score and (recheck or not stored(j))
                and j["status"] not in ("applied", "skipped", "rejected")][:top]
    if not jobs:
        console.print("Nothing to check — every job above the score is already checked (--recheck to redo).")
        return
    details = applicant.load()
    table = Table(title=f"AI fit check ({len(jobs)} jobs)")
    for col in ("Verdict", "Company", "Title", "Why"):
        table.add_column(col, overflow="fold")
    colors = {"apply": "green", "maybe": "yellow", "skip": "red"}
    spent0 = ai_client.spend_summary()["total_spent"]
    for j in jobs:
        try:
            r = check_job(j, details, resume_text)
        except ai_client.AIUnavailable as e:
            console.print(f"[yellow]Stopped: {e}[/yellow]")
            break
        tracker.set_ai_fit(j["id"], r)
        why_text = r["summary"]
        if r["dealbreakers"]:
            why_text += " — " + "; ".join(f"“{b['quote'][:90]}”" for b in r["dealbreakers"])
        table.add_row(f"[{colors[r['verdict']]}]{r['verdict']}[/]", j["company"], j["title"][:50],
                      why_text[:260])
    console.print(table)
    cost = ai_client.spend_summary()["total_spent"] - spent0
    console.print(f"[dim]Cost: ${cost:.4f}. `jobpipe apply` skips jobs marked skip.[/dim]")


@main.command("cloud-push")
@click.argument("url")
@click.option("--with-ai-key", is_flag=True, help="Also send your Anthropic API key")
def cloud_push(url: str, with_ai_key: bool) -> None:
    """Copy your details, resume, saved answers and auto-apply settings from
    this computer to your jobpipe in the cloud (its dashboard URL)."""
    import httpx
    from pipeline import applicant, autoapply
    from pipeline.ai import client as ai_client
    url = url.rstrip("/")
    if not url.startswith("https://") and not url.startswith("http://localhost"):
        raise click.ClickException("Use the https:// address of your cloud dashboard.")
    token = click.prompt("Dashboard access token (JOBPIPE_DASHBOARD_TOKEN, input hidden)", hide_input=True)
    headers = {"Authorization": f"Bearer {token}"}
    sent = []
    with httpx.Client(timeout=60, headers=headers) as c:
        def post(path: str, **kw) -> None:
            r = c.post(url + path, **kw)
            if r.status_code == 401:
                raise click.ClickException("The cloud dashboard rejected that token.")
            if r.status_code >= 400:
                raise click.ClickException(f"{path}: {r.status_code} {r.text[:200]}")
        post("/api/profile", json=applicant.load())
        sent.append("details")
        resume = applicant.PRIVATE_DIR / applicant.RESUME_FILE
        if resume.exists():
            post("/api/resume", content=resume.read_bytes(), headers={"Content-Type": "application/pdf"})
            sent.append("resume")
        saved = applicant.load_answers()
        items = [{"question": q, "answer": a, "company": "", "everywhere": True} for q, a in saved["all"].items()]
        for company_key, qs in saved["companies"].items():
            items += [{"question": q, "answer": a, "company": company_key, "everywhere": False} for q, a in qs.items()]
        if items:
            post("/api/answers", json={"answers": items})
            sent.append(f"{len(items)} answers")
        ai = {k: v for k, v in ai_client.load_settings().items() if k in ("model", "monthly_budget", "enabled")}
        if with_ai_key and ai_client.get_key():
            ai["key"] = ai_client.get_key()
            sent.append("AI key")
        post("/api/ai", json=ai)
        post("/api/autoapply", json=autoapply.load_settings())
        sent.append("AI and auto-apply settings")
    console.print(f"[green]Sent to {url}:[/green] {', '.join(sent)}.")


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
