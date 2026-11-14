"use strict";
/* jobpipe dashboard — Live jobs, Needs you, Applied, Auto-apply, Crawler. */

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const page = $("#page");
let timers = [];
const every = (ms, fn) => timers.push(setInterval(fn, ms));

function store(key, value) {
  try {
    if (value === undefined) return JSON.parse(localStorage.getItem("jobpipe:" + key) || "null");
    localStorage.setItem("jobpipe:" + key, JSON.stringify(value));
  } catch { return null; }
}

async function api(path, body) {
  const opts = body === undefined ? {} :
    { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const r = await fetch(path, opts);
  if (r.status === 401) { location.href = "/login"; throw new Error("Sign in first"); }
  const data = await r.json().catch(() => ({ error: `The server answered ${r.status}` }));
  if (!r.ok) throw new Error(data.error || `The server answered ${r.status}`);
  return data;
}

function toast(msg, isError = false) {
  const t = document.createElement("div");
  t.className = "toast" + (isError ? " err" : "");
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), isError ? 7000 : 4500);
}

function ago(min) {
  if (min == null) return "—";
  if (min < 1) return "now";
  if (min < 60) return `${Math.round(min)}m`;
  if (min < 48 * 60) return `${Math.round(min / 60)}h`;
  return `${Math.round(min / 1440)}d`;
}

function heat(min) {
  // Raspberry when fresh, cooling to the line colour over ~3 days
  if (min == null) return "var(--line)";
  const t = Math.min(1, Math.log(min + 1) / Math.log(4320));
  const a = [232, 51, 109], b = [214, 220, 230];
  const c = a.map((v, i) => Math.round(v + (b[i] - v) * t));
  return `rgb(${c.join(",")})`;
}

const scoreClass = s => (s >= 70 ? "hi" : s >= 55 ? "mid" : "lo");
const fitTag = j => {
  if (!j.ai_verdict) return "";
  const cls = { apply: "good", maybe: "caution", skip: "stop" }[j.ai_verdict] || "plain";
  const label = { apply: "Fits", maybe: "Check", skip: "Dealbreaker" }[j.ai_verdict] || j.ai_verdict;
  const tip = [j.ai_summary, ...(j.ai_dealbreakers || []).map(d => `${d.rule}: “${d.quote}”`)].join("\n");
  return `<span class="tag ${cls}" title="${esc(tip)}">${label}</span>`;
};
const statusTag = s => {
  const map = { applied: "good", interview: "good", offer: "good", rejected: "plain", skipped: "plain" };
  return s && s !== "scraped" ? `<span class="tag ${map[s] || "plain"}">${esc(s)}</span>` : "";
};

/* ── Actions shared by pages ─────────────────────────────── */

let canOpenBrowser = true;

async function runApply(jobIds, mode, label) {
  try {
    await api("/api/apply/run", { job_ids: jobIds, mode, label });
    toast({
      practice: "Filling the form in the background. It shows up under Needs you when it's done.",
      handoff: "A browser is opening with the form filled in. Finish it there and press Submit.",
      submit: "Submitting in the background. Watch it under Auto-apply.",
    }[mode]);
    refreshCounts();
  } catch (e) { toast(e.message, true); }
}

async function setStatus(id, status) {
  try {
    await api(`/api/jobs/${encodeURIComponent(id)}/status`, { status });
    toast(status === "applied" ? "Marked applied." : status === "skipped" ? "Skipped. It won't be offered again." : "Saved.");
    refreshCounts();
    return true;
  } catch (e) { toast(e.message, true); return false; }
}

/* ── Live jobs ───────────────────────────────────────────── */

const feedState = Object.assign({ hours: "24", minScore: 0, fit: "any", sponsor: "any", q: "", hideDone: true, sort: "age" },
  store("feed") || {});
let feedJobs = [], seenIds = null, notifyOn = store("notify") || false;

