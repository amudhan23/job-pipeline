"""Web dashboard for browsing and filtering scraped jobs.

Run with: jobpipe dashboard
Opens at http://localhost:8050
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
from http.cookies import SimpleCookie
from http.server import HTTPServer, SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from typing import Any

from pipeline.dashboard.routes import ApiMixin
from pipeline.tracker.db import DEFAULT_DB_PATH

STATIC_DIR = Path(__file__).resolve().parent / "static"
TOKEN_ENV = "JOBPIPE_DASHBOARD_TOKEN"
COOKIE = "jobpipe_session"

LOGIN_HTML = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Sign in · jobpipe</title>
<link rel="stylesheet" href="/static/app.css"></head>
<body class="login"><form method="post" action="/login" class="login-card">
<h1>jobpipe</h1><p>This dashboard holds your applications and private details. Enter the access
token you set as JOBPIPE_DASHBOARD_TOKEN.</p>
<label for="t">Access token</label><input id="t" name="token" type="password" autocomplete="current-password" autofocus>
<button type="submit">Sign in</button>__ERR__</form></body></html>"""


def _session_value(token: str) -> str:
    return hmac.new(token.encode(), b"jobpipe-dashboard-session", hashlib.sha256).hexdigest()

PROFILE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>My details · Job Pipeline</title>
<style>
  @import url("https://fonts.googleapis.com/css2?family=Schibsted+Grotesk:wght@400;500;600;700;800&display=swap");
  :root { --bg:#eef1f6; --surface:#ffffff; --border:#d6dce6; --text:#17202e;
          --muted:#5c6779; --accent:#2848d8; --green:#18875a; --red:#c2362f; --yellow:#b7791f; }
  * { margin:0; padding:0; box-sizing:border-box; }
  body { font-family:"Schibsted Grotesk",ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif; background:var(--bg); color:var(--text); }
  .topnav { display:flex; gap:18px; align-items:center; font-weight:600; font-size:14px; margin-bottom:6px; flex-wrap:wrap; }
  .topnav b { font-weight:800; font-size:18px; margin-right:8px; } .topnav b span { color:#e8336d; }
  .wrap { max-width:760px; margin:0 auto; padding:24px 20px 80px; }
  a { color:var(--accent); text-decoration:none; }
  h1 { font-size:28px; margin-bottom:6px; font-weight:800; letter-spacing:-0.02em; }
  .sub { color:var(--muted); font-size:14px; margin-bottom:22px; line-height:1.5; }
  .card { background:var(--surface); border:1px solid var(--border); border-radius:6px; padding:20px; margin-bottom:18px; }
  .drop { border:2px dashed var(--border); border-radius:10px; padding:22px; text-align:center; cursor:pointer; color:var(--muted); }
  .drop:hover, .drop.over { border-color:var(--accent); color:var(--text); }
  h2 { font-size:17px; font-weight:700; color:var(--text); margin:2px 0 14px; }
  .grid { display:grid; grid-template-columns:1fr 1fr; gap:14px; }
  .full { grid-column:1 / -1; }
  label { display:block; font-size:13px; margin-bottom:5px; }
  input, select, textarea { width:100%; background:#fff; border:1px solid var(--border); color:var(--text); font:inherit;
                  padding:9px 11px; border-radius:6px; font-size:14px; outline:none; }
  input:focus, select:focus { border-color:var(--accent); }
  .filled { border-color:var(--yellow) !important; box-shadow:0 0 0 2px rgba(183,121,31,.15); }
  .help { color:var(--muted); font-size:12px; margin-top:4px; }
  .bar { position:sticky; bottom:0; background:var(--bg); padding:14px 0; display:flex; gap:12px; align-items:center;
         border-top:1px solid var(--border); }
  button { background:var(--accent); color:#fff; border:0; padding:10px 22px; border-radius:6px; font-weight:600; font-size:14px; cursor:pointer; font-family:inherit; }
  #status { font-size:13px; }
  .ok { color:var(--green); } .err { color:var(--red); } .warn { color:var(--yellow); }
  .lock { font-size:12px; color:var(--muted); margin-top:10px; }
  @media (max-width:600px) { .grid { grid-template-columns:1fr; } }
</style>
</head>
<body>
<div class="wrap">
  <nav class="topnav"><b>job<span>pipe</span></b><a href="/#/jobs">Live jobs</a><a href="/#/attention">Needs you</a>
    <a href="/#/applied">Applied</a><a href="/#/auto">Auto-apply</a><a href="/#/crawler">Crawler</a></nav>
  <h1 style="margin-top:14px">My details</h1>
  <p class="sub">The answers application forms ask for. Upload your resume to pre-fill what it contains —
    pre-filled boxes are <span class="warn">highlighted</span> so you can check them.
    Nothing you typed is ever overwritten.</p>

  <div class="card">
    <div class="drop" id="drop">
      <strong id="resumeLabel">Upload resume (PDF)</strong><br>
      <span style="font-size:13px">Click or drop a file · it's stored on this machine and attached to applications</span>
    </div>
    <input type="file" id="file" accept="application/pdf" hidden>
    <div class="lock">🔒 Saved only on this computer (data/private/, excluded from git). Never committed to your public repo.</div>
  </div>

  <div id="missing" class="card warn" style="display:none"></div>

  <div class="card" id="qcard" style="display:none">
    <h2>Questions waiting for you</h2>
    <p class="help" style="margin:-6px 0 14px">From applications the bot couldn't finish, grouped by company. An answer applies only to
      that company unless you tick “use for every company”.</p>
    <div id="pending"></div>
    <button type="button" id="saveAnswers" style="margin-top:6px">Save answers</button>
    <span id="astatus" style="margin-left:10px;font-size:13px"></span>
  </div>

  <form id="form"></form>

  <div class="card" id="savedcard" style="display:none">
    <h2>Your saved answers</h2>
    <p class="help" style="margin:-6px 0 14px">Clear a box and save to forget an answer.</p>
    <div id="saved"></div>
    <button type="button" id="saveSaved" style="margin-top:6px">Save changes</button>
  </div>

  <div class="card" id="aicard">
    <h2>AI (Claude API)</h2>
    <p class="help" style="margin:-6px 0 14px">Drafts answers to open questions (“Why us?”, “What exceptional work…”)
      from your resume and the job description, using your own Anthropic API credits. It never answers
      legal, eligibility or demographic questions, and stops at your monthly budget.</p>
    <div class="grid">
      <div class="full"><label>API key</label>
        <input id="aikey" type="password" autocomplete="off" placeholder="sk-ant-…">
        <div class="help" id="aikeyinfo"></div></div>
      <div><label>Model</label><select id="aimodel">
        <option value="sonnet">Sonnet — good writing, ~1¢ per answer</option>
        <option value="haiku">Haiku — cheapest</option>
        <option value="opus">Opus — strongest</option></select></div>
      <div><label>Monthly budget (USD)</label><input id="aibudget" type="number" min="0" max="1000" step="0.5"></div>
      <div class="full"><label style="display:flex;gap:8px;align-items:center">
        <input id="aienabled" type="checkbox" style="width:auto"> Use AI drafts when applying</label></div>
    </div>
    <div class="help" id="aispend" style="margin:10px 0"></div>
    <button type="button" id="aisave">Save AI settings</button>
    <button type="button" id="aiforget" style="background:var(--border);color:var(--text);margin-left:8px">Delete key</button>
    <span id="aistatus" style="margin-left:10px;font-size:13px"></span>
    <div class="lock">🔒 The key is stored in data/private/ on this computer only, and never shown again in full.</div>
  </div>

  <div class="bar"><button id="save">Save</button><span id="status"></span></div>
</div>

<script>
let fields = [];

function esc(s) { const d = document.createElement('div'); d.textContent = s ?? ''; return d.innerHTML; }

function render(data) {
  fields = data.fields;
  const form = document.getElementById('form');
  let html = '', open = false;
  for (const f of fields) {
    if (f.section) {
      if (open) html += '</div></div>';
      html += `<div class="card"><h2>${esc(f.section)}</h2><div class="grid">`;
      open = true; continue;
    }
    const v = data.values[f.key] ?? '';
    const wide = f.label.length > 38 ? ' full' : '';
    let input;
    if (f.kind === 'choice') {
      input = `<select id="${f.key}">` + f.options.map(o =>
        `<option value="${esc(o)}"${o === v ? ' selected' : ''}>${o ? esc(o) : '— choose —'}</option>`).join('') + '</select>';
    } else {
      const type = {email:'email', tel:'tel', url:'url'}[f.kind] || 'text';
      input = `<input id="${f.key}" type="${type}" value="${esc(v)}">`;
    }
    html += `<div class="${wide.trim()}"><label for="${f.key}">${esc(f.label)}</label>${input}` +
            (f.help ? `<div class="help">${esc(f.help)}</div>` : '') + '</div>';
  }
  if (open) html += '</div></div>';
  form.innerHTML = html;
  showMeta(data);
}

function showMeta(data) {
  document.getElementById('resumeLabel').textContent =
    data.has_resume ? '✓ Resume on file — click to replace' : 'Upload resume (PDF)';
  const m = document.getElementById('missing');
  if (data.missing && data.missing.length) {
    m.style.display = 'block';
    m.innerHTML = '<strong>Auto-apply still needs:</strong> ' + data.missing.map(esc).join(', ');
  } else { m.style.display = 'none'; }
}

function values() {
  const out = {};
  for (const f of fields) if (f.key) out[f.key] = document.getElementById(f.key).value;
  return out;
}

function status(msg, cls) { const s = document.getElementById('status'); s.textContent = msg; s.className = cls; }

async function load() {
  const r = await fetch('/api/profile');
  render(await r.json());
}

async function save(e) {
  e && e.preventDefault();
  const r = await fetch('/api/profile', {method:'POST', headers:{'Content-Type':'application/json'},
                                         body: JSON.stringify(values())});
  const data = await r.json();
  if (!r.ok) return status(data.error || 'Save failed', 'err');
  document.querySelectorAll('.filled').forEach(el => el.classList.remove('filled'));
  showMeta(data);
  status('Saved ✓', 'ok');
}

async function upload(file) {
  if (!file) return;
  if (file.type !== 'application/pdf') return status('Please choose a PDF', 'err');
  status('Reading resume…', '');
  const r = await fetch('/api/resume', {method:'POST', headers:{'Content-Type':'application/pdf'}, body:file});
  const data = await r.json();
  if (!r.ok) return status(data.error || 'Upload failed', 'err');
  let n = 0;
  for (const [k, v] of Object.entries(data.found || {})) {
    const el = document.getElementById(k);
    if (el && !el.value.trim()) { el.value = v; el.classList.add('filled'); n++; }
  }
  document.getElementById('resumeLabel').textContent = '✓ Resume on file — click to replace';
  status(n ? `Resume saved. Pre-filled ${n} field${n>1?'s':''} — check the highlighted boxes, then Save.`
           : 'Resume saved. Nothing new to pre-fill.', n ? 'warn' : 'ok');
}

// ── Questions waiting for you / saved answers ──
// Answers apply only to the company that asked, unless "use for every
// company" is ticked — company-specific answers stay out of your profile.
const inputStyle = 'width:100%;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:9px';

function answerInput(q, company, options, value, multi) {
  const attrs = `data-q="${esc(q)}" data-company="${esc(company)}"`;
  const opts = (options || []).filter(o => o && !/^(select|choose)/i.test(o));
  if (opts.length && multi) {
    // "Select all that apply": tick as many as fit; saved as "A, B"
    return `<div ${attrs} data-multi>` + opts.map(o =>
      `<label style="display:flex;gap:6px;align-items:center;font-weight:normal">
         <input type="checkbox" value="${esc(o)}" style="width:auto"> ${esc(o)}</label>`).join('') + '</div>';
  }
  if (opts.length) {
    return `<select ${attrs}><option value="">— choose —</option>` +
      opts.map(o => `<option${o === value ? ' selected' : ''}>${esc(o)}</option>`).join('') + '</select>';
  }
  return /why|describe|tell us|explain/i.test(q)
    ? `<textarea ${attrs} rows="3" style="${inputStyle}">${esc(value || '')}</textarea>`
    : `<input ${attrs} value="${esc(value || '')}">`;
}

async function loadAnswers() {
  const data = await (await fetch('/api/answers')).json();
  const pending = Object.entries(data.pending || {});
  document.getElementById('qcard').style.display = pending.length ? 'block' : 'none';
  document.getElementById('pending').innerHTML = pending.map(([company, qs]) =>
    `<h2 style="margin-top:10px">${esc(company)}</h2>` +
    Object.entries(qs).map(([q, p]) =>
      `<div style="margin-bottom:16px"><label>${esc(p.label)}</label>${p.draft ? '<div class="help" style="color:#e879f9;margin-bottom:4px">✎ AI draft — edit it, then save to make it your answer</div>' : ''}${answerInput(q, company, p.options, p.draft || '', p.multi)}
       <label style="font-size:12px;color:var(--muted);margin-top:6px;display:flex;gap:6px;align-items:center">
         <input type="checkbox" data-every style="width:auto"> Use this answer for every company</label></div>`
    ).join('')).join('');

  const saved = data.saved || {all: {}, companies: {}};
  const groups = [];
  if (Object.keys(saved.all || {}).length) groups.push(['Every company', '', saved.all]);
  for (const [c, qs] of Object.entries(saved.companies || {})) groups.push([c, c, qs]);
  document.getElementById('savedcard').style.display = groups.length ? 'block' : 'none';
  document.getElementById('saved').innerHTML = groups.map(([title, company, qs]) =>
    `<h2 style="margin-top:10px">${esc(title)}</h2>` +
    Object.entries(qs).map(([q, a]) =>
      `<div style="margin-bottom:12px"><label>${esc(q)}</label>
       <input data-q="${esc(q)}" data-company="${esc(company)}" ${company ? '' : 'data-everyfixed'} value="${esc(a)}"></div>`
    ).join('')).join('');
}

async function postAnswers(containerId, keepEmpty) {
  const answers = [];
  document.querySelectorAll(`#${containerId} [data-q]`).forEach(el => {
    const value = el.hasAttribute('data-multi')
      ? [...el.querySelectorAll('input:checked')].map(b => b.value).join(', ')
      : el.value.trim();
    if (!value && !keepEmpty) return;
    const box = el.parentElement.querySelector('[data-every]');
    answers.push({question: el.dataset.q, company: el.dataset.company, answer: value,
                  everywhere: el.hasAttribute('data-everyfixed') || !!(box && box.checked)});
  });
  const r = await fetch('/api/answers', {method:'POST', headers:{'Content-Type':'application/json'},
                                         body: JSON.stringify({answers})});
  document.getElementById('astatus').textContent = r.ok ? 'Saved ✓' : 'Save failed';
  loadAnswers();
}
async function loadAI(data) {
  data = data || await (await fetch('/api/ai')).json();
  document.getElementById('aikeyinfo').textContent = data.key ? `Saved: ${data.key}` + (data.key_from_env ? ' (from ANTHROPIC_API_KEY)' : '') : 'No key saved yet — create one at console.anthropic.com → API keys.';
  document.getElementById('aimodel').value = data.settings.model;
  document.getElementById('aibudget').value = data.settings.monthly_budget;
  document.getElementById('aienabled').checked = !!data.settings.enabled;
  const s = data.spend;
  document.getElementById('aispend').innerHTML =
    `This month: <b>$${s.month_spent.toFixed(4)}</b> of $${s.monthly_budget.toFixed(2)} (${s.month_calls} calls) · all time $${s.total_spent.toFixed(4)}` +
    (data.ready ? ' · <span class="ok">ready</span>' : ` · <span class="warn">${esc(data.why)}</span>`);
}
async function postAI(body) {
  const r = await fetch('/api/ai', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  const data = await r.json();
  const st = document.getElementById('aistatus');
  if (!r.ok) { st.textContent = data.error || 'Save failed'; st.className = 'err'; return; }
  st.textContent = 'Saved ✓'; st.className = 'ok';
  document.getElementById('aikey').value = '';
  loadAI(data);
}
document.getElementById('aisave').onclick = () => {
  const body = {model: document.getElementById('aimodel').value,
                monthly_budget: parseFloat(document.getElementById('aibudget').value || '0'),
                enabled: document.getElementById('aienabled').checked};
  const key = document.getElementById('aikey').value.trim();
  if (key) body.key = key;
  postAI(body);
};
document.getElementById('aiforget').onclick = () => postAI({forget_key: true});
loadAI();

document.getElementById('saveAnswers').onclick = () => postAnswers('pending', false);
document.getElementById('saveSaved').onclick = () => postAnswers('saved', true);
loadAnswers();

const drop = document.getElementById('drop'), file = document.getElementById('file');
drop.onclick = () => file.click();
file.onchange = () => upload(file.files[0]);
drop.ondragover = e => { e.preventDefault(); drop.classList.add('over'); };
drop.ondragleave = () => drop.classList.remove('over');
drop.ondrop = e => { e.preventDefault(); drop.classList.remove('over'); upload(e.dataTransfer.files[0]); };
document.getElementById('save').onclick = save;
document.getElementById('form').onsubmit = save;
load();
</script>
</body>
</html>"""


class DashboardHandler(ApiMixin, SimpleHTTPRequestHandler):
    """HTTP handler serving the dashboard and API."""

    db_path: Path = DEFAULT_DB_PATH
    private_dir: Path | None = None  # None → applicant.PRIVATE_DIR

    # ── Local-only guard ────────────────────────────────────────────────
    # The server binds 127.0.0.1, and also rejects requests whose Host or
    # Origin isn't localhost (stops DNS-rebinding and cross-site reads of
    # your private details from other websites open in your browser).
    _LOCAL = {"localhost", "127.0.0.1", "[::1]"}

    def _is_local_request(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].lower()
        if host not in self._LOCAL:
            return False
        origin = self.headers.get("Origin")
        if origin:
            o = urlparse(origin).hostname or ""
            if o.lower() not in {"localhost", "127.0.0.1", "::1"}:
                return False
        return True

    # ── Remote mode (cloud): an access token instead of "this machine only" ──

    def _token(self) -> str:
        return os.environ.get(TOKEN_ENV, "")

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return (urlparse(origin).netloc or "").lower() == (self.headers.get("Host") or "").lower()

    def _authorized(self) -> bool:
        token = self._token()
        if not token:
            return self._is_local_request()
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].strip(), token):
            return True
        jar = SimpleCookie(self.headers.get("Cookie") or "")
        got = jar.get(COOKIE)
        return bool(got and hmac.compare_digest(got.value, _session_value(token)) and self._same_origin())

    def _deny(self, api: bool) -> None:
        if self._token():
            if api:
                return self._send_json({"error": "Sign in first"}, 401)
            self.send_response(303)
            self.send_header("Location", "/login")
            self.end_headers()
            return
        self.send_error(403, "Dashboard only answers requests from this machine")

    def _login(self) -> None:
        token = self._token()
        length = min(int(self.headers.get("Content-Length") or 0), 4096)
        given = parse_qs(self.rfile.read(length).decode("utf-8", "replace")).get("token", [""])[0]
        if not token or not hmac.compare_digest(given.strip(), token):
            return self._send_html(LOGIN_HTML.replace("__ERR__", '<p class="err">That token is wrong.</p>'), 401)
        secure = "; Secure" if (self.headers.get("X-Forwarded-Proto") == "https") else ""
        self.send_response(303)
        self.send_header("Set-Cookie", f"{COOKIE}={_session_value(token)}; HttpOnly; SameSite=Strict; "
                                       f"Path=/; Max-Age={30 * 86400}{secure}")
        self.send_header("Location", "/")
        self.end_headers()

    def _pdir(self) -> Path:
        from pipeline import applicant
        return self.private_dir or applicant.PRIVATE_DIR

    def _tracker(self):
        from pipeline.tracker.db import Tracker
        return Tracker(self.db_path)

    def _send_static(self, name: str) -> None:
        target = (STATIC_DIR / name).resolve()
        if STATIC_DIR.resolve() not in target.parents or not target.is_file():
            return self.send_error(404)
        ctype = {".js": "text/javascript", ".css": "text/css", ".html": "text/html",
                 ".svg": "image/svg+xml"}.get(target.suffix, "application/octet-stream")
        self._send_bytes(target.read_bytes(), ctype + ("; charset=utf-8" if ctype.startswith("text") else ""))

    def _send_bytes(self, data: bytes, ctype: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/login":
            if not self._token():
                return self.send_error(404)
            return self._send_html(LOGIN_HTML.replace("__ERR__", ""))
        if parsed.path.startswith("/static/") and parsed.path.endswith(".css"):
            return self._send_static(parsed.path[len("/static/"):])     # the login page needs it
        if not self._authorized():
            return self._deny(parsed.path.startswith("/api/"))

        if parsed.path == "/" or parsed.path == "/index.html":
            self._send_static("index.html")
        elif parsed.path.startswith("/static/"):
            self._send_static(parsed.path[len("/static/"):])
        elif parsed.path.startswith("/files/"):
            got = self.file_response(parsed.path)
            if not got:
                return self.send_error(404)
            self._send_bytes(*got)
        elif parsed.path == "/profile":
            self._send_html(PROFILE_HTML)
        elif parsed.path == "/api/jobs":
            scope = parse_qs(parsed.query).get("scope", ["latest"])[0]
            self._send_json(self._get_jobs_data(scope))
        elif parsed.path == "/api/profile":
            self._send_json(self._profile_payload())
        elif parsed.path == "/api/answers":
            self._send_json(self._answers_payload())
        elif parsed.path == "/api/ai":
            self._send_json(self._ai_payload())
        else:
            try:
                got = self.api_get(parsed.path, parse_qs(parsed.query))
            except Exception as e:                      # show the problem instead of a dead page
                return self._send_json({"error": f"{type(e).__name__}: {e}"}, 500)
            if got is None:
                return self.send_error(404)
            self._send_json(*got)

    def do_POST(self) -> None:
        from pipeline import applicant
        path = urlparse(self.path).path
        if path == "/login" and self._token():
            return self._login()
        if not self._authorized():
            return self._deny(True)
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        length = int(self.headers.get("Content-Length") or 0)
        if length > applicant.MAX_RESUME_BYTES + 1024:
            return self._send_json({"error": "Upload too large (max 10 MB)"}, 413)
        body = self.rfile.read(length)

        try:
            if path in ("/api/profile", "/api/answers", "/api/ai"):
                if ctype != "application/json":  # forces a CORS preflight for cross-site posts
                    return self._send_json({"error": "Expected application/json"}, 415)
                data = json.loads(body or b"{}")
                if not isinstance(data, dict):
                    return self._send_json({"error": "Expected an object"}, 400)
                if path == "/api/ai":
                    from pipeline.ai import client as ai_client
                    if data.get("forget_key"):
                        ai_client.forget_key(self._pdir())
                    if data.get("key"):
                        ai_client.save_key(str(data["key"]), self._pdir())
                    ai_client.save_settings({k: data[k] for k in ("model", "monthly_budget", "enabled")
                                             if k in data}, self._pdir())
                    return self._send_json(self._ai_payload())
                if path == "/api/answers":
                    items = data.get("answers")
                    if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
                        return self._send_json({"error": "Expected {answers: [...]}"}, 400)
                    applicant.save_answers(items, self._pdir())
                    return self._send_json(self._answers_payload())
                applicant.save(data, self._pdir())
                return self._send_json(self._profile_payload())
            if path == "/api/resume":
                if ctype != "application/pdf":
                    return self._send_json({"error": "Upload a PDF"}, 415)
                applicant.save_resume(body, self._pdir())
                try:
                    found = applicant.parse_resume(applicant.resume_text(body))
                except Exception:
                    found = {}  # unreadable PDF text (e.g. scanned image) — still saved
                return self._send_json({"found": found, "has_resume": True})
            if ctype != "application/json":
                return self._send_json({"error": "Expected application/json"}, 415)
            data = json.loads(body or b"{}")
            if not isinstance(data, dict):
                return self._send_json({"error": "Expected an object"}, 400)
            got = self.api_post(path, data)
            if got is not None:
                return self._send_json(*got)
        except (ValueError, json.JSONDecodeError) as e:
            return self._send_json({"error": str(e)}, 400)
        self.send_error(404)

    def _ai_payload(self) -> dict[str, Any]:
        import os
        from pipeline.ai import client as ai_client
        ready, why = ai_client.status(self._pdir())
        return {"key": ai_client.masked_key(self._pdir()),        # never the whole key
                "key_from_env": bool(os.environ.get("ANTHROPIC_API_KEY")),
                "settings": ai_client.load_settings(self._pdir()),
                "spend": {k: v for k, v in ai_client.spend_summary(self._pdir()).items() if k != "recent"},
                "ready": ready, "why": why}

    def _answers_payload(self) -> dict[str, Any]:
        from pipeline import applicant
        return {"pending": applicant.load_pending(self._pdir()),
                "saved": applicant.load_answers(self._pdir())}

    def _profile_payload(self) -> dict[str, Any]:
        from pipeline import applicant
        values = applicant.load(self._pdir())
        return {
            "fields": applicant.FIELDS,
            "values": values,
            "has_resume": applicant.has_resume(self._pdir()),
            "missing": applicant.missing_required(values),
        }

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
                try:
                    job["ai_fit_parsed"] = json.loads(job.get("ai_fit") or "null")
                except (json.JSONDecodeError, TypeError):
                    job["ai_fit_parsed"] = None
                job["ai_verdict"] = (job["ai_fit_parsed"] or {}).get("verdict", "")
                job.pop("description", None)          # not shown; keeps the payload small
                jobs.append(job)

            return {
                "jobs": jobs, "stats": stats, "run": run,
                "scope": "latest" if run_id is not None else "all",
                "saved_total": saved_total,
            }
        except Exception as e:
            return {"jobs": [], "stats": {"error": str(e)}, "run": None,
                    "scope": scope, "saved_total": 0}

    def _send_html(self, content: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(content.encode())

    def _send_json(self, data: dict, status: int = 200) -> None:
        # No Access-Control-Allow-Origin header: other websites must not be
        # able to read this API through your browser.
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(json.dumps(data, default=str).encode())

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress default access logs."""
        pass


def run_dashboard(host: str = "127.0.0.1", port: int = 8050, db_path: Path | None = None) -> None:
    """Start the dashboard web server. Anything but this machine needs an
    access token (JOBPIPE_DASHBOARD_TOKEN) — it holds your private details."""
    if db_path:
        DashboardHandler.db_path = db_path
    if host not in ("127.0.0.1", "localhost", "::1") and not os.environ.get(TOKEN_ENV):
        raise SystemExit(f"Refusing to serve on {host} without {TOKEN_ENV} set — "
                         "the dashboard holds your private details.")
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    server.daemon_threads = True
    shown = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
    print(f"🎯 Dashboard running at http://{shown}:{port}")
    print("   Press Ctrl+C to stop\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down dashboard...")
        server.shutdown()
