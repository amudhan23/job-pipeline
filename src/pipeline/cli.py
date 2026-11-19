"""CLI entry point for the job pipeline."""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
import logging
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

    from pipeline.notify.telegram import Telegram
    import time
    tg = Telegram()
    last_health = [0.0]

    def on_share(text: str, image: bytes | None) -> str:
        """A post you forwarded to the bot (text, link or screenshot)."""
        from pipeline import leads
        try:
            found = leads.add_shared(text, image=image)
        except ValueError as e:
            return str(e)
        except Exception as e:
            return f"Couldn't read it: {str(e)[:150]}"
        if not found:
            return "Read it, but it doesn't look like someone hiring. Saved anyway under Leads."
        msgs = []
        for lead in found:
            board = ""
            try:
                board = leads.watch(lead, resolve=leads._resolver())
                if board:
                    leads.LeadStore().update(lead["id"], board=board)
            except Exception:
                pass
            what = " · ".join(x for x in [lead["company"], lead["role"]] if x) or "a role"
            msgs.append(f"{what} (score {lead['score']:.0f}"
                        + (", mentions visa sponsorship" if lead["visa"] == "yes" else
                           ", says no sponsorship" if lead["visa"] == "no" else "") + ")"
                        + (f": now watching {board}" if board else ": couldn't find its job board; it's under Leads"))
        return "Got it: " + "; ".join(msgs)

    def on_phone(job_id: str, action: str) -> str:
        """A button tapped in Telegram."""
        from pipeline.models import ApplicationStatus
        if action in ("lead_watch", "lead_dismiss"):
            from pipeline import leads
            store = leads.LeadStore()
            lead = store.lead(job_id)
            if not lead:
                return "That post is gone."
            if action == "lead_dismiss":
                store.update(job_id, status="dismissed")
                return "Dismissed."
            board = leads.watch(lead, resolve=leads._resolver())
            store.update(job_id, status="watching", board=board or None)
            return (f"Watching {board}: its openings will come through like any other job."
                    if board else "Couldn't find that company's job board. Try Search companies on the dashboard.")
        tracker = Tracker()
        job = tracker.get_job(job_id)
        if not job:
            return "That job is gone from the list."
        if action == "skip":
            tracker.update_status(job_id, ApplicationStatus.SKIPPED, notes="skipped from Telegram")
            return f"Skipped {job['company']}."
        if job["status"] == "applied":
            return f"Already applied to {job['company']}."
        if action == "approve":
            n = autoapply.approve(job)
            s_ = autoapply.load_settings()
            if int(s_.get("review_first", 0)) >= 1000:          # "always ask me"
                return f"Submitting {job['company']} with exactly those answers."
            left = max(0, int(s_.get("review_first", 0)) - n)
            return (f"Submitting {job['company']} with exactly those answers. "
                    + (f"{left} more approval{'s' if left != 1 else ''} before it runs on its own."
                       if left else "That was the last one: from now on it submits on its own."))
        autoapply.enqueue(job_id, "submit" if action == "submit" else "practice")
        return (f"Submitting {job['company']} next — you'll get a message when it's done."
                if action == "submit" else f"Preparing {job['company']}; it'll wait under Needs you.")

    def after_tick(new_ids: list[str]) -> None:
        tracker = Tracker()
        if new_ids:
            fresh = [j for j in (tracker.get_job(i) for i in new_ids) if j]
            for j in fresh:
                console.print(f"[bold magenta]NEW[/bold magenta] {datetime.now():%H:%M} "
                              f"{j['company']} — {j['title']}  [dim]({j['fit_score']:.0f}, {j['location']})[/dim]")
            pool.submit(fit_check, new_ids)
            try:
                tg.notify_new(fresh)
            except Exception as e:
                console.print(f"[yellow]Telegram: {e}[/yellow]")
        if tg.configured():
            tg.poll_once(on_phone, on_share=on_share)
            tg.maybe_summary(tracker)
        meta = autoapply.maybe_autoapply(tracker, allow_auto=auto)
        if meta:
            console.print(f"[cyan]Auto-apply started:[/cyan] {meta['label']}")
        try:
            from pipeline import leads
            leads.maybe_run(tg)                                  # hiring posts, in the background
        except Exception as e:
            console.print(f"[yellow]leads: {e}[/yellow]")
        if time.monotonic() - last_health[0] > 600:           # every 10 minutes
            last_health[0] = time.monotonic()
            try:
                from pipeline import health
                found = health.problems()
                for f in found:
                    console.print(f"[yellow]⚠ {f['what']}[/yellow]")
                health.alert(found, tg)
            except Exception as e:
                console.print(f"[yellow]health check: {e}[/yellow]")

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

    def watchdog() -> None:
        """If the crawler dies or stops writing its status, exit so the
        container restarts (Fly/Docker restart policy) instead of sitting idle."""
        import os
        import time
        from pipeline.crawler.worker import parse_iso, read_status, utcnow
        time.sleep(300)
        while True:
            time.sleep(60)
            st = read_status()
            upd = parse_iso(st.get("updated_at")) if st.get("updated_at") else None
            stale = upd is None or (utcnow() - upd).total_seconds() > 30 * 60
            if not t.is_alive() or stale:
                why = "the crawler thread stopped" if not t.is_alive() else "no crawler pass in 30 minutes"
                console.print(f"[red]Restarting: {why}.[/red]")
                try:
                    from pipeline.notify.telegram import Telegram
                    Telegram().send(f"🔄 jobpipe is restarting itself: {why}.")
                except Exception:
                    pass
                os._exit(3)

    threading.Thread(target=watchdog, name="watchdog", daemon=True).start()
    try:
        run_dashboard(host=host, port=port)
    finally:
        loop.call_soon_threadsafe(stop.set)
        t.join(timeout=15)