async function pageJobs() {
  page.innerHTML = `
    <div class="head"><div><h1>Live jobs</h1><p class="lede" id="crawl-line">Loading…</p></div>
      <button id="notify" class="quiet" title="Browser notification when a matching job opens"></button></div>
    <h2 style="margin-top:6px">Just opened</h2>
    <div class="ribbon" id="ribbon"></div>
    <div class="filters">
      <div class="seg" id="hours">
        ${[["1", "Last hour"], ["24", "Today"], ["72", "3 days"], ["all", "All open"]].map(([v, l]) =>
          `<button data-v="${v}" class="${feedState.hours === v ? "on" : ""}">${l}</button>`).join("")}
      </div>
      <label class="check">Score at least <input type="number" id="minScore" min="0" max="100" step="5" style="width:70px" value="${feedState.minScore}"></label>
      <select id="fit" aria-label="AI fit">
        <option value="any">Any AI fit</option><option value="ok">Hide dealbreakers</option><option value="apply">Fits only</option>
      </select>
      <select id="sponsor" aria-label="Sponsorship">
        <option value="any">Any sponsorship</option><option value="known">Sponsors H-1B (high/medium)</option>
      </select>
      <label class="check"><input type="checkbox" id="hideDone" ${feedState.hideDone ? "checked" : ""}> Hide applied and skipped</label>
      <input type="search" id="q" class="grow" placeholder="Search company, title, location" value="${esc(feedState.q)}">
    </div>
    <div class="table-wrap"><table>
      <thead><tr><th></th><th data-sort="age" class="sorted">Opened</th><th data-sort="title">Job</th>
        <th class="hide-sm">Location</th><th data-sort="score">Score</th><th class="hide-sm">AI fit</th>
        <th class="hide-sm">H-1B</th><th></th></tr></thead>
      <tbody id="rows"></tbody></table></div>`;
  $("#fit").value = feedState.fit; $("#sponsor").value = feedState.sponsor;
  $$("#hours button").forEach(b => b.onclick = () => { feedState.hours = b.dataset.v; saveFeed(); pageJobs(); });
  const rerender = () => { saveFeed(); renderRows(); };
  $("#minScore").oninput = e => { feedState.minScore = +e.target.value || 0; rerender(); };
  $("#fit").onchange = e => { feedState.fit = e.target.value; rerender(); };
  $("#sponsor").onchange = e => { feedState.sponsor = e.target.value; rerender(); };
  $("#hideDone").onchange = e => { feedState.hideDone = e.target.checked; rerender(); };
  $("#q").oninput = e => { feedState.q = e.target.value; rerender(); };
  $$("th[data-sort]").forEach(th => th.onclick = () => { feedState.sort = th.dataset.sort; saveFeed(); renderRows(); });
  $("#notify").onclick = toggleNotify; paintNotify();
  await loadFeed();
  every(20000, loadFeed);
}

function saveFeed() { store("feed", feedState); }

async function loadFeed() {
  let data;
  try { data = await api(`/api/feed?hours=${feedState.hours}`); }
  catch (e) { $("#crawl-line").textContent = e.message; return; }
  canOpenBrowser = data.can_open_browser;
  const fresh = new Set();
  if (seenIds) data.jobs.forEach(j => { if (!seenIds.has(j.id)) fresh.add(j.id); });
  seenIds = new Set(data.jobs.map(j => j.id));
  feedJobs = data.jobs;
  crawlLine(data.crawler);
  renderRibbon();
  renderRows(fresh);
  if (fresh.size && notifyOn) notifyAbout(data.jobs.filter(j => fresh.has(j.id)));
}

function crawlLine(st) {
  const el = $("#crawl-line");
  if (!el) return;
  if (st && st.running) {
    const f = st.frontier || {}, h = st.last_hour || {};
    el.textContent = `Watching ${f.boards || 0} company job boards. ${h.reads || 0} reads and ` +
      `${h.new_postings || 0} new postings in the last hour.`;
  } else {
    el.innerHTML = `The crawler isn't running, so this list only changes when you run <code>jobpipe run</code>. ` +
      `Start <code>jobpipe watch</code> to catch jobs minutes after they open.`;
  }
}

function filtered() {
  const q = feedState.q.trim().toLowerCase();
  let jobs = feedJobs.filter(j => {
    if (j.fit_score < feedState.minScore) return false;
    if (feedState.hideDone && ["applied", "interview", "offer", "rejected", "skipped"].includes(j.status)) return false;
    if (feedState.fit === "ok" && j.ai_verdict === "skip") return false;
    if (feedState.fit === "apply" && j.ai_verdict !== "apply") return false;
    if (feedState.sponsor === "known" && !["high", "medium"].includes(j.sponsorship)) return false;
    if (q && !`${j.company} ${j.title} ${j.location}`.toLowerCase().includes(q)) return false;
    return true;
  });
  const by = {
    age: (a, b) => (a.age_min ?? 1e9) - (b.age_min ?? 1e9),
    score: (a, b) => b.fit_score - a.fit_score,
    title: (a, b) => `${a.company}${a.title}`.localeCompare(`${b.company}${b.title}`),
  }[feedState.sort];
  return jobs.sort(by);
}

