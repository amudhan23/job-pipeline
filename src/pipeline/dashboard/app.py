"""Web dashboard for browsing and filtering scraped jobs.

Run with: jobpipe dashboard
Opens at http://localhost:8050
"""

from __future__ import annotations

import json
import sqlite3
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from typing import Any

from pipeline.tracker.db import DEFAULT_DB_PATH

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Job Pipeline Dashboard</title>
<style>
  :root {
    --bg: #0f172a; --surface: #1e293b; --border: #334155;
    --text: #e2e8f0; --muted: #94a3b8; --accent: #38bdf8;
    --green: #4ade80; --red: #f87171; --yellow: #fbbf24;
    --orange: #fb923c;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: var(--bg); color: var(--text); }
  .container { max-width: 1400px; margin: 0 auto; padding: 20px; }

  /* Header */
  .header { display: flex; justify-content: space-between; align-items: center;
            margin-bottom: 24px; flex-wrap: wrap; gap: 12px; }
  .header h1 { font-size: 24px; font-weight: 700; }
  .header h1 span { color: var(--accent); }

  /* Stats bar */
  .stats { display: flex; gap: 16px; margin-bottom: 20px; flex-wrap: wrap; }
  .stat-card { background: var(--surface); border: 1px solid var(--border);
               border-radius: 8px; padding: 16px 20px; min-width: 120px; }
  .stat-card .value { font-size: 28px; font-weight: 700; color: var(--accent); }
  .stat-card .label { font-size: 12px; color: var(--muted); text-transform: uppercase;
                      letter-spacing: 0.5px; margin-top: 4px; }

  /* Filters */
  .filters { display: flex; gap: 12px; margin-bottom: 20px; flex-wrap: wrap; align-items: center; }
  .filters input, .filters select {
    background: var(--surface); border: 1px solid var(--border); color: var(--text);
    padding: 8px 12px; border-radius: 6px; font-size: 14px; outline: none;
  }
  .filters input:focus, .filters select:focus { border-color: var(--accent); }
  .filters input { width: 260px; }
  .filters label { font-size: 13px; color: var(--muted); }

  /* Table */
  .table-wrap { overflow-x: auto; border-radius: 8px; border: 1px solid var(--border); }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  thead { background: var(--surface); }
  th { padding: 12px 16px; text-align: left; font-weight: 600; color: var(--muted);
       font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px;
       cursor: pointer; user-select: none; white-space: nowrap; }
  th:hover { color: var(--accent); }
  th.sorted-asc::after { content: " ▲"; color: var(--accent); }
  th.sorted-desc::after { content: " ▼"; color: var(--accent); }
  td { padding: 10px 16px; border-top: 1px solid var(--border); }
  tr:hover { background: rgba(56, 189, 248, 0.05); }

  /* Score badge */
  .score { font-weight: 700; padding: 3px 10px; border-radius: 12px;
           font-size: 13px; display: inline-block; min-width: 40px; text-align: center; }
  .score-high { background: rgba(74, 222, 128, 0.15); color: var(--green); }
  .score-med { background: rgba(251, 191, 36, 0.15); color: var(--yellow); }
  .score-low { background: rgba(248, 113, 113, 0.15); color: var(--red); }

  /* Sponsorship badge */
  .sponsor { font-size: 12px; padding: 2px 8px; border-radius: 10px; }
  .sponsor-high { background: rgba(74, 222, 128, 0.15); color: var(--green); }
  .sponsor-medium { background: rgba(251, 191, 36, 0.15); color: var(--yellow); }
  .sponsor-low { background: rgba(248, 113, 113, 0.15); color: var(--red); }
  .sponsor-unknown { background: rgba(148, 163, 184, 0.15); color: var(--muted); }
  .jd-blocked { background: rgba(248, 113, 113, 0.2); color: var(--red); font-weight: 600; }

  /* Status */
  .status { font-size: 12px; padding: 2px 8px; border-radius: 10px;
            background: rgba(148, 163, 184, 0.15); color: var(--muted); }
  .status-scored { background: rgba(56, 189, 248, 0.15); color: var(--accent); }
  .status-applied { background: rgba(74, 222, 128, 0.15); color: var(--green); }
  .status-interview { background: rgba(251, 191, 36, 0.15); color: var(--yellow); }
  .status-rejected { background: rgba(248, 113, 113, 0.15); color: var(--red); }

  a { color: var(--accent); text-decoration: none; }
  a:hover { text-decoration: underline; }
  .company-name { font-weight: 600; }
  .location { color: var(--muted); font-size: 13px; }
  .filings { color: var(--muted); font-size: 12px; }

  .empty { text-align: center; padding: 60px 20px; color: var(--muted); }
  .empty p { margin-top: 8px; font-size: 14px; }

  /* Responsive */
  @media (max-width: 768px) {
    .filters input { width: 100%; }
    .stats { gap: 8px; }
    .stat-card { flex: 1; min-width: 80px; padding: 10px; }
    .stat-card .value { font-size: 20px; }
  }
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>🎯 Job <span>Pipeline</span></h1>
    <div style="color:var(--muted);font-size:13px">
      Last refresh: <span id="refreshTime">—</span>
    </div>
  </div>

  <div id="runBanner" style="color:var(--muted);font-size:13px;margin-bottom:12px"></div>

  <div class="stats" id="stats"></div>

  <div class="filters">
    <select id="scope" onchange="loadJobs()">
      <option value="latest">Latest run</option>
      <option value="all">All saved jobs</option>
    </select>
    <input type="text" id="search" placeholder="Search company, title, location..."
           oninput="filterJobs()">
    <select id="minScore" onchange="filterJobs()">
      <option value="0">All scores</option>
      <option value="50">Score ≥ 50</option>
      <option value="60">Score ≥ 60</option>
      <option value="70">Score ≥ 70</option>
      <option value="80">Score ≥ 80</option>
    </select>
    <select id="sponsorFilter" onchange="filterJobs()">
      <option value="all">All sponsorship</option>
      <option value="high">H-1B: High</option>
      <option value="medium">H-1B: Medium+</option>
      <option value="eligible">JD eligible only</option>
    </select>
    <select id="statusFilter" onchange="filterJobs()">
      <option value="all">All statuses</option>
      <option value="scraped">Scraped</option>
      <option value="scored">Scored</option>
      <option value="applied">Applied</option>
      <option value="interview">Interview</option>
    </select>
    <select id="postedFilter" onchange="filterJobs()">
      <option value="0">Posted: any time</option>
      <option value="1">Last 24 hours</option>
      <option value="7">Last 7 days</option>
      <option value="30">Last 30 days</option>
    </select>
  </div>

  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th onclick="sortBy('fit_score')" data-col="fit_score" class="sorted-desc">Score</th>
          <th onclick="sortBy('age_hours')" data-col="age_hours">Posted</th>
          <th onclick="sortBy('company')">Company</th>
          <th onclick="sortBy('title')">Title</th>
          <th onclick="sortBy('location')">Location</th>
          <th onclick="sortBy('sponsorship')">H-1B</th>
          <th onclick="sortBy('h1b_filings_count')">Filings</th>
          <th onclick="sortBy('platform')">ATS</th>
          <th onclick="sortBy('status')">Status</th>
          <th>Apply</th>
        </tr>
      </thead>
      <tbody id="jobsBody"></tbody>
    </table>
  </div>
  <div class="empty" id="emptyState" style="display:none">
    <h2>No jobs found</h2>
    <p>Run <code>jobpipe run</code> to scrape and score jobs, then refresh.</p>
  </div>
</div>

<script>
let allJobs = [];
let sortCol = 'fit_score';
let sortDir = 'desc';

async function loadJobs() {
  const scope = document.getElementById('scope').value;
  const resp = await fetch('/api/jobs?scope=' + scope);
  const data = await resp.json();
  allJobs = data.jobs;

  // Say exactly which jobs are on screen, matching the CLI's last run
  const banner = document.getElementById('runBanner');
  const r = data.run;
  if (data.scope === 'latest' && r) {
    const when = new Date(r.started_at).toLocaleString();
    const win = r.max_age_days ? `posted in the last ${r.max_age_days}d` : 'any posting date';
    banner.textContent = `Showing latest run: ${when} · ${win} · ${r.job_count} jobs ` +
      `(${data.saved_total} saved in total — pick "All saved jobs" to see them)`;
  } else if (!r) {
    banner.textContent = `Showing all ${data.saved_total} saved jobs (no run recorded yet — run "jobpipe run")`;
  } else {
    banner.textContent = `Showing all ${data.saved_total} saved jobs from every run`;
  }
  // Hours since posting; undated jobs sort last and never pass a time filter
  allJobs.forEach(j => {
    const t = j.posted_at ? Date.parse(j.posted_at) : NaN;
    j.age_hours = isNaN(t) ? Infinity : (Date.now() - t) / 3.6e6;
  });
  document.getElementById('refreshTime').textContent = new Date().toLocaleTimeString();

  // Stats
  const stats = data.stats;
  const statsEl = document.getElementById('stats');
  statsEl.innerHTML = Object.entries(stats).map(([k, v]) =>
    `<div class="stat-card"><div class="value">${v}</div><div class="label">${k}</div></div>`
  ).join('');

  filterJobs();
}

function filterJobs() {
  const search = document.getElementById('search').value.toLowerCase();
  const minScore = parseInt(document.getElementById('minScore').value);
  const sponsor = document.getElementById('sponsorFilter').value;
  const status = document.getElementById('statusFilter').value;
  const postedDays = parseInt(document.getElementById('postedFilter').value);

  let filtered = allJobs.filter(j => {
    if (postedDays && !(j.age_hours <= postedDays * 24)) return false;
    if (j.fit_score < minScore) return false;
    if (search && !(j.company+j.title+j.location).toLowerCase().includes(search)) return false;
    if (sponsor === 'high' && j.sponsorship !== 'high') return false;
    if (sponsor === 'medium' && !['high','medium'].includes(j.sponsorship)) return false;
    if (sponsor === 'eligible' && j.jd_blocked) return false;
    if (status !== 'all' && j.status !== status) return false;
    return true;
  });

  // Sort
  filtered.sort((a, b) => {
    let va = a[sortCol], vb = b[sortCol];
    if (typeof va === 'string') { va = va.toLowerCase(); vb = vb.toLowerCase(); }
    if (va < vb) return sortDir === 'asc' ? -1 : 1;
    if (va > vb) return sortDir === 'asc' ? 1 : -1;
    return 0;
  });

  renderTable(filtered);
}

function sortBy(col) {
  if (sortCol === col) { sortDir = sortDir === 'asc' ? 'desc' : 'asc'; }
  else { sortCol = col; sortDir = col === 'fit_score' ? 'desc' : 'asc'; }

  document.querySelectorAll('th').forEach(th => {
    th.classList.remove('sorted-asc', 'sorted-desc');
    if (th.dataset.col === col) th.classList.add(sortDir === 'asc' ? 'sorted-asc' : 'sorted-desc');
  });

  filterJobs();
}

function fmtAge(h) {
  if (!isFinite(h)) return '—';
  if (h < 1) return 'now';
  if (h < 24) return Math.floor(h) + 'h';
  const d = Math.floor(h / 24);
  return d < 60 ? d + 'd' : Math.floor(d / 30) + 'mo';
}
function scoreClass(s) { return s >= 70 ? 'score-high' : s >= 50 ? 'score-med' : 'score-low'; }
function sponsorClass(s) { return 'sponsor-' + s; }
function statusClass(s) {
  if (['scored','resume_generated'].includes(s)) return 'status-scored';
  if (s === 'applied') return 'status-applied';
  if (s === 'interview') return 'status-interview';
  if (['rejected','ghosted'].includes(s)) return 'status-rejected';
  return '';
}

function renderTable(jobs) {
  const body = document.getElementById('jobsBody');
  const empty = document.getElementById('emptyState');

  if (!jobs.length) { body.innerHTML = ''; empty.style.display = 'block'; return; }
  empty.style.display = 'none';

  body.innerHTML = jobs.map(j => `<tr>
    <td><span class="score ${scoreClass(j.fit_score)}">${Math.round(j.fit_score)}</span></td>
    <td><span class="location">${fmtAge(j.age_hours)}</span></td>
    <td><span class="company-name">${esc(j.company)}</span></td>
    <td>${esc(j.title)}</td>
    <td><span class="location">${esc(j.location)}</span></td>
    <td>${j.jd_blocked
      ? '<span class="sponsor jd-blocked">⛔ NO</span>'
      : '<span class="sponsor ' + sponsorClass(j.sponsorship) + '">' + j.sponsorship + '</span>'}</td>
    <td><span class="filings">${j.h1b_filings_count || '—'}</span></td>
    <td>${j.platform}</td>
    <td><span class="status ${statusClass(j.status)}">${j.status}</span></td>
    <td><a href="${esc(j.url)}" target="_blank">Apply →</a></td>
  </tr>`).join('');
}

function esc(s) { const d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }

loadJobs();
// Auto-refresh every 60s
setInterval(loadJobs, 60000);
</script>
</body>
</html>"""


class DashboardHandler(SimpleHTTPRequestHandler):
    """HTTP handler serving the dashboard and API."""

    db_path: Path = DEFAULT_DB_PATH

    def do_GET(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path == "/" or parsed.path == "/index.html":
            self._send_html(DASHBOARD_HTML)
        elif parsed.path == "/api/jobs":
            scope = parse_qs(parsed.query).get("scope", ["latest"])[0]
            self._send_json(self._get_jobs_data(scope))
        else:
            self.send_error(404)

    def _get_jobs_data(self, scope: str = "latest") -> dict[str, Any]:
        """Jobs from the latest pipeline run (default) or every saved job."""
        from pipeline.tracker.db import Tracker
        try:
            tracker = Tracker(self.db_path)
            run = tracker.latest_run()
            # "latest" = only what the most recent `jobpipe run` returned.
            # Before any run was recorded (old databases), fall back to all.
            run_id = run["id"] if (scope == "latest" and run) else None
            rows = tracker.get_jobs(run_id)
            stats = tracker.get_stats(run_id)
            saved_total = tracker.get_stats(None)["total"]

            jobs = []
            for job in rows:
                # Parse fit_breakdown from JSON string
                try:
                    job["fit_breakdown"] = json.loads(job.get("fit_breakdown", "{}"))
                except (json.JSONDecodeError, TypeError):
                    job["fit_breakdown"] = {}
                # Check if JD blocks sponsorship (stored in description analysis)
                # For now, check the description for no-sponsorship language
                from pipeline.filters.sponsorship import check_jd_blocks_sponsorship
                job["jd_blocked"] = check_jd_blocks_sponsorship(job.get("description", ""))
                jobs.append(job)

            return {
                "jobs": jobs, "stats": stats, "run": run,
                "scope": "latest" if run_id is not None else "all",
                "saved_total": saved_total,
            }
        except Exception as e:
            return {"jobs": [], "stats": {"error": str(e)}, "run": None,
                    "scope": scope, "saved_total": 0}

    def _send_html(self, content: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(content.encode())

    def _send_json(self, data: dict) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, default=str).encode())

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress default access logs."""
        pass


def run_dashboard(host: str = "0.0.0.0", port: int = 8050, db_path: Path | None = None) -> None:
    """Start the dashboard web server."""
    if db_path:
        DashboardHandler.db_path = db_path

    server = HTTPServer((host, port), DashboardHandler)
    print(f"🎯 Dashboard running at http://localhost:{port}")
    print("   Press Ctrl+C to stop\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down dashboard...")
        server.shutdown()