@main.command()
@click.option("--job-id", "-j", default=None, help="A job the crawler found (see `jobpipe status`)")
@click.option("--url", "-u", default=None, help="Any posting URL, found by the crawler or not")
@click.option("--jd", type=click.Path(exists=True, dir_okay=False), default=None,
              help="A file with the job description (when the page needs a login or you pasted it)")
@click.option("--company", default="", help="Company name (default: from the posting)")
@click.option("--title", default="", help="Role title (default: from the posting)")
@click.option("--output-dir", "-o", default=None, help="Where to write it (default: the job's application folder)")
@click.option("--ai/--no-ai", "use_ai", default=True, help="Let Claude write it (default: on with a key)")
@click.option("--reword/--no-reword", default=True, help="Reword bullets toward the job (fact-guarded)")
@click.pass_context
def resume(ctx: click.Context, job_id: str | None, url: str | None, jd: str | None, company: str,
           title: str, output_dir: str | None, use_ai: bool, reword: bool) -> None:
    """Write the resume for one job: --job-id for one the crawler found, or
    --url for any posting. With a career profile it is written from the
    profile for this job's needs; without one it is arranged from your resume."""
    import json as _json
    import re as _re
    from pipeline import applicant
    from pipeline.ai.context import job_from_url
    from pipeline.resume.tailor import tailored_resume
    if not job_id and not url:
        raise click.ClickException("give --job-id or --url")
    if job_id:
        job = Tracker().get_job(job_id)
        if not job:
            raise click.ClickException(f"Job {job_id} not found")
        if jd:
            job = {**job, "description": Path(jd).read_text(errors="replace")}
    else:
        job = job_from_url(url, company, title, Path(jd).read_text(errors="replace") if jd else "",
                           tracker=Tracker())
    if len((job.get("description") or "").split()) < 60:
        console.print("[yellow]No job description could be read from the posting; pass one with --jd FILE "
                      "for a resume written to the job.[/yellow]")
    job_id = job["id"]
    folder = Path(output_dir) if output_dir else \
        applicant.PRIVATE_DIR / "applications" / _re.sub(r"[^A-Za-z0-9_.-]", "_", job_id)
    for stale in (folder / "tailor.json",):          # an explicit request always rebuilds
        stale.unlink(missing_ok=True)
    for old in folder.glob("*Resume.pdf") if folder.exists() else []:
        old.unlink()
    pdf, note = tailored_resume(job, ctx.obj["config"], applicant.load(), folder, use_ai=use_ai,
                                rewrite=reword)
    if not pdf:
        raise click.ClickException(f"Resume not built: {note}")
    info = _json.loads((folder / "tailor.json").read_text())
    how = {"profile": "written from your career profile", "ai": "arranged by Claude"}.get(info.get("by"), "keyword rules")
    console.print(f"[bold green]Built {pdf}[/bold green]  ({how})")
    console.print(f"  {note}")
    if info.get("by") == "profile":
        console.print(f"  [dim]steps: {' → '.join(info.get('steps') or [])}"
                      f"{'' if info.get('had_description', True) else ' · no job description was available'}[/dim]")
        if info.get("dropped"):
            console.print(f"  [yellow]dropped (not in your profile):[/yellow] " + " | ".join(info["dropped"])[:600])
        if info.get("trimmed"):
            console.print(f"  [dim]trimmed to fit one page: {len(info['trimmed'])} lines[/dim]")
    else:
        console.print(f"  [dim]projects: {', '.join(info.get('projects', []))} · "
                      f"languages: {', '.join(info.get('languages', [])[:4])}…[/dim]")
    for c in info.get("changes", []):
        if "error" in c:
            console.print(f"  [yellow]rewording skipped: {c['error']}[/yellow]")
        elif c["kept"]:
            console.print(f"  [green]reworded[/green] {c['before']}\n        → {c['after']}")
        else:
            console.print(f"  [dim]kept original ({c['why']}): {c['before'][:70]}…[/dim]")


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


@main.command()
def seed() -> None:
    """Watch every company that has hired new grads or interns (~4,000 boards).

    The crawler does this by itself once a week; run it to do it now."""
    from pipeline.discovery import BoardRegistry
    from pipeline.discovery import seed as seeder
    registry = BoardRegistry()
    console.print("Downloading the company lists…")
    info = seeder.run_sync(registry)
    for e in info["errors"]:
        console.print(f"[yellow]{e}[/yellow]")
    counts = registry.counts()
    console.print(f"[green]{info['new']} new company boards added[/green] "
                  f"({info['boards']} in the lists). Now watching {sum(counts.values())}: "
                  + ", ".join(f"{p} {n}" for p, n in sorted(counts.items())))
    console.print("The crawler reads each new board once to learn what's already open, "
                  "then reports roles that open after that.")


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
@click.option("--notify-ready", is_flag=True,
              help="Practice mode: send each complete application to Telegram with every answer, "
                   "for a Submit/Skip tap")