function renderRibbon() {
  const el = $("#ribbon");
  const hot = feedJobs.filter(j => j.age_min != null && j.age_min <= 60 && j.fit_score >= feedState.minScore &&
    !["applied", "skipped"].includes(j.status)).sort((a, b) => a.age_min - b.age_min).slice(0, 12);
  if (!hot.length) {
    el.outerHTML = `<div class="ribbon-empty" id="ribbon">Nothing in the last hour yet. New postings land here the minute the crawler sees them.</div>`;
    return;
  }
  el.className = "ribbon";
  el.innerHTML = hot.map(j => `
    <div class="opened" data-id="${esc(j.id)}" title="${esc(j.age_basis === "seen" ? "Minutes since the crawler first saw it" : "Minutes since the company posted it")}">
      <div class="mins">${esc(ago(j.age_min))}<small>${j.age_basis === "seen" ? "since it opened" : "since posted"}</small></div>
      <div class="who">${esc(j.company)}</div>
      <div class="what">${esc(j.title)}</div>
    </div>`).join("");
  $$(".opened", el).forEach(c => c.onclick = () => {
    const row = document.querySelector(`tr[data-id="${CSS.escape(c.dataset.id)}"]`);
    if (row) { row.scrollIntoView({ block: "center" }); row.classList.remove("flash"); void row.offsetWidth; row.classList.add("flash"); }
  });
}

function renderRows(fresh = new Set()) {
  const rows = $("#rows");
  if (!rows) return;
  $$("th[data-sort]").forEach(th => th.classList.toggle("sorted", th.dataset.sort === feedState.sort));
  const jobs = filtered();
  if (!jobs.length) {
    rows.innerHTML = `<tr><td colspan="8"><div class="empty"><strong>No jobs match these filters.</strong>
      Widen the time window or lower the score.</div></td></tr>`;
    return;
  }
  rows.innerHTML = jobs.slice(0, 600).map(j => `
    <tr data-id="${esc(j.id)}" class="${fresh.has(j.id) ? "flash" : ""}">
      <td class="heat"><i style="background:${heat(j.age_min)}"></i></td>
      <td class="age">${esc(ago(j.age_min))}<small>${j.age_basis === "seen" ? "seen" : j.age_basis === "posted" ? "posted" : ""}</small></td>
      <td class="title-cell"><a href="${esc(j.url)}" target="_blank" rel="noopener">${esc(j.title)}</a>
        <div class="co">${esc(j.company)} ${statusTag(j.status)}${j.jd_blocked ? ' <span class="tag stop">JD: no sponsorship</span>' : ""}</div></td>
      <td class="loc hide-sm" title="${esc(j.location)}">${esc(j.location)}</td>
      <td><span class="score ${scoreClass(j.fit_score)}">${j.fit_score}</span></td>
      <td class="hide-sm">${fitTag(j)}</td>
      <td class="hide-sm">${j.sponsorship && j.sponsorship !== "unknown" ? `<span class="tag plain" title="${j.h1b_filings_count || 0} H-1B filings">${esc(j.sponsorship)}</span>` : ""}</td>
      <td class="actions">${j.apply_supported ? `
        <button data-act="fill" title="Fill the form in the background; nothing is sent">Fill form</button>
        <button data-act="handoff" ${canOpenBrowser ? "title='Opens a browser with the form filled; you press Submit'" : "disabled title='Needs the dashboard running on your computer'"}>Open and finish</button>` :
        `<a class="btn" href="${esc(j.url)}" target="_blank" rel="noopener">Open posting</a>`}
        <button data-act="applied" class="quiet" title="I applied to this">Applied</button>
        <button data-act="skip" class="quiet" title="Don't show this again">Skip</button></td>
    </tr>`).join("");
  $$("button[data-act]", rows).forEach(b => b.onclick = async () => {
    const id = b.closest("tr").dataset.id, j = feedJobs.find(x => x.id === id);
    const act = b.dataset.act;
    if (act === "fill") runApply([id], "practice", `Fill: ${j.company}`);
    if (act === "handoff") runApply([id], "handoff", `Open and finish: ${j.company}`);
    if (act === "applied" && await setStatus(id, "applied")) { j.status = "applied"; renderRows(); }
    if (act === "skip" && await setStatus(id, "skipped")) { j.status = "skipped"; renderRows(); }
  });
}

function paintNotify() {
  const b = $("#notify");
  if (b) b.textContent = notifyOn ? "Notifications on" : "Notify me about new matches";
}

async function toggleNotify() {
  if (!("Notification" in window)) return toast("This browser can't show notifications.", true);
  if (!notifyOn) {
    const p = await Notification.requestPermission();
    if (p !== "granted") return toast("Notifications are blocked for this page in your browser settings.", true);
  }
  notifyOn = !notifyOn; store("notify", notifyOn); paintNotify();
  toast(notifyOn ? `You'll get a notification when a job scoring ${feedState.minScore}+ opens, while this tab is open.` : "Notifications off.");
}

function notifyAbout(jobs) {
  jobs.filter(j => j.fit_score >= feedState.minScore && j.age_min != null && j.age_min < 30).slice(0, 3).forEach(j => {
    try { new Notification(`${j.company} just opened a role`, { body: `${j.title} — score ${j.fit_score}`, tag: j.id }); } catch { /* ignored */ }
  });
}

/* ── Needs you ───────────────────────────────────────────── */