def apply(job_id: tuple[str, ...], top: int, min_score: float, headed: bool, submit: bool,
          yes: bool, use_ai: bool, trust_ai: bool, use_fit: bool, use_tailor: bool,
          handoff: bool, handoff_minutes: int, notify_ready: bool) -> None:
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
    from pipeline.notify.telegram import Telegram
    tg = Telegram()
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
    from pipeline import autoapply as _aa
    autoapply_settings = _aa.load_settings()

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
        if not headed:                       # unattended: ask on your phone
            try:
                tg.notify_questions(job, [p for p in pending if not p.get("draft")])
            except Exception:
                pass
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
            pdf, note = tailored_resume(job, config, details, folder, use_ai=use_ai,
                                        rewrite=autoapply_settings.get("craft", True))
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
                    if not headed:
                        try:
                            tg.notify_stopped(job, "it has answers Claude wrote, and auto-apply is set not to "
                                                   "submit those (turn on “Submit forms with AI-written answers”)")
                        except Exception:
                            pass
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
        get_code = None
        from pipeline.notify import mailcodes as _mc
        if not headed and (_mc.configured() or tg.configured()):
            def get_code(msg: str, job=job) -> str | None:
                """Gmail first (if you saved an app password), then Telegram."""
                from datetime import timedelta, timezone
                from pipeline.notify import mailcodes
                if mailcodes.configured():
                    console.print("  [dim]waiting for the security-code email…[/dim]")
                    code = mailcodes.find_code(job["company"],
                                               since=_dt.now(timezone.utc) - timedelta(minutes=3))
                    if code:
                        console.print("  [green]✓ read the security code from your email[/green]")
                        return code
                if tg.configured():
                    return tg.ask(f"🔐 {msg}\nReply with the code within 10 minutes.", 600)
                return None
        report = practice_apply(job, details, upload, folder, headless=not headed, saved=saved,
                                on_filled=on_filled, ask_human=ask_human, writer=writer,
                                handoff_s=handoff_minutes * 60 if handoff else None, get_code=get_code)
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
            try:
                tg.notify_applied(job, report)
            except Exception:
                pass
        elif report.submit_note:
            console.print(f"  [yellow]not submitted — {report.submit_note}[/yellow] · "
                          f"screenshot: {report.screenshot}\n")
            if not headed and report.mode == "submit":
                try:
                    tg.notify_stopped(job, report.submit_note)
                except Exception:
                    pass
        else:
            verdict = "[green]ready to submit[/green]" if report.ready_to_submit \
                else "[yellow]not ready — see above[/yellow]"
            console.print(f"  {verdict} · screenshot: {report.screenshot}\n")
            if notify_ready and report.ready_to_submit and tg.configured():
                try:
                    from pipeline import autoapply as _aa
                    s_ = _aa.load_settings()
                    tg.notify_ready(job, report, resume=upload,
                                    progress=(_aa.approvals(), int(s_.get("review_first", 0))))
                    console.print("  [cyan]sent to Telegram for your OK[/cyan]")
                except Exception as e:
                    console.print(f"  [yellow]Telegram: {e}[/yellow]")
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


@main.command()
@click.argument("companies")
@click.option("--roles", "-r", default="", help='Comma-separated roles, e.g. "software engineer, backend"')
@click.option("--anywhere", is_flag=True, help="Any location (default: your profile's locations)")
@click.option("--no-watch", is_flag=True, help="Search once; don't add these companies to the crawler's watchlist")
@click.pass_context
def search(ctx: click.Context, companies: str, roles: str, anywhere: bool, no_watch: bool) -> None:
    """Search companies you name for roles you name, e.g.
    jobpipe search "Stripe, Jane Street, Datadog" -r "software engineer, backend"."""
    import asyncio
    from rich.table import Table
    from pipeline.search import run_search, upsert_search
    s = upsert_search(companies, roles, anywhere=anywhere, watch=not no_watch)
    if not s["companies"]:
        raise click.ClickException("Name at least one company.")
    s = asyncio.run(run_search(s, ctx.obj["config"], progress=lambda m: console.print(f"[dim]{m}[/dim]")))
    tracker = Tracker()
    table = Table(title=f"Roles: {', '.join(s['roles']) or 'your profile roles'}")
    for col in ("Company", "Board", "Score", "Job", "Location"):
        table.add_column(col, overflow="fold")
    for company, r in s["results"].items():
        jobs = sorted((tracker.get_job(i) for i in r["jobs"]), key=lambda j: -(j or {}).get("fit_score", 0))
        boards = ", ".join(f"{p}/{sl}" for p, sl in r["boards"]) or f"[yellow]{r['error']}[/yellow]"
        if not jobs:
            table.add_row(company, boards, "", "[dim]no matching roles open[/dim]" if r["boards"] else "", "")
        for k, j in enumerate(jobs[:15]):
            table.add_row(company if k == 0 else "", boards if k == 0 else "", f"{j['fit_score']:.0f}",
                          f"{j['title']}\n[dim]{j['url']}[/dim]", j["location"])
    console.print(table)
    if s["watch"]:
        console.print("[dim]Watching these companies: the crawler reads them at least every 15 minutes "
                      "with these roles.[/dim]")


@main.group()
def telegram() -> None:
    """Job alerts and replies on your phone."""


@telegram.command("setup")
def telegram_setup() -> None:
    """Connect a Telegram bot: make one with @BotFather, paste its token here,
    then send /start to it."""
    from pipeline.notify.telegram import Telegram
    tg = Telegram()
    console.print("1. In Telegram, open @BotFather, send /newbot and follow the steps.\n"
                  "2. It gives you a token like 123456:ABC-…")
    token = click.prompt("Paste the bot token (input hidden)", hide_input=True)
    try:
        bot = tg.connect(token)
    except Exception as e:
        raise click.ClickException(f"Telegram didn't accept that token: {e}")
    console.print(f"3. Now open [bold]https://t.me/{bot}[/bold] and press Start (or send /start). Waiting…")
    chat = tg.wait_for_start(180)
    if not chat:
        raise click.ClickException("No message arrived within 3 minutes. Run `jobpipe telegram setup` again.")
    console.print("[green]Connected.[/green] You'll get alerts there while `jobpipe watch` or `serve` runs.")


@telegram.command("test")
def telegram_test() -> None:
    """Send a test message."""
    from pipeline.notify.telegram import Telegram
    tg = Telegram()
    if not tg.configured():
        raise click.ClickException("Not connected yet: run `jobpipe telegram setup`.")
    if not tg.send("👋 Test from jobpipe. Alerts are working."):
        raise click.ClickException("Telegram didn't take the message — check the bot token.")
    console.print("Sent.")


@main.group()
def profile() -> None:
    """Your career profile: the source every resume, answer and email is written from."""


@profile.command("set")
@click.argument("path", type=click.Path(exists=True, dir_okay=False))
def profile_set(path: str) -> None:
    """Save a markdown file as your career profile (kept in data/private)."""
    from pipeline.resume import writer as pw
    text = Path(path).read_text()
    pw.save_profile(text)
    left = len(re.findall(r"\[confirm", text, re.I))
    console.print(f"Saved. {len(pw.never_claim(text))} never-claim terms"
                  + (f"; [yellow]{left} \\[confirm] notes still to check[/yellow]." if left else "."))


@profile.command("reference")
@click.argument("pdf", type=click.Path(exists=True, dir_okay=False))
def profile_reference(pdf: str) -> None:
    """Save a resume whose style new resumes should copy (never its facts)."""
    from pipeline.resume import writer as pw
    n = pw.save_reference(Path(pdf).read_bytes())
    console.print(f"[green]Saved your reference resume[/green] ({n:,} characters of text read).")


@profile.command("show")
def profile_show() -> None:
    """Print where the profile is and what the checker reads from it."""
    from pipeline.resume import writer as pw
    text = pw.load_profile()
    if not text:
        raise click.ClickException("No profile yet: `jobpipe profile set my_profile.md`, or paste it on "
                                   "the dashboard's Career profile page.")
    secs = [h for h in pw.sections(text) if not h.startswith("project:") and h != "_top"]
    console.print(f"Sections: {', '.join(secs)}")
    console.print(f"Never claim: {', '.join(pw.never_claim(text)) or '(none)'}")


@profile.command("check")
@click.argument("resume_json", required=False, type=click.Path(exists=True, dir_okay=False))
def profile_check(resume_json: str | None) -> None:
    """Show what the writer finds in your career profile and the notes left to confirm.

    Give a resume JSON (the shape the writer uses) to fact-check it instead."""
    import re
    from pipeline.resume import writer as pw
    if not pw.has_profile():
        console.print("[red]No career profile.[/red] Save one with `jobpipe profile set FILE` "
                      "or on the Career profile page.")
        raise SystemExit(1)
    profile_text = pw.load_profile()
    if resume_json:
        _, dropped = pw.check(json.loads(Path(resume_json).read_text()), profile_text)
        for d in dropped:
            console.print(f"[red]✗[/red] {d['where']}: {d['line']}  [dim]({d['why']})[/dim]")
        console.print("[green]Every line is supported.[/green]" if not dropped
                      else f"{len(dropped)} line(s) would be dropped.")
        return
    secs = pw.sections(profile_text)
    roles = [h for h in secs if not h.startswith("project:") and ":" in h]
    projects = [h[len("project:"):] for h in secs if h.startswith("project:") and not h.rstrip().endswith(":")]
    console.print(f"[green]Career profile found[/green] ({len(profile_text):,} characters).")
    console.print(f"[bold]Roles ({len(roles)}):[/bold] " + "; ".join(roles))
    console.print(f"[bold]Projects ({len(projects)}):[/bold] " + ", ".join(projects))
    never = pw.never_claim(profile_text)
    if never:
        console.print(f"[bold]Never claimed:[/bold] " + ", ".join(sorted(never)))
    from rich.markup import escape
    notes = [(i, line.strip()) for i, line in enumerate(profile_text.splitlines(), 1)
             if re.search(r"\[confirm(?! \.\.\.)", line, re.I)]      # not the rule explaining notes
    if notes:
        console.print(f"\n[yellow]{len(notes)} notes to confirm[/yellow] (the writer won't use these as facts "
                      "until you edit them into plain text or delete them):")
        for i, line in notes:
            console.print(f"  line {i}: {escape(line[:220])}")


@main.command()
@click.option("--online", is_flag=True, help="Also contact the AI, Telegram, Gmail and the job sites")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output")
def doctor(online: bool, as_json: bool) -> None:
    """Check every part of the setup and say what to fix."""
    from pipeline import doctor as doc
    found = doc.checks(online=online)
    if as_json:
        click.echo(json.dumps(found, indent=1))
    else:
        style = {"ok": "[green]✓[/green]", "warn": "[yellow]![/yellow]", "fail": "[red]✗[/red]"}
        for c in found:
            console.print(f" {style[c['level']]} [bold]{c['name']}[/bold]: {c['message']}")
        fails = sum(c["level"] == "fail" for c in found)
        console.print(f"\n{'[red]' + str(fails) + ' problem(s) to fix.[/red]' if fails else '[green]Ready.[/green]'}")
    if any(c["level"] == "fail" for c in found):
        raise SystemExit(1)