async function pageAttention() {
  page.innerHTML = `<div class="head"><div><h1>Needs you</h1>
    <p class="lede">Applications the bot filled as far as it could. Answer what's missing once below and it's reused
    for every form that asks again, or open one and finish it yourself.</p></div></div><div id="att">Loading…</div>`;
  let data;
  try { data = await api("/api/attention"); } catch (e) { $("#att").textContent = e.message; return; }
  canOpenBrowser = data.can_open_browser;
  const jobs = data.items.map(it => {
    const j = it.job;
    const status = it.ready ? `<span class="ready">Ready to submit</span>` :
      it.needs.length ? `<span class="waiting">${it.needs.length} question${it.needs.length > 1 ? "s need" : " needs"} you</span>` : "";
    return `<div class="job-block" data-id="${esc(j.id)}">
      <div class="top"><div><div class="name">${esc(j.company)}: ${esc(j.title)}</div>
        <div class="sub">Score ${j.fit_score}. Filled ${it.filled} field${it.filled === 1 ? "" : "s"}${it.resume ? ", resume attached" : ""}.
        ${it.captcha ? "Has a bot check on submit." : ""} ${status}</div></div>
        <div>
          <button data-act="handoff" class="primary" ${canOpenBrowser ? "" : "disabled title='Needs the dashboard running on your computer'"}>Open and finish</button>
          ${it.ready ? `<button data-act="submit">Submit now</button>` : `<button data-act="fill" title="Fill again with your newest answers">Fill again</button>`}
          ${it.screenshot ? `<a class="btn" href="${esc(it.screenshot)}" target="_blank">Screenshot</a>` : ""}
          <button data-act="skip" class="quiet">Skip job</button>
        </div></div>
      ${it.needs.length ? `<ul>${it.needs.map(n => `<li>${esc(n.label)} <span class="why">${esc(n.source)}</span></li>`).join("")}</ul>` : ""}
      ${it.ai_written.length ? `<div class="sub" style="margin-top:8px">Claude wrote: ${it.ai_written.map(esc).join("; ")}. Check them before submitting.</div>` : ""}
    </div>`;
  }).join("");
  const companies = Object.keys(data.pending).filter(c => Object.keys(data.pending[c]).length);
  const questions = companies.map(c => `<div class="q-company" data-company="${esc(c)}">
      <div class="top"><strong>${esc(c)}</strong>
        <button data-ai ${data.ai_ready ? "" : `disabled title="${esc(data.ai_why)}"`}>Fill with AI</button></div>
      ${Object.entries(data.pending[c]).map(([q, p]) => questionInput(c, q, p)).join("")}
    </div>`).join("");
  $("#att").innerHTML = `
    ${data.items.length ? jobs : `<div class="panel empty"><strong>Nothing waiting on you.</strong>
      Use Fill form on a job, or turn on Auto-apply in prepare mode, and filled applications will wait here.</div>`}
    <h2>Questions to answer once</h2>
    ${companies.length ? `<p class="lede" style="margin-bottom:10px">Answers are saved for that company unless you tick
      “Use for every company”. Fill with AI suggests answers from your resume; nothing is saved until you press Save.</p>
      ${questions}<div class="bar"><button class="primary" id="saveAns">Save answers</button><span class="note" id="saveNote"></span></div>` :
      `<div class="panel empty"><strong>No open questions.</strong>Every question the bot has met so far has an answer.</div>`}`;

  $$(".job-block button[data-act]").forEach(b => b.onclick = async () => {
    const id = b.closest(".job-block").dataset.id, it = data.items.find(i => i.job.id === id);
    const act = b.dataset.act;
    if (act === "handoff") runApply([id], "handoff", `Open and finish: ${it.job.company}`);
    if (act === "fill") runApply([id], "practice", `Fill again: ${it.job.company}`);
    if (act === "submit") {
      if (!confirm(`Submit the ${it.job.company} application now? It's sent as filled (see the screenshot).`)) return;
      runApply([id], "submit", `Submit: ${it.job.company}`);
    }
    if (act === "skip" && await setStatus(id, "skipped")) b.closest(".job-block").remove();
  });
  $$("button[data-ai]").forEach(b => b.onclick = () => suggest(b.closest(".q-company")));
  const save = $("#saveAns");
  if (save) save.onclick = saveAnswers;
}