@main.command()
@click.option("--fake", is_flag=True, help="Use a made-up applicant and a blank resume (no private data needed)")
@click.option("--board", default=None, help="Greenhouse board to test against (default: a few well-known ones)")
@click.option("--out", "out_dir", default=None, help="Where to keep the screenshot and report")
def selftest(fake: bool, board: str | None, out_dir: str | None) -> None:
    """Fill a real, live job application end to end without sending it.
    Proves the browser, the form filler and your details work together."""
    from pipeline import doctor as doc
    folder = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="jobpipe-selftest-"))
    console.print(f"[bold]Self-test[/bold] ({'made-up applicant' if fake else 'your details'}). Nothing is submitted.\n")
    result = doc.selftest(folder, fake=fake, board=board, say=lambda m: console.print("  " + m))
    (folder / "selftest.json").write_text(json.dumps(result, indent=1))
    if result.get("screenshot"):
        console.print(f"\n  screenshot: {result['screenshot']}")
    console.print(f"\n{'[green]Passed.[/green]' if result['ok'] else '[red]Failed.[/red]'} Report: {folder / 'selftest.json'}")
    if not result["ok"]:
        raise SystemExit(1)


@main.group()
def mail() -> None:
    """Read job sites' security-code emails from Gmail (for unattended submits)."""


@mail.command("setup")
def mail_setup() -> None:
    """Save your Gmail address and an app password, then test it."""
    from pipeline.notify import mailcodes
    console.print("Gmail needs 2-step verification on. Then make an app password at "
                  "[bold]https://myaccount.google.com/apppasswords[/bold] (name it jobpipe).\n"
                  "jobpipe opens your inbox read-only, looks only at recent mail from job sites, "
                  "marks nothing read and keeps nothing.")
    addr = click.prompt("Gmail address")
    pw = click.prompt("App password (input hidden)", hide_input=True)
    try:
        mailcodes.save({"email": addr, "app_password": pw, "enabled": True})
    except ValueError as e:
        raise click.ClickException(str(e))
    ok, msg = mailcodes.test_login()
    console.print(("[green]" if ok else "[red]") + msg + ("[/green]" if ok else "[/red]"))


@mail.command("test")
def mail_test() -> None:
    """Log in and count recent Greenhouse emails."""
    from pipeline.notify import mailcodes
    ok, msg = mailcodes.test_login()
    console.print(("[green]" if ok else "[red]") + msg + ("[/green]" if ok else "[/red]"))
    if not ok:
        raise SystemExit(1)


@mail.command("forget")
def mail_forget() -> None:
    """Delete the saved Gmail app password."""
    from pipeline.notify import mailcodes
    mailcodes.forget()
    console.print("Removed. Security codes will be asked for on Telegram.")


@main.command()
@click.option("--job", "job_id", default=None, help="Rehearse on this job (default: your best open match)")
@click.option("--headless", is_flag=True, help="Don't show the browser while it fills the form")
@click.option("--wait-minutes", default=10, help="How long to wait for your tap on Telegram")
@click.option("--skip", "skip_steps", default="", help="Comma list of steps to skip: apply,telegram,mail,leads")
@click.pass_context
def rehearse(ctx: click.Context, job_id: str | None, headless: bool, wait_minutes: int, skip_steps: str) -> None:
    """Dress rehearsal on this computer before going to the cloud: every step
    of an unattended application on a real job, without sending anything.

    1. setup check   2. fill a real form for your best match (you watch)
    3. the Telegram approval round trip   4. read a security code from Gmail
    5. collect hiring posts. Stop `jobpipe watch` first (it would take the taps)."""
    import time as _time
    from types import SimpleNamespace
    from pipeline import applicant, autoapply, doctor as doc
    from pipeline.notify import mailcodes
    from pipeline.notify.telegram import Telegram
    skip = {x.strip() for x in skip_steps.split(",") if x.strip()}
    results: list[tuple[str, str, str]] = []

    def done(step: str, level: str, msg: str) -> None:
        results.append((step, level, msg))
        mark = {"ok": "[green]✓[/green]", "warn": "[yellow]![/yellow]", "fail": "[red]✗[/red]"}[level]
        console.print(f"{mark} [bold]{step}[/bold]: {msg}\n")

    console.rule("1. Setup check")
    checks = doc.checks(online=True)
    for c in checks:
        console.print(f"   {'✓' if c['level'] == 'ok' else '!' if c['level'] == 'warn' else '✗'} {c['name']}: {c['message']}")
    fails = [c["name"] for c in checks if c["level"] == "fail"]
    done("Setup", "fail" if fails else "ok", f"fix: {', '.join(fails)}" if fails else "everything in place")
    blocking = {"Your details", "Resume PDF", "Browser for applying"} & set(fails)

    tracker = Tracker()
    job = None
    if "apply" not in skip and not blocking:
        console.rule("2. Fill a real application (nothing is submitted)")
        if job_id:
            job = tracker.get_job(job_id)
        else:
            s_ = autoapply.load_settings()
            picks = autoapply.candidates(tracker, {**s_, "fit_check": False, "fresh_hours": 24 * 14})
            job = picks[0] if picks else None
        if not job:
            done("Fill a form", "warn", "no open job matches yet: run `jobpipe watch` for a while, or pass --job")
        else:
            console.print(f"   {job['company']}: {job['title']} (score {job['fit_score']:.0f})\n")
            try:
                ctx.invoke(apply, job_id=(job["id"],), headed=not headless, submit=False)
                from pipeline.dashboard.routes import _report
                rep = _report(applicant.PRIVATE_DIR, job["id"]) or {}
                n = len([f for f in rep.get("filled") or [] if f.get("value")])
                need = rep.get("needs_attention") or []
                done("Fill a form", "ok" if rep.get("ready_to_submit") else "warn",
                     f"{n} fields filled" + (f"; still needs you: {', '.join(f['label'] for f in need[:4])}" if need
                                            else "; it would have been ready to submit"))
            except SystemExit:
                done("Fill a form", "fail", "the apply run stopped; see the output above")
            except Exception as e:
                done("Fill a form", "fail", str(e)[:200])

    tg = Telegram()
    if "telegram" not in skip:
        console.rule("3. Approval on your phone")
        if not tg.configured():
            done("Telegram", "warn", "not connected: `jobpipe telegram setup`")
        else:
            from pipeline.dashboard.routes import _report
            rep = (_report(applicant.PRIVATE_DIR, job["id"]) if job else None) or {}
            fake = SimpleNamespace(
                filled=[SimpleNamespace(label=f["label"], value=f.get("value", "")) for f in rep.get("filled") or []]
                or [SimpleNamespace(label="First name", value=applicant.load().get("first_name", "You"))],
                ai_written=rep.get("ai_written") or [], screenshot=rep.get("screenshot"))
            j = job or {"id": "rehearsal", "company": "Rehearsal Inc.", "title": "Software Engineer", "fit_score": 0}
            tg.notify_ready(j, fake, rehearsal=True)
            console.print(f"   Sent to Telegram. Tap [bold]Submit it[/bold] or [bold]Skip[/bold] "
                          f"(waiting up to {wait_minutes} min)…")
            tapped: list[str] = []
            deadline = _time.monotonic() + wait_minutes * 60
            while not tapped and _time.monotonic() < deadline:
                tg.poll_once(lambda jid, action: tapped.append(action) or
                             ("Rehearsal: this is where it would submit. Nothing was sent." if action == "approve"
                              else "Rehearsal: skipped."), wait=20)
            if tapped:
                done("Telegram", "ok", f"your tap arrived ({'Submit' if tapped[0] == 'approve' else 'Skip'}); "
                     "in a real run that sends the application")
            else:
                done("Telegram", "fail", "no tap arrived. Is `jobpipe watch` running somewhere? It takes the taps.")

    if "mail" not in skip:
        console.rule("4. Security codes from Gmail")
        if not mailcodes.configured():
            done("Gmail codes", "warn", "not set (`jobpipe mail setup`); codes will be asked for on Telegram")
        else:
            ok, msg = mailcodes.test_login()
            if not ok:
                done("Gmail codes", "fail", msg)
            else:
                got = mailcodes.latest_code()
                done("Gmail codes", "ok", f"{msg} Latest code email: {got['subject']} ({got['date'][:16]}), "
                     f"read the code {got['code']}" if got else
                     f"{msg} No code emails in 60 days to test on; the first real one will be read live")

    if "leads" not in skip:
        console.rule("5. Hiring posts")
        from pipeline import leads as _leads
        rep = _leads.fetch(sources=["hn", "bluesky"])
        top = sorted(rep["new"], key=lambda x: -x["score"])[:3]
        msg = ", ".join(f"{k}: {v} posts" for k, v in rep["posts"].items()) or "no posts"
        if rep["errors"]:
            msg += "; errors: " + "; ".join(f"{k}: {v}" for k, v in rep["errors"].items())
        for t in top:
            console.print(f"   {t['score']:.0f}  {t['company'] or '?'}: {t['role'] or '?'}  {t['url']}")
        done("Hiring posts", "fail" if rep["errors"] and not rep["posts"] else "ok",
             f"{msg}; {len(rep['new'])} new leads")

    console.rule("Result")
    bad = [r for r in results if r[1] == "fail"]
    if bad:
        console.print(f"[red]{len(bad)} step(s) to fix before the cloud:[/red] " + ", ".join(r[0] for r in bad))
        raise SystemExit(1)
    console.print("[green]Rehearsal passed.[/green] Next: [bold]jobpipe cloud deploy[/bold]")


@main.group()
def leads() -> None:
    """Hiring posts from Hacker News, Bluesky, X, and posts you send in."""


@leads.command("fetch")
@click.option("--source", "sources", default="", help="Comma list: hn,bluesky,x (default: the enabled ones)")
def leads_fetch(sources: str) -> None:
    """Collect new hiring posts now and show the best."""
    from pipeline import leads as _leads
    rep = _leads.fetch(sources=[x.strip() for x in sources.split(",") if x.strip()] or None)
    for k, v in rep["posts"].items():
        console.print(f"{k}: {v} posts read")
    for k, v in rep["errors"].items():
        console.print(f"[red]{k}: {v}[/red]")
    for lead in sorted(rep["new"], key=lambda x: -x["score"])[:15]:
        visa = {"yes": " [green]visa ✓[/green]", "no": " [red]no visa[/red]"}.get(lead["visa"], "")
        console.print(f"{lead['score']:>4.0f}  {lead['company'] or '?'}: {lead['role'] or '?'}{visa}  [dim]{lead['url']}[/dim]")
    console.print(f"{len(rep['new'])} new leads.")