function questionInput(company, q, p) {
  const id = "q" + Math.random().toString(36).slice(2, 9);
  const opts = (p.options || []).filter(o => o && !/^(select|choose)/i.test(o));
  const draft = p.draft || "";
  let input;
  if (opts.length && p.multi) {
    input = `<div class="opts" data-input data-multi>${opts.map(o =>
      `<label class="check"><input type="checkbox" value="${esc(o)}"> ${esc(o)}</label>`).join("")}</div>`;
  } else if (opts.length) {
    input = `<select id="${id}" data-input><option value="">Choose…</option>${opts.map(o =>
      `<option ${o === draft ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>`;
  } else {
    input = `<textarea id="${id}" data-input placeholder="Your answer">${esc(draft)}</textarea>`;
  }
  return `<div class="q" data-q="${esc(q)}" data-company="${esc(company)}">
    <label class="ql" for="${id}">${esc(p.label || q)}</label>${input}
    <div class="suggested" ${draft ? "" : "hidden"}>${draft ? "Drafted by Claude from your resume. Edit it before saving." : ""}</div>
    <label class="check every"><input type="checkbox" data-every> Use for every company</label></div>`;
}

function readInput(qEl) {
  const el = $("[data-input]", qEl);
  if (el.hasAttribute("data-multi")) return $$("input:checked", el).map(b => b.value).join(", ");
  return el.value.trim();
}

async function suggest(block) {
  const btn = $("button[data-ai]", block);
  btn.disabled = true; btn.textContent = "Asking Claude…";
  try {
    const res = await api("/api/attention/suggest", { company: block.dataset.company });
    let n = 0;
    for (const [q, s] of Object.entries(res.suggestions || {})) {
      const qEl = $$(".q", block).find(e => e.dataset.q === q);
      if (!qEl || !s || !s.value) continue;
      const el = $("[data-input]", qEl);
      if (el.hasAttribute("data-multi")) {
        const want = s.value.split(",").map(x => x.trim());
        $$("input", el).forEach(b => { b.checked = want.includes(b.value); });
      } else if (!el.value) el.value = s.value;
      const note = $(".suggested", qEl);
      note.hidden = false;
      note.textContent = s.how === "draft" ? "Drafted by Claude from your resume. Edit it before saving." : `Suggested by Claude: ${s.basis}`;
      n++;
    }
    toast(n ? `Claude suggested ${n} answer${n > 1 ? "s" : ""}. Review them, then Save answers.` :
      res.error || "Your resume doesn't settle these. They need your own answers.");
  } catch (e) { toast(e.message, true); }
  btn.disabled = false; btn.textContent = "Fill with AI";
}

async function saveAnswers() {
  const answers = $$(".q").map(qEl => ({
    question: qEl.dataset.q, company: qEl.dataset.company, answer: readInput(qEl),
    everywhere: $("[data-every]", qEl).checked,
  })).filter(a => a.answer);
  if (!answers.length) return toast("Type or pick at least one answer first.", true);
  try {
    await api("/api/answers", { answers });
    toast(`Saved ${answers.length} answer${answers.length > 1 ? "s" : ""}. Use Fill again to put them in the forms.`);
    pageAttention();
  } catch (e) { toast(e.message, true); }
}

/* ── Applied ─────────────────────────────────────────────── */

async function pageApplied() {
  page.innerHTML = `<div class="head"><div><h1>Applied</h1><p class="lede" id="app-sum">Loading…</p></div></div><div id="app"></div>`;
  let data;
  try { data = await api("/api/applications"); } catch (e) { $("#app-sum").textContent = e.message; return; }
  const items = data.items;
  const n = s => items.filter(i => i.status === s).length;
  $("#app-sum").textContent = items.length ?
    `${items.length} applications: ${n("interview")} interviewing, ${n("offer")} offers, ${n("rejected")} rejected.` :
    "Applications you submit, through the bot or yourself, are listed here.";
  if (!items.length) {
    $("#app").innerHTML = `<div class="panel empty"><strong>No applications yet.</strong>Submit one from Needs you, or mark a job Applied on Live jobs.</div>`;
    return;
  }
  $("#app").innerHTML = `<div class="table-wrap"><table><thead><tr><th>Applied</th><th>Job</th><th class="hide-sm">How</th>
    <th>Status</th><th class="hide-sm">Resume sent</th><th class="hide-sm">Proof</th></tr></thead><tbody>
    ${items.map(i => `<tr data-id="${esc(i.id)}">
      <td class="age">${i.applied_at ? esc(new Date(i.applied_at).toLocaleDateString(undefined, { month: "short", day: "numeric" })) : "—"}
        <small>${i.applied_at ? esc(new Date(i.applied_at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })) : ""}</small></td>
      <td class="title-cell"><a href="${esc(i.url)}" target="_blank" rel="noopener">${esc(i.title)}</a>
        <div class="co">${esc(i.company)}</div>
        ${i.ai_answers.length ? `<details><summary class="note">Claude's answers (${i.ai_answers.length})</summary>
          ${i.ai_answers.map(a => `<p class="note"><b>${esc(a.label)}</b><br>${esc(a.value)}</p>`).join("")}</details>` : ""}</td>
      <td class="hide-sm note">${i.mode === "handoff" ? "You finished it" : i.by_bot ? "Bot submitted" : "Marked by you"}</td>
      <td><select data-status aria-label="Status">${["applied", "interview", "offer", "rejected"].map(s =>
        `<option ${s === i.status ? "selected" : ""}>${s}</option>`).join("")}</select></td>
      <td class="hide-sm">${i.resume_file ? `<a href="${esc(i.resume_file)}" target="_blank">Tailored PDF</a>` : '<span class="note">Your saved resume</span>'}</td>
      <td class="hide-sm">${i.screenshot ? `<a href="${esc(i.screenshot)}" target="_blank">Screenshot</a>` : ""}</td>
    </tr>`).join("")}</tbody></table></div>`;
  $$("select[data-status]").forEach(s => s.onchange = () => setStatus(s.closest("tr").dataset.id, s.value));
}

/* ── Auto-apply ──────────────────────────────────────────── */

async function pageAuto() {
  page.innerHTML = `<div class="head"><div><h1>Auto-apply</h1><p class="lede">Let the bot work through fresh jobs on
    its own, or start a batch now.</p></div></div><div id="auto">Loading…</div>`;
  let data;
  try { data = await api("/api/autoapply"); } catch (e) { $("#auto").textContent = e.message; return; }
  const s = data.settings;
  canOpenBrowser = data.can_open_browser;
  const used = Object.values(data.today).reduce((a, b) => a + b, 0);
  $("#auto").innerHTML = `
    <div class="panel">
      <label class="toggle"><span class="switch"><input type="checkbox" id="enabled" ${s.enabled ? "checked" : ""}><span></span></span>
        Apply on its own as new jobs open</label>
      <p class="note ${data.crawler_running ? "" : "warn"}" style="margin:8px 0 14px">${data.crawler_running ?
        `The crawler is running, so this takes effect within a minute. ${used} of ${s.max_per_day} tries used today.` :
        "This runs inside the crawler, which isn't running. Start it with <code>jobpipe watch</code> or <code>jobpipe serve</code>."}</p>
      <div class="grid2">
        <div class="field choice wide"><label>What it does with each job</label>
          <label><input type="radio" name="mode" value="prepare" ${s.mode === "prepare" ? "checked" : ""}>
            <span>Prepare the form for me<small>Fills everything it can and waits under Needs you. Nothing is sent.</small></span></label>
          <label><input type="radio" name="mode" value="submit" ${s.mode === "submit" ? "checked" : ""}>
            <span>Submit complete forms<small>Sends forms with every required question answered. Never ticks consent boxes; stops at CAPTCHAs.</small></span></label>
        </div>
        <div class="field"><label for="min_score">Lowest score</label><input type="number" id="min_score" min="0" max="100" value="${s.min_score}">
          <div class="hint">Only jobs scoring at least this.</div></div>
        <div class="field"><label for="fresh_hours">Opened within (hours)</label><input type="number" id="fresh_hours" min="1" max="720" value="${s.fresh_hours}">
          <div class="hint">Older postings are left alone.</div></div>
        <div class="field"><label for="max_per_day">Tries per day</label><input type="number" id="max_per_day" min="0" max="200" value="${s.max_per_day}"></div>
        <div class="field"><label for="max_per_company">Per company per day</label><input type="number" id="max_per_company" min="1" max="20" value="${s.max_per_company}"></div>
        <div class="field choice row wide"><label style="width:100%;margin:0">Extras</label>
          <label class="check"><input type="checkbox" id="fit_check" ${s.fit_check ? "checked" : ""}> AI fit check first (skips dealbreakers)</label>
          <label class="check"><input type="checkbox" id="tailor" ${s.tailor ? "checked" : ""}> Upload a resume arranged for each job</label>
          <label class="check"><input type="checkbox" id="trust_ai" ${s.trust_ai ? "checked" : ""}> Submit forms with AI-written answers</label>
        </div>
      </div>
      <div class="bar"><button class="primary" id="saveAuto">Save settings</button></div>
    </div>

    <h2>Next in line</h2>
    ${data.candidates.length ? `<div class="table-wrap"><table><tbody>${data.candidates.map(j => `
      <tr><td class="heat"><i style="background:${heat(j.age_min)}"></i></td><td class="age">${esc(ago(j.age_min))}</td>
      <td class="title-cell">${esc(j.title)}<div class="co">${esc(j.company)}</div></td>
      <td><span class="score ${scoreClass(j.fit_score)}">${j.fit_score}</span></td><td>${fitTag(j)}</td></tr>`).join("")}
      </tbody></table></div>` : `<div class="panel empty"><strong>Nothing qualifies right now.</strong>No open job meets the score and freshness above that hasn't been tried.</div>`}

    <h2>Run a batch now</h2>
    <div class="panel"><div class="grid2">
      <div class="field"><label for="r_top">How many jobs</label><input type="number" id="r_top" min="1" max="25" value="5"></div>
      <div class="field"><label for="r_min">Lowest score</label><input type="number" id="r_min" min="0" max="100" value="${s.min_score}"></div>
      <div class="field"><label for="r_mode">Mode</label><select id="r_mode">
        <option value="practice">Fill forms, send nothing</option><option value="submit">Submit complete forms</option></select></div>
      <div class="field choice"><label>Browser</label>
        <label class="check"><input type="checkbox" id="r_headed" ${canOpenBrowser ? "" : "disabled"}> Show the browser while it works</label></div>
    </div><div class="bar"><button class="primary" id="runNow">Start batch</button></div></div>

    <h2>Recent runs</h2>
    <div id="runs">${runsTable(data.runs)}</div>
    <div id="logbox"></div>`;
  $("#saveAuto").onclick = async () => {
    const body = { enabled: $("#enabled").checked, mode: $("input[name=mode]:checked").value };
    ["min_score", "fresh_hours", "max_per_day", "max_per_company"].forEach(k => body[k] = +$("#" + k).value);
    ["fit_check", "tailor", "trust_ai"].forEach(k => body[k] = $("#" + k).checked);
    try { await api("/api/autoapply", body); toast(body.enabled ? "Saved. Auto-apply is on." : "Saved. Auto-apply is off."); pageAuto(); }
    catch (e) { toast(e.message, true); }
  };
  $("#enabled").onchange = () => $("#saveAuto").click();
  $("#runNow").onclick = async () => {
    const mode = $("#r_mode").value;
    if (mode === "submit" && !confirm("Submit every complete form in this batch?")) return;
    try {
      const meta = await api("/api/apply/run", { top: +$("#r_top").value, min_score: +$("#r_min").value, mode,
        headed: $("#r_headed").checked, label: mode === "submit" ? "Batch submit" : "Batch fill" });
      toast("Started."); showLog(meta.id); pageAutoRuns();
    } catch (e) { toast(e.message, true); }
  };
  bindRuns();
  every(5000, pageAutoRuns);
}

function runsTable(runs) {
  if (!runs.length) return `<div class="panel empty"><strong>No runs yet.</strong>Runs you start here or that auto-apply starts appear here with their log.</div>`;
  return `<div class="table-wrap"><table><tbody>${runs.map(r => `<tr data-run="${esc(r.id)}">
    <td class="age">${esc(new Date(r.started_at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }))}
      <small>${esc(new Date(r.started_at).toLocaleDateString(undefined, { month: "short", day: "numeric" }))}</small></td>
    <td>${esc(r.label)}${r.auto ? ' <span class="tag plain">auto</span>' : ""}</td>
    <td><span class="tag ${r.status === "running" ? "caution" : r.status === "finished" ? "good" : "plain"}">${esc(r.status)}</span></td>
    <td class="actions"><button data-log>Log</button>${r.status === "running" ? '<button data-stop class="quiet">Stop</button>' : ""}</td></tr>`).join("")}
    </tbody></table></div>`;
}

async function pageAutoRuns() {
  try { const d = await api("/api/autoapply"); if ($("#runs")) { $("#runs").innerHTML = runsTable(d.runs); bindRuns(); } } catch { /* next tick */ }
}

function bindRuns() {
  $$("#runs button[data-log]").forEach(b => b.onclick = () => showLog(b.closest("tr").dataset.run));
  $$("#runs button[data-stop]").forEach(b => b.onclick = async () => {
    await api(`/api/runs/${b.closest("tr").dataset.run}/stop`, {}).catch(e => toast(e.message, true));
    pageAutoRuns();
  });
}

let logTimer = null;
function showLog(id) {
  clearInterval(logTimer);
  const box = $("#logbox");
  if (!box) return;
  box.innerHTML = `<h2>Log of ${esc(id)}</h2><div class="log" id="log"></div>`;
  let offset = 0;
  const pull = async () => {
    try {
      const d = await api(`/api/runs/${id}/log?offset=${offset}`);
      offset = d.offset;
      const el = $("#log");
      if (!el) return clearInterval(logTimer);
      if (d.text) { el.textContent += d.text; el.scrollTop = el.scrollHeight; }
      if (d.status !== "running") clearInterval(logTimer);
    } catch { clearInterval(logTimer); }
  };
  pull(); logTimer = setInterval(pull, 1500); timers.push(logTimer);
}

/* ── Crawler ─────────────────────────────────────────────── */

async function pageCrawler() {
  page.innerHTML = `<div class="head"><div><h1>Crawler</h1><p class="lede">It reads every company job board it knows,
    more often where postings appear, and checks the job feed every few minutes.</p></div></div><div id="cr">Loading…</div>`;
  const draw = async () => {
    let d;
    try { d = await api("/api/crawler"); } catch (e) { $("#cr").textContent = e.message; return; }
    const st = d.status || {}, f = st.frontier || {}, h = st.last_hour || {}, http = st.http || {};
    const same = h.outcomes ? (h.outcomes.same || 0) : 0;
    const when = s => s ? new Date(s).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "—";
    $("#cr").innerHTML = `
      <p class="${st.running ? "note" : "note warn"}">${st.running ? `Running since ${when(st.started_at)}.` :
        "Not running. Start it with <code>jobpipe watch</code> (or <code>jobpipe serve</code> for crawler and dashboard together)."}</p>
      <div class="facts">
        <div><b>${f.boards || Object.values(d.by_platform).reduce((a, b) => a + b, 0)}</b><span>company boards watched</span></div>
        <div><b>${h.reads || 0}</b><span>board reads in the last hour</span></div>
        <div><b>${h.reads ? Math.round(100 * same / h.reads) : 0}%</b><span>of reads found nothing changed</span></div>
        <div><b>${h.new_postings || 0}</b><span>new matching postings, last hour</span></div>
        <div><b>${f.avg_interval_min || "—"}<small style="font-size:14px"> min</small></b><span>average time between reads</span></div>
        <div><b>${when(st.next_feed_at)}</b><span>next job-feed check</span></div>
      </div>
      <h2>Boards by system</h2>
      <p class="note">${Object.entries(d.by_platform).map(([p, n]) => `${esc(p)} ${n}`).join(", ") + "." || "None yet. Run jobpipe discover or start the crawler."}
        ${st.last_discovery && st.last_discovery.at ? ` Last web discovery at ${when(st.last_discovery.at)} found ${st.last_discovery.new || 0} new boards.` : ""}</p>
      <h2>What just happened</h2>
      ${d.events.length ? `<ul class="events">${d.events.slice(0, 30).map(e => `<li><time>${when(e.at)}</time>
        <span class="kind-${esc(e.kind)}">${{ new: "Opened", closed: "Closed", reopened: "Reopened" }[e.kind] || esc(e.kind)}</span>
        <span>${e.company ? esc(e.company) + ": " : ""}${esc(e.title || "A posting no longer tracked")}</span></li>`).join("")}</ul>` :
        `<div class="panel empty"><strong>No changes recorded yet.</strong>The first read of each board is the starting point; new and closed postings show up from the second read on.</div>`}
      <h2>Busiest boards</h2>
      <div class="table-wrap"><table><thead><tr><th>Company</th><th>System</th><th>New postings a day</th><th>Read every</th><th>Last read</th><th>Last new posting</th></tr></thead><tbody>
      ${d.boards.map(b => `<tr><td>${esc(b.name || b.slug)}</td><td class="note">${esc(b.platform)}</td>
        <td>${b.rate != null ? b.rate.toFixed(2) : "—"}</td><td>${b.interval_s ? Math.round(b.interval_s / 60) + " min" : "—"}</td>
        <td class="note">${when(b.last_fetch_at)}</td><td class="note">${b.last_change_at ? when(b.last_change_at) : "—"}</td></tr>`).join("")}
      </tbody></table></div>
      ${(st.errors || []).length ? `<h2>Recent read errors</h2><ul class="events">${st.errors.map(e =>
        `<li><time>${when(e.at)}</time><span>${esc(e.board)}: ${esc(e.error)}</span></li>`).join("")}</ul>` : ""}
      <p class="note" style="margin-top:14px">HTTP since start: ${http.requests || 0} requests, ${http.not_modified || 0} answered “not modified”,
        ${http.throttled || 0} slow-downs from servers.</p>`;
  };
  await draw();
  every(15000, draw);
}

/* ── Frame ───────────────────────────────────────────────── */

async function refreshCounts() {
  try {
    const s = await api("/api/summary");
    $("#c-new").textContent = s.new_last_hour || "";
    $("#c-att").textContent = s.attention || "";
    $("#c-app").textContent = s.applied || "";
    $("#rail-foot").innerHTML = `<span class="dot ${s.crawler_running ? "live" : ""}"></span>` +
      (s.crawler_running ? `Crawler running. ${s.new_today} new today.` : "Crawler stopped.");
  } catch { /* offline */ }
}

const routes = { jobs: pageJobs, attention: pageAttention, applied: pageApplied, auto: pageAuto, crawler: pageCrawler };

function route() {
  timers.forEach(clearInterval); timers = [];
  const name = (location.hash.replace(/^#\//, "").split("/")[0]) || "jobs";
  const fn = routes[name] || pageJobs;
  $$(".rail a[data-page]").forEach(a => a.classList.toggle("on", a.dataset.page === name));
  seenIds = null;
  fn();
  page.focus({ preventScroll: true });
}

window.addEventListener("hashchange", route);
route();
refreshCounts();
setInterval(refreshCounts, 30000);