@leads.command("add")
@click.argument("text", required=False)
@click.option("--image", type=click.Path(exists=True, dir_okay=False), help="A screenshot of the post")
def leads_add(text: str | None, image: str | None) -> None:
    """Add a post you saw (paste its text, or give a screenshot)."""
    from pipeline import leads as _leads
    if not text and not image:
        text = click.edit("# Paste the post below, save and close\n") or ""
        text = "\n".join(l for l in text.splitlines() if not l.startswith("#"))
    found = _leads.add_shared(text or "", image=Path(image).read_bytes() if image else None)
    for lead in found:
        board = _leads.watch(lead, resolve=_leads._resolver())
        console.print(f"{lead['company'] or '?'}: {lead['role'] or '?'} (score {lead['score']:.0f})"
                      + (f", watching {board}" if board else ""))
    if not found:
        console.print("That doesn't read as a hiring post.")


@leads.command("x-token")
def leads_x_token() -> None:
    """Save an X API bearer token (pay-per-use; about $0.005 per post read)."""
    from pipeline import leads as _leads
    console.print("Create an app at console.x.com, buy a few dollars of credits, copy the app's Bearer Token.\n"
                  "jobpipe reads at most the daily cap you set (default 100 posts, about $0.50 a day).")
    tok = click.prompt("Bearer token (input hidden)", hide_input=True)
    cap = click.prompt("Posts per day at most", default=100, type=int)
    try:
        _leads.save_settings({"x_token": tok, "x": True, "x_daily_posts": cap})
    except ValueError as e:
        raise click.ClickException(str(e))
    console.print("Saved. X is on.")


@main.group()
def hirebase() -> None:
    """Jobs from 500k+ career pages beyond your boards (free key from hirebase.org)."""


@hirebase.command("key")
def hirebase_key() -> None:
    """Save your Hirebase API key (Profile → API Key at hirebase.org) and test it."""
    import httpx
    from pipeline import hirebase as hb
    key = click.prompt("Hirebase API key (input hidden)", hide_input=True)
    try:
        hb.save_key(key)
    except ValueError as e:
        raise click.ClickException(str(e))
    try:
        r = httpx.post(hb.ESTIMATE, json={"job_titles": ["Software Engineer"], "days_ago": 1},
                       headers={"x-api-key": hb.get_key()}, timeout=20)
        if r.status_code == 401:
            raise click.ClickException("Saved, but Hirebase rejected it. Copy it again from Profile → API Key.")
        console.print(f"[green]Saved and working.[/green] (Hirebase answered {r.status_code}.) "
                      "The crawler asks for new matches every 8 hours, within the free 500 jobs a month.")
    except httpx.HTTPError as e:
        console.print(f"Saved. Couldn't reach Hirebase to test it ({type(e).__name__}).")


@hirebase.command("fetch")
def hirebase_fetch() -> None:
    """Ask Hirebase for new matching jobs now and store them (uses some of the monthly allowance)."""
    import asyncio
    from pipeline import hirebase as hb
    from pipeline.crawler.worker import Crawler
    if not hb.get_key():
        raise click.ClickException("No key yet: `jobpipe hirebase key`.")
    crawler = Crawler(load_config())

    async def go():
        try:
            return await crawler.poll_hirebase()
        finally:
            await crawler.fetcher.aclose()
    new_ids = asyncio.run(go())
    info = crawler.last_hirebase
    if info.get("error"):
        raise click.ClickException(info["error"])
    tracker = Tracker()
    for i in new_ids[:30]:
        j = tracker.get_job(i)
        if j:
            tag = " [green]visa ✓[/green]" if (j.get("description") or "").startswith("Visa sponsorship: offered") else ""
            console.print(f"{j['fit_score']:>4.0f}  {j['company']}: {j['title']} · {j['location']}{tag}  [dim]{j['url']}[/dim]")
    st = hb.public()
    console.print(f"{info['returned']} returned, {info.get('reposts', 0)} agency/aggregator reposts dropped, "
                  f"{info['matching']} match your filters, {info['new_jobs']} new in Live jobs, "
                  f"{info['new_boards']} new boards for the crawler. {st['used']} of {st['monthly_jobs']} used this month.")


@hirebase.command("status")
def hirebase_status() -> None:
    """This month's usage and the last ask."""
    from pipeline import hirebase as hb
    st = hb.public()
    console.print(json.dumps(st, indent=1))


# ── Cloud ──────────────────────────────────────────────────────────────

CLOUD_FILE = Path.home() / ".config" / "jobpipe" / "cloud.json"


def _cloud_saved() -> dict:
    try:
        return json.loads(CLOUD_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _cloud_save(data: dict) -> None:
    import os
    CLOUD_FILE.parent.mkdir(parents=True, exist_ok=True)
    CLOUD_FILE.write_text(json.dumps(data, indent=1))
    os.chmod(CLOUD_FILE, 0o600)


def _push(url: str, token: str, with_secrets: bool = True) -> list[str]:
    import httpx
    from pipeline import sync
    bundle, names = sync.build(with_secrets=with_secrets)
    r = httpx.post(url.rstrip("/") + "/api/sync", content=bundle, timeout=120,
                   headers={"Authorization": f"Bearer {token}", "Content-Type": "application/zip"})
    if r.status_code == 401:
        raise click.ClickException("The cloud dashboard rejected the token.")
    if r.status_code >= 400:
        raise click.ClickException(f"Upload failed: {r.status_code} {r.text[:200]}")
    added = r.json().get("boards_added", 0)
    return names + ([f"{added} new boards registered"] if added else [])


@main.group()
def cloud() -> None:
    """Run jobpipe on Fly.io around the clock."""


@cloud.command("deploy")
@click.option("--app", "app_name", default=None, help="Fly app name (becomes <name>.fly.dev)")
@click.option("--region", default="ewr", help="Fly region (ewr = Newark, near New York)")
@click.option("--no-push", is_flag=True, help="Don't copy your details and settings up afterwards")
def cloud_deploy(app_name: str | None, region: str, no_push: bool) -> None:
    """Create (or update) your jobpipe on Fly.io and copy your setup to it.

    Needs flyctl and a Fly account with a card on file (about $14/month for
    1 shared CPU, 2 GB memory and a 3 GB disk). Safe to run again: it
    updates the same app."""
    import secrets
    import shutil
    import subprocess
    import time as _time
    import httpx
    repo = Path(__file__).resolve().parents[2]
    if not (repo / "Dockerfile").exists() or not (repo / "fly.toml").exists():
        raise click.ClickException("Run this from a checkout of the jobpipe repository (Dockerfile not found).")
    fly = shutil.which("fly") or shutil.which("flyctl")
    if not fly:
        import platform
        how = ('pwsh -Command "iwr https://fly.io/install.ps1 -useb | iex"' if platform.system() == "Windows"
               else "curl -L https://fly.io/install.sh | sh")
        raise click.ClickException(f"Install flyctl first:\n  {how}\nthen run this again.")

    def run(*args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
        return subprocess.run([fly, *args], cwd=repo, check=check, text=True,
                              capture_output=capture)

    if run("auth", "whoami", check=False, capture=True).returncode != 0:
        console.print("Opening the Fly login in your browser…")
        run("auth", "login")
    saved = _cloud_saved()
    app_name = app_name or saved.get("app") or f"jobpipe-{secrets.token_hex(3)}"
    if not re.fullmatch(r"[a-z0-9-]{3,40}", app_name):
        raise click.ClickException("App names are 3-40 lowercase letters, digits and dashes.")
    exists = run("status", "-a", app_name, check=False, capture=True).returncode == 0
    if not exists:
        console.print(f"Creating the app [bold]{app_name}[/bold]…")
        run("apps", "create", app_name)
    vols = run("volumes", "list", "-a", app_name, "--json", check=False, capture=True)
    if vols.returncode != 0 or '"jobpipe_data"' not in (vols.stdout or ""):
        console.print("Creating a 3 GB disk for your data…")
        run("volumes", "create", "jobpipe_data", "--size", "3", "--region", region, "-a", app_name, "--yes")
    token = saved.get("token") if saved.get("app") == app_name else None
    if not token:
        token = secrets.token_urlsafe(32)
        run("secrets", "set", f"JOBPIPE_DASHBOARD_TOKEN={token}", "-a", app_name, "--stage")
    url = f"https://{app_name}.fly.dev"
    _cloud_save({"app": app_name, "url": url, "token": token, "region": region})
    console.print("Building and deploying (the first build takes a few minutes)…")
    run("deploy", "-a", app_name, "--primary-region", region, "--ha=false", "--remote-only")
    console.print("Waiting for it to come up…")
    for _ in range(60):
        try:
            if httpx.get(url + "/healthz", timeout=10).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        _time.sleep(5)
    else:
        raise click.ClickException(f"It didn't answer at {url}/healthz. See `fly logs -a {app_name}`.")
    if not no_push:
        console.print("Copying your details, resume, profile, answers and settings…")
        sent = _push(url, token)
        console.print(f"[green]Sent:[/green] {', '.join(sent)}")
    console.print(f"\n[bold green]Running at {url}[/bold green]\nDashboard password: [bold]{token}[/bold] "
                  f"(also saved in {CLOUD_FILE})\n"
                  "[yellow]Stop `jobpipe watch`/`serve` on this computer now:[/yellow] two copies would both "
                  "answer Telegram and both apply.\nLogs: [bold]fly logs -a " + app_name + "[/bold]   "
                  "Check it: open the dashboard → Auto-apply → Check everything.")


@cloud.command("push")
@click.argument("url", required=False)
@click.option("--no-secrets", is_flag=True, help="Don't send the Anthropic and Hunter keys")
def cloud_push_cmd(url: str | None, no_secrets: bool) -> None:
    """Copy your details, resume, career profile, answers, settings, keys
    and watched boards from this computer to your cloud jobpipe."""
    saved = _cloud_saved()
    url = (url or saved.get("url") or "").rstrip("/")
    if not url:
        raise click.ClickException("Give the cloud dashboard's https:// address (or run `jobpipe cloud deploy`).")
    if not url.startswith("https://") and not url.startswith("http://localhost"):
        raise click.ClickException("Use the https:// address of your cloud dashboard.")
    token = saved.get("token") if saved.get("url") == url else None
    token = token or click.prompt("Dashboard password (JOBPIPE_DASHBOARD_TOKEN, input hidden)", hide_input=True)
    sent = _push(url, token, with_secrets=not no_secrets)
    console.print(f"[green]Sent to {url}:[/green] {', '.join(sent)}.")


@main.command("cloud-push", hidden=True)
@click.argument("url", required=False)
@click.option("--with-ai-key", is_flag=True, hidden=True)
@click.pass_context
def cloud_push(ctx: click.Context, url: str | None, with_ai_key: bool) -> None:
    """Old name for `jobpipe cloud push`."""
    ctx.invoke(cloud_push_cmd, url=url, no_secrets=False)


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
