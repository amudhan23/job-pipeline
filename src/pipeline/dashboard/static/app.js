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

const feedState = Object.assign({ hours: "all", minScore: 0, fit: "any", sponsor: "any", q: "", hideDone: true, sort: "age" },
  store("feed") || {});
// "Today" used to be the default; it only lists roles that opened since the
// crawler started, so on a fresh install it looks almost empty
if (!feedState.v) { feedState.hours = "all"; feedState.v = 2; }
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
  crawlLine(data.crawler, data.counts);
  renderRibbon();
  renderRows(fresh);
  if (fresh.size && notifyOn) notifyAbout(data.jobs.filter(j => fresh.has(j.id)));
}

function crawlLine(st, counts) {
  const el = $("#crawl-line");
  if (!el) return;
  const c = counts || {};
  const tally = c.open !== undefined ?
    `${c.open.toLocaleString()} open roles match your filters, ${c.today.toLocaleString()} opened in the last day. ` : "";
  if (st && st.running) {
    const f = st.frontier || {}, h = st.last_hour || {};
    const unread = (f.boards || 0) - (f.bootstrapped || 0);
    const learning = unread > 0 ? ` ${unread.toLocaleString()} boards not read yet.` : "";
    el.innerHTML = esc(tally) + esc(`Watching ${(f.boards || 0).toLocaleString()} company job boards. ${h.reads || 0} reads and ` +
      `${h.new_postings || 0} new postings in the last hour.${learning}`) +
      (feedState.hours !== "all" && c.open > (c.shown || 0) ? ` <a href="#" id="show-all">Show all ${c.open.toLocaleString()} open</a>` : "");
    const a = $("#show-all");
    if (a) a.onclick = e => { e.preventDefault(); feedState.hours = "all"; saveFeed(); pageJobs(); };
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
        <button data-act="resume" class="quiet" title="The resume made for this job">Resume</button>
        <button data-act="applied" class="quiet" title="I applied to this">Applied</button>
        <button data-act="skip" class="quiet" title="Don't show this again">Skip</button></td>
    </tr>`).join("");
  $$("button[data-act]", rows).forEach(b => b.onclick = async () => {
    const id = b.closest("tr").dataset.id, j = feedJobs.find(x => x.id === id);
    const act = b.dataset.act;
    if (act === "fill") runApply([id], "practice", `Fill: ${j.company}`);
    if (act === "handoff") runApply([id], "handoff", `Open and finish: ${j.company}`);
    if (act === "resume") showResume(id, `${j.company}: ${j.title}`);
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
          ${canOpenBrowser ? `<button data-act="handoff" class="primary">Open and finish</button>` :
            `<button data-act="finish" class="primary" title="Every answer with Copy buttons, and a link to the form">Finish it yourself</button>`}
          ${it.ready ? `<button data-act="submit">Submit now</button>` : `<button data-act="fill" title="Fill again with your newest answers">Fill again</button>`}
          ${it.screenshot ? `<a class="btn" href="${esc(it.screenshot)}" target="_blank">Screenshot</a>` : ""}
          <button data-act="resume" class="quiet">Resume</button>
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
  const or = data.outreach || { waiting: [], due: [] };
  const reach = (or.waiting.length || or.due.length) ? `<h2>Reach out</h2>
    <p class="lede" style="margin-bottom:10px">A cold application gets an interview about 3% of the time; a note to the hiring
      manager or a referral does far better. Do this for every application that matters.</p>
    ${or.due.map(f => `<div class="job-block" data-or="${esc(f.job_id)}"><div class="top"><div>
      <div class="name">${esc(f.company)}: ${esc(f.title)}</div>
      <div class="sub">${esc(f.label)} to ${esc(f.contact)} was due ${esc((f.due || "").replace("T", " "))}.</div></div>
      <div><button data-or-open class="primary">Open outreach</button></div></div></div>`).join("")}
    ${or.waiting.map(j => `<div class="job-block" data-or="${esc(j.id)}"><div class="top"><div>
      <div class="name">${esc(j.company)}: ${esc(j.title)}</div>
      <div class="sub">Applied ${esc((j.applied_at || "").slice(0, 10))}. ${j.contacts ? `${j.contacts} contact${j.contacts > 1 ? "s" : ""} found, nothing sent yet.` :
        j.planned ? "Whom to look for is planned; add the person you find." : "Nobody contacted yet."}</div></div>
      <div>${j.planned || j.contacts ? `<button data-or-open class="primary">Open outreach</button>` :
        `<button data-or-plan class="primary">Plan outreach</button>`}</div></div></div>`).join("")}` : "";
  const td = data.today || {};
  const meter = (n, target) => `<span class="meter"><i style="width:${Math.min(100, Math.round(100 * (n || 0) / target))}%"></i></span>`;
  const today = td.applied_target ? `<div class="panel today"><h2 style="margin-top:0">Today</h2>
    <div class="today-grid">
      <div><b>${td.applied}</b> of ${td.applied_target} applications ${meter(td.applied, td.applied_target)}
        <div class="sub">${td.to_approve ? `${td.to_approve} filled and waiting for your OK below.` : "Auto-apply fills the rest as postings open."}</div></div>
      <div><b>${td.reach_outs}</b> of ${td.reach_target} reach-outs ${meter(td.reach_outs, td.reach_target)}
        <div class="sub">${td.to_contact ? `${td.to_contact} application${td.to_contact > 1 ? "s" : ""} with nobody contacted yet. ` : ""}${esc(td.window?.label || "")}</div></div>
      <div><b>${td.followups_due}</b> follow-up${td.followups_due === 1 ? "" : "s"} due<div class="sub">${td.followups_due ? "Listed under Reach out." : "None overdue."}</div></div>
      <div><b>${(td.interviews || []).length}</b> interview${(td.interviews || []).length === 1 ? "" : "s"} in play
        <div class="sub">${(td.interviews || []).map(i => `${esc(i.company)}: ${i.has_pack ? "prep pack ready" : "no prep pack yet"}`).join(" · ") || "Mark a job Interview on Applied and the prep pack is written."}</div></div>
    </div></div>` : "";
  $("#att").innerHTML = `
    ${today}
    ${data.items.length ? jobs : `<div class="panel empty"><strong>Nothing waiting on you.</strong>
      Use Fill form on a job, or turn on Auto-apply in prepare mode, and filled applications will wait here.</div>`}
    ${reach}
    <h2>Questions to answer once</h2>
    ${companies.length ? `<p class="lede" style="margin-bottom:10px">Answers are saved for that company unless you tick
      “Use for every company”. Fill with AI suggests answers from your resume; nothing is saved until you press Save.</p>
      ${questions}<div class="bar"><button class="primary" id="saveAns">Save answers</button><span class="note" id="saveNote"></span></div>` :
      `<div class="panel empty"><strong>No open questions.</strong>Every question the bot has met so far has an answer.</div>`}`;

  $$("[data-or-plan]").forEach(b => b.onclick = () => startOutreach(b.closest("[data-or]").dataset.or, b));
  $$("[data-or-open]").forEach(b => b.onclick = () => { location.hash = "#/outreach"; });
  $$(".job-block button[data-act]").forEach(b => b.onclick = async () => {
    const id = b.closest(".job-block").dataset.id, it = data.items.find(i => i.job.id === id);
    const act = b.dataset.act;
    if (act === "handoff") runApply([id], "handoff", `Open and finish: ${it.job.company}`);
    if (act === "finish") showSent(id, true);
    if (act === "fill") runApply([id], "practice", `Fill again: ${it.job.company}`);
    if (act === "submit") {
      if (!confirm(`Submit the ${it.job.company} application now? It's sent as filled (see the screenshot).`)) return;
      runApply([id], "submit", `Submit: ${it.job.company}`);
    }
    if (act === "resume") showResume(id, `${it.job.company}: ${it.job.title}`);
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

/* ── Scorecard: which channel converts for you ───────────── */

async function scorecard() {
  const sc = await api("/api/scorecard");
  const box = document.createElement("div");
  box.className = "panel scorecard";
  const row = (label, w, m) => `<tr><td>${label}</td><td>${w}</td><td>${m}</td></tr>`;
  const rate = (a, b) => a ? `${Math.round(100 * b / a)}%` : "—";
  const w = sc.week, m = sc.month;
  const src = Object.entries(m.by_source || {}).sort((a, b) => b[1].applied - a[1].applied).slice(0, 6);
  box.innerHTML = `<h2 style="margin-top:0">Scorecard</h2>
    <p class="note">Interviews come from channels, not totals: a cold application converts a few percent of the time, a referral or a note to the hiring manager far more. Aim for 10 applications and 3 reach-outs a day.</p>
    <div class="table-wrap"><table><thead><tr><th></th><th>7 days</th><th>30 days</th></tr></thead><tbody>
      ${row("Applications", w.applied, m.applied)}
      ${row("Reach-outs sent", w.outreach.sent, m.outreach.sent)}
      ${row("Replies to reach-outs", `${w.outreach.replied} (${rate(w.outreach.sent, w.outreach.replied)})`, `${m.outreach.replied} (${rate(m.outreach.sent, m.outreach.replied)})`)}
      ${row("Interviews", `${w.interview} (${rate(w.applied, w.interview)} of applications)`, `${m.interview} (${rate(m.applied, m.interview)})`)}
      ${row("Offers", w.offer, m.offer)}
      ${row("Rejections", w.rejected, m.rejected)}
    </tbody></table></div>
    ${src.length ? `<p class="note" style="margin-bottom:4px">Last 30 days by where the job was found:</p>
    <div class="table-wrap"><table><thead><tr><th>Source</th><th>Applied</th><th>Interviews</th><th>Rate</th></tr></thead><tbody>
      ${src.map(([k, v]) => `<tr><td>${esc(k)}</td><td>${v.applied}</td><td>${v.interview}</td><td>${rate(v.applied, v.interview)}</td></tr>`).join("")}
    </tbody></table></div>` : ""}`;
  $("#app").before(box);
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
  scorecard().catch(() => {});
  if (!items.length) {
    $("#app").innerHTML = `<div class="panel empty"><strong>No applications yet.</strong>Submit one from Needs you, or mark a job Applied on Live jobs.</div>`;
    return;
  }
  $("#app").innerHTML = `<div class="table-wrap"><table><thead><tr><th>Applied</th><th>Job</th><th class="hide-sm">How</th>
    <th>Status</th><th class="hide-sm">Resume sent</th><th class="hide-sm">Proof</th><th></th></tr></thead><tbody>
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
      <td class="actions"><button class="quiet" data-sent>What was sent</button> <button data-reach>Reach out</button>
        ${i.status === "interview" || i.status === "offer" ? `<button class="primary" data-prep>Prep</button>` : ""}</td>
    </tr>`).join("")}</tbody></table></div>`;
  $$("select[data-status]").forEach(s => s.onchange = async () => {
    const ok = await setStatus(s.closest("tr").dataset.id, s.value);
    if (ok && s.value === "interview") { toast("Interview! Writing your prep pack (a few minutes)."); pageApplied(); }
  });
  $$("button[data-prep]").forEach(b => b.onclick = () => showPrep(b.closest("tr").dataset.id));
  $$("button[data-reach]").forEach(b => b.onclick = () => startOutreach(b.closest("tr").dataset.id, b));
  $$("button[data-sent]").forEach(b => b.onclick = () => showSent(b.closest("tr").dataset.id, false));
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
          <label class="check"><input type="checkbox" id="tailor" ${s.tailor ? "checked" : ""}> Upload a resume made for each job</label>
          <label class="check"><input type="checkbox" id="craft" ${s.craft ? "checked" : ""}> Reword resume bullets toward the job (facts kept)</label>
          <label class="check"><input type="checkbox" id="trust_ai" ${s.trust_ai ? "checked" : ""}> Submit forms with AI-written answers</label>
        </div>
        <div class="field"><label for="review_first">Ask me on Telegram before the first</label>
          <input type="number" id="review_first" min="0" max="1000" value="${s.review_first}">
          <div class="hint">In submit mode, complete forms come to your phone with every answer for a Submit or Skip tap.
          ${data.needs_review ? `<b>${data.approvals} of ${s.review_first} approved</b>; then it submits on its own.` :
            s.mode === "submit" && s.review_first ? `Done: ${data.approvals} approved, it now submits on its own.` : ""}
          0 never asks; 1000 always asks.</div></div>
        <div class="field"><label for="repost_days">Don't re-apply to the same role within (days)</label>
          <input type="number" id="repost_days" min="0" max="365" value="${s.repost_days}">
          <div class="hint">Catches reposts and the same opening listed twice.</div></div>
        <div class="field wide"><label for="never_companies">Never apply to</label>
          <textarea id="never_companies" rows="2" placeholder="One company per line, e.g. a past employer">${esc((s.never_companies || []).join("\n"))}</textarea></div>
      </div>
      <div class="bar"><button class="primary" id="saveAuto">Save settings</button></div>
    </div>

    <h2>Alerts on your phone</h2>
    <div class="panel" id="tg">Loading…</div>

    <h2>Security codes from Gmail</h2>
    <div class="panel" id="mail">Loading…</div>

    <h2>Setup check</h2>
    <div class="panel" id="doctor"><button id="runDoctor">Check everything</button>
      <span class="note">Checks the browser, resume builder, your details, AI key, Telegram, email codes and job boards.</span></div>

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
    ["min_score", "fresh_hours", "max_per_day", "max_per_company", "review_first", "repost_days"].forEach(k => body[k] = +$("#" + k).value);
    ["fit_check", "tailor", "craft", "trust_ai"].forEach(k => body[k] = $("#" + k).checked);
    body.never_companies = $("#never_companies").value;
    try { await api("/api/autoapply", body); toast(body.enabled ? "Saved. Auto-apply is on." : "Saved. Auto-apply is off."); pageAuto(); }
    catch (e) { toast(e.message, true); }
  };
  $("#enabled").onchange = () => $("#saveAuto").click();
  mailCard();
  $("#runDoctor").onclick = runDoctor;
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
  telegramCard();
}

async function telegramCard() {
  const box = $("#tg");
  let t;
  try { t = await api("/api/telegram"); } catch (e) { box.textContent = e.message; return; }
  if (!t.connected) {
    box.innerHTML = `<p class="note" style="margin-top:0">Get a Telegram message the minute a matching job opens,
      with buttons to submit, prepare or skip it, and answer form questions by replying.</p>
      <ol class="note" style="padding-left:18px"><li>In Telegram, open <b>@BotFather</b>, send <code>/newbot</code>, and follow the steps.</li>
      <li>Paste the token it gives you here.</li><li>Open your new bot and press Start.</li></ol>
      <div class="bar"><input type="password" id="tg_token" placeholder="Bot token, like 123456:ABC-…" style="width:340px">
      <button class="primary" id="tg_connect">Connect</button><span class="note" id="tg_note"></span></div>`;
    $("#tg_connect").onclick = async () => {
      const note = $("#tg_note");
      try {
        const r = await api("/api/telegram/connect", { token: $("#tg_token").value.trim() });
        note.innerHTML = `Now open <a href="https://t.me/${esc(r.bot)}" target="_blank" rel="noopener">t.me/${esc(r.bot)}</a> and press Start. Waiting…`;
        for (let i = 0; i < 8; i++) {
          const w = await api("/api/telegram/wait", {});
          if (w.connected) { toast("Telegram connected."); return telegramCard(); }
        }
        note.textContent = "No message arrived. Press Start in the bot, then Connect again.";
      } catch (e) { note.textContent = e.message; }
    };
    return;
  }
  box.innerHTML = `<p class="note" style="margin-top:0">Connected to <b>@${esc(t.bot)}</b>. Alerts arrive while the crawler runs.</p>
    <div class="grid2">
      <div class="field"><label for="tg_min">Alert for jobs scoring at least</label><input type="number" id="tg_min" min="0" max="100" value="${t.min_score}"></div>
      <div class="field"><label for="tg_hour">Morning summary at (hour, 0–23)</label><input type="number" id="tg_hour" min="0" max="23" value="${t.summary_hour}"></div>
      <div class="field choice row wide"><label style="width:100%;margin:0">Send me</label>
        <label class="check"><input type="checkbox" id="tg_new" ${t.new_jobs ? "checked" : ""}> New matching jobs</label>
        <label class="check"><input type="checkbox" id="tg_applied" ${t.applied ? "checked" : ""}> Each application sent</label>
        <label class="check"><input type="checkbox" id="tg_needs" ${t.needs ? "checked" : ""}> Questions a form needs answered</label>
        <label class="check"><input type="checkbox" id="tg_on" ${t.enabled ? "checked" : ""}> Alerts on</label></div>
    </div>
    <div class="bar"><button class="primary" id="tg_save">Save alerts</button><button id="tg_test">Send a test</button></div>`;
  $("#tg_save").onclick = async () => {
    try {
      await api("/api/telegram", { min_score: +$("#tg_min").value, summary_hour: +$("#tg_hour").value,
        new_jobs: $("#tg_new").checked, applied: $("#tg_applied").checked, needs: $("#tg_needs").checked, enabled: $("#tg_on").checked });
      toast("Alerts saved.");
    } catch (e) { toast(e.message, true); }
  };
  $("#tg_test").onclick = async () => {
    try { const r = await api("/api/telegram/test", {}); toast(r.sent ? "Sent. Check Telegram." : "Telegram didn't take it.", !r.sent); }
    catch (e) { toast(e.message, true); }
  };
}

/* ── Resume view ─────────────────────────────────────────── */

async function showResume(jobId, title) {
  const dlg = $("#modal"), body = $("#modal-body");
  const render = r => {
    const changes = (r.changes || []).filter(c => !c.error);
    const err = (r.changes || []).find(c => c.error);
    body.innerHTML = `<h2>Resume for ${esc(title)}</h2>
      ${r.exists ? `<p class="note">${esc(r.note || "")}</p>
        <div class="bar" style="margin-top:6px"><a class="btn" href="${esc(r.file)}" target="_blank">Open the PDF</a>
        <button data-rb="1">Rebuild</button><button data-rb="0" class="quiet">Rebuild without rewording</button>
        <button class="quiet" data-close style="margin-left:auto">Close</button></div>
        ${r.had_description === false ? `<p class="note warn">This job had no description to read, so the resume is
          aimed at the title only. Paste the description below and rebuild.</p>` : ""}
        ${r.had_description === false ? `<textarea id="rb-jd" class="profile-text" style="min-height:120px"
          placeholder="Paste the job description here"></textarea>` : ""}
        ${r.plan ? `<h2>What the job needs</h2><p class="note">${esc(r.plan.job_summary || "")}</p>
          ${(r.plan.requirements || []).map(q => `<div class="change"><div class="before"><b>${q.must ? "Must" : "Nice"}:</b>
            ${esc(q.need)}</div><div class="${q.strength === "none" ? "kept-no" : "after"}">${esc(q.strength)}: ${esc(q.evidence)}</div></div>`).join("")}
          ${(r.plan.leave_out || []).length ? `<p class="note">Left out for this job: ${r.plan.leave_out.map(esc).join("; ")}</p>` : ""}` : ""}
        ${(r.steps || []).length ? `<p class="note">Steps: ${r.steps.map(esc).join(" → ")}</p>` : ""}
        ${r.by === "profile" ? `<p class="note">Written from your career profile and checked line by line against it.</p>
          ${(r.dropped || []).length ? `<h2>Left out by the fact check</h2>${r.dropped.map(d => `<div class="change">
            <div class="before">${esc(d.where)}: ${esc(d.line)}</div><div class="kept-no">${esc(d.why)}</div></div>`).join("")}` :
            `<p class="note">Every line passed the check.</p>`}
          ${(r.trimmed || []).length ? `<p class="note">Trimmed to fit one page: ${r.trimmed.length} line${r.trimmed.length > 1 ? "s" : ""}.</p>` : ""}
          ${(r.parse_problems || []).length ? `<p class="note warn">A screening system may misread this PDF: ${r.parse_problems.map(esc).join("; ")}</p>` :
            r.steps && r.steps.includes("parses as text") ? `<p class="note">Reads back cleanly as text, the way screening systems parse it.</p>` : ""}` : ""}
        ${r.projects ? `<p class="note">Projects: ${r.projects.map(esc).join(", ")}. Languages lead with ${(r.languages || []).slice(0, 3).map(esc).join(", ")}.</p>` : ""}
        ${err ? `<p class="note warn">Rewording was skipped: ${esc(err.error)}</p>` : ""}
        ${changes.length ? `<h2>What changed</h2>${changes.map(c => `<div class="change">
          <div class="before">${esc(c.before)}</div>
          ${c.kept ? `<div class="after">${esc(c.after)}</div>` : `<div class="kept-no">Kept the original: the rewrite ${esc(c.why)}.</div>`}</div>`).join("")}` :
          r.by === "profile" ? "" : `<p class="note">No bullet was reworded; they're chosen and ordered for the job.</p>`}` :
      `${r.last_error ? `<p class="note warn" id="rb-err">The last build failed: ${esc(r.last_error)}</p>` :
          `<p class="note" id="rb-err">No resume has been made for this job yet. Building one takes 2-4 minutes: it reads the job, writes, then reviews as the hiring manager.</p>`}
        <details class="note"><summary>Paste the job description (if the posting's text didn't come through)</summary>
          <textarea id="rb-jd" class="profile-text" style="min-height:120px" placeholder="Paste the job description here"></textarea></details>
        <div class="bar"><button class="primary" data-rb="1">${r.last_error ? "Try again" : "Build resume"}</button>
        <button class="quiet" data-close>Close</button></div>`}`;
    $$("[data-rb]", body).forEach(b => b.onclick = async () => {
      b.disabled = true;
      const t0 = Date.now();
      const tick = () => { b.textContent = `Building… ${Math.round((Date.now() - t0) / 1000)}s`; };
      tick();
      const timer = setInterval(tick, 1000);
      try {
        const res = await Promise.race([
          api(`/api/resume/${encodeURIComponent(jobId)}/build`,
              { reword: b.dataset.rb === "1", description: ($("#rb-jd", body) || {}).value || "" }),
          new Promise((_, no) => setTimeout(() => no(new Error(
            "The build is taking over 9 minutes. Check the terminal running jobpipe serve for an error, " +
            "and run jobpipe doctor to confirm LaTeX and the AI key work.")), 540000))]);
        render(res);
      }
      catch (e) {
        toast(e.message, true); b.disabled = false; b.textContent = "Try again";
        const note = $("#rb-err", body);          // keep the reason on screen, not just in a toast
        if (note) { note.className = "note warn"; note.textContent = e.message; }
      }
      finally { clearInterval(timer); }
    });
    $$("[data-close]", body).forEach(b => b.onclick = () => dlg.close());
  };
  body.innerHTML = "Loading…";
  dlg.showModal();
  try { render(await api(`/api/resume/${encodeURIComponent(jobId)}`)); } catch (e) { body.textContent = e.message; }
}

/* ── Search companies ────────────────────────────────────── */

async function pageSearch() {
  page.innerHTML = `<div class="head"><div><h1>Search companies</h1>
    <p class="lede">Name the companies and roles you want. It finds each company's job portal, pulls the matching roles,
    and keeps watching those companies so a new opening shows up within 15 minutes.</p></div></div>
    <div class="panel search-form"><div class="grid2">
      <div class="field wide"><label for="s_co">Companies</label>
        <textarea id="s_co" placeholder="Stripe, Jane Street, Datadog, Ramp"></textarea>
        <div class="hint">Separate with commas or new lines.</div></div>
      <div class="field wide"><label for="s_roles">Roles</label>
        <input type="text" id="s_roles" style="width:100%" placeholder="software engineer, backend engineer, platform engineer">
        <div class="hint">Leave empty to use the roles in your profile.</div></div>
      <div class="field choice row wide">
        <label class="check"><input type="checkbox" id="s_any"> Any location</label>
        <label class="check"><input type="checkbox" id="s_watch" checked> Keep watching these companies</label></div>
    </div><div class="bar"><button class="primary" id="s_go">Search</button></div></div>
    <div id="searches">Loading…</div>`;
  $("#s_go").onclick = async () => {
    try {
      await api("/api/searches", { companies: $("#s_co").value, roles: $("#s_roles").value,
        anywhere: $("#s_any").checked, watch: $("#s_watch").checked });
      toast("Searching. Results fill in below as each company is found.");
      $("#s_co").value = ""; drawSearches();
    } catch (e) { toast(e.message, true); }
  };
  await drawSearches();
  every(4000, drawSearches);
}

async function drawSearches() {
  const box = $("#searches");
  if (!box) return;
  let d;
  try { d = await api("/api/searches"); } catch (e) { box.textContent = e.message; return; }
  if (!d.searches.length) { box.innerHTML = `<div class="panel empty"><strong>No searches yet.</strong>Add a few companies above.</div>`; return; }
  box.innerHTML = d.searches.map(s => `<div class="search" data-id="${esc(s.id)}">
    <h2>${s.companies.map(esc).join(", ")}</h2>
    <p class="note">Roles: ${s.roles.length ? s.roles.map(esc).join(", ") : "your profile's roles"}. ${s.anywhere ? "Any location." : "Your locations."}
      ${s.watch ? "Watched by the crawler." : ""} ${s.running ? `<b>${esc(s.running)}</b>` : s.last_run ? `Last searched ${esc(new Date(s.last_run).toLocaleString())}.` : ""}</p>
    <div class="bar" style="margin-top:4px"><button data-s="run" ${s.running ? "disabled" : ""}>Search again</button><button data-s="delete" class="quiet">Delete</button></div>
    ${s.results.map(r => `<div class="result-co"><h3>${esc(r.company)}</h3>
      <span class="note">${r.boards.length ? r.boards.map(b => esc(b[0])).join(", ") : esc(r.error)}</span></div>
      ${r.jobs.length ? `<div class="table-wrap"><table><tbody>${r.jobs.map(j => `<tr data-id="${esc(j.id)}">
        <td class="title-cell"><a href="${esc(j.url)}" target="_blank" rel="noopener">${esc(j.title)}</a> ${statusTag(j.status)}</td>
        <td class="loc">${esc(j.location)}</td><td><span class="score ${scoreClass(j.fit_score)}">${j.fit_score}</span></td>
        <td class="actions"><button data-act="fill">Fill form</button><button data-act="handoff" ${canOpenBrowser ? "" : "disabled"}>Open and finish</button>
          <button data-act="resume" class="quiet">Resume</button></td></tr>`).join("")}</tbody></table></div>` :
        r.boards.length ? `<p class="note">No open roles match right now${s.watch ? "; you'll see new ones here and on Live jobs" : ""}.</p>` : ""}`).join("")}
  </div>`).join("");
  $$(".search").forEach(el => {
    const id = el.dataset.id;
    $$("button[data-s]", el).forEach(b => b.onclick = async () => {
      if (b.dataset.s === "delete" && !confirm("Delete this search? Its companies stay on the crawler's list.")) return;
      try { await api(`/api/searches/${id}/${b.dataset.s}`, {}); drawSearches(); } catch (e) { toast(e.message, true); }
    });
    $$("button[data-act]", el).forEach(b => b.onclick = () => {
      const row = b.closest("tr"), jid = row.dataset.id, title = $("a", row).textContent;
      if (b.dataset.act === "fill") runApply([jid], "practice", `Fill: ${title}`);
      if (b.dataset.act === "handoff") runApply([jid], "handoff", `Open and finish: ${title}`);
      if (b.dataset.act === "resume") showResume(jid, title);
    });
  });
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
    more often where postings appear, and checks the job feed every few minutes.</p></div></div><div id="cr">Loading…</div>
    <h2>Jobs beyond your boards (Hirebase)</h2><div class="panel" id="hb">Loading…</div>`;
  hirebaseCard();
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

async function hirebaseCard() {
  const box = $("#hb"); if (!box) return;
  let h;
  try { h = await api("/api/hirebase"); } catch (e) { box.textContent = e.message; return; }
  const last = h.last || {};
  box.innerHTML = `<p class="note" style="margin-top:0">Hirebase indexes 500,000+ company career pages, including Workday and iCIMS.
    Every few hours jobpipe asks it for postings it saw since the last ask that match your titles and locations. Ones on boards
    jobpipe can crawl get added to the crawler (and applied to like any other); the rest appear in Live jobs with their link.</p>
    ${h.has_key ? `<p><span class="ready">Connected</span> ${h.used} of ${h.monthly_jobs} jobs used this month
      ${last.asked_at ? ` · last ask ${new Date(last.asked_at).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}: ${last.returned} returned, ${last.matching} matched` : ""}
      ${last.error ? `<br><span class="note warn">${esc(last.error)}</span>` : ""}</p>` : ""}
    <div class="grid2">
      <div class="field"><label for="hb-key">API key</label><input type="password" id="hb-key" autocomplete="off"
        placeholder="${h.has_key ? "Saved. Paste a new one to replace it" : "hirebase.org → Profile → API Key (free)"}"></div>
      <div class="field"><label for="hb-month">Jobs per month at most</label><input type="number" id="hb-month" min="0" value="${h.monthly_jobs}">
        <div class="hint">The free plan allows 500. Asks are sized so this lasts the whole month.</div></div>
      <div class="field"><label for="hb-every">Ask every (hours)</label><input type="number" id="hb-every" min="1" max="48" value="${h.every_hours}"></div>
      <label class="check"><input type="checkbox" id="hb-visa" ${h.visa_only ? "checked" : ""}> Only postings Hirebase marks as sponsoring visas</label>
      <label class="check"><input type="checkbox" id="hb-on" ${h.enabled ? "checked" : ""}> On</label>
    </div>
    <div class="bar"><button class="primary" id="hb-save">Save</button>${h.has_key ? `<button class="quiet" id="hb-forget">Remove key</button>` : ""}</div>`;
  $("#hb-save").onclick = async () => {
    const body = { monthly_jobs: +$("#hb-month").value, every_hours: +$("#hb-every").value, visa_only: $("#hb-visa").checked, enabled: $("#hb-on").checked };
    if ($("#hb-key").value) body.key = $("#hb-key").value;
    try { await api("/api/hirebase", body); toast("Saved."); hirebaseCard(); } catch (e) { toast(e.message, true); }
  };
  if ($("#hb-forget")) $("#hb-forget").onclick = async () => { await api("/api/hirebase/forget", {}); toast("Removed."); hirebaseCard(); };
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


/* ── Career profile ──────────────────────────────────────── */

const PROFILE_TEMPLATE = `# Career profile: Your Name

## Rules
- Total experience is N years.
- No em dashes. No summary section. One page.
- Never claim: (things you must never be credited with, comma separated)

## Experience

### Company: Title
City | Mon YYYY – Mon YYYY
Stack: the tools you really used there
- What you built, for whom, and the result (with the real number).

## Projects
- **Project name** | stack | link
  What it does and the result.

## Skills by level
- **Used in production:**
- **Used in projects:**
- **Coursework only:**

## Education
- **University**: Degree, years. Coursework: ...

## Stories (for "Why us?" and "Tell us about…" questions)
- **Short title:** what happened, what you did, what changed.
`;

async function pageCareer() {
  page.innerHTML = `<div class="head"><div><h1>Career profile</h1>
    <p class="lede">Everything about your work in one place: every role, project, real number, skill level and rule.
    Resumes, form answers, fit checks and outreach are written from this, and every resume line is checked against it
    before it's printed. Facts about one job can only appear under that job.</p></div></div>
    <div class="panel"><div class="profile-stats" id="cp-stats">Loading…</div>
      <textarea id="cp-text" class="profile-text" spellcheck="true" aria-label="Career profile"></textarea>
      <div class="bar"><button class="primary" id="cp-save">Save profile</button>
        <label class="btn quiet file-btn">Load a .md or .txt file<input type="file" id="cp-file" accept=".md,.txt,text/plain,text/markdown" hidden></label>
        <button class="quiet" id="cp-tpl">Start from the outline</button>
        <span class="note" id="cp-dirty"></span></div>
      <p class="note">Write <b>[confirm …]</b> next to anything you haven't checked; text inside it never counts as proof for a resume line.
      Stays in your private folder, never in the repo.</p></div>
    <div class="panel"><h2>How resumes are written</h2>
      <div class="field choice row"><span>Model</span>
        <label class="check"><input type="radio" name="cp-model" value="opus"> Opus (best, about $0.25 a resume)</label>
        <label class="check"><input type="radio" name="cp-model" value="sonnet"> Sonnet (about $0.10)</label></div>
      <p class="note" id="cp-ref-note"></p>
      <div class="bar"><label class="btn file-btn">Upload a reference resume (PDF)<input type="file" id="cp-ref" accept="application/pdf" hidden></label></div>
      <p class="note">A resume whose look and writing you like. New resumes copy its style: bullets per role, length, tone,
      what it bolds. Never its facts; those only come from your profile.</p></div>`;
  const ta = $("#cp-text");
  const stats = d => {
    $("#cp-stats").innerHTML = d.ready ? `<span><b>${d.roles.length}</b> sections found</span>
      <span><b>${d.never_claim.length}</b> never-claim terms${d.never_claim.length ? `: ${d.never_claim.slice(0, 8).map(esc).join(", ")}` : ""}</span>
      <span class="${d.confirm ? "waiting" : "ready"}">${d.confirm ? `${d.confirm} [confirm] notes left to check` : "Nothing left to confirm"}</span>` :
      `<span class="waiting">No profile yet.</span><span>Until you add one, resumes reuse the bullets in your config.</span>`;
  };
  let d;
  try { d = await api("/api/career-profile"); } catch (e) { $("#cp-stats").textContent = e.message; return; }
  ta.value = d.text; stats(d);
  $("#cp-ref-note").textContent = d.has_reference ? "Reference resume saved." :
    "No reference resume yet: your uploaded resume on My details sets the style.";
  $$("input[name=cp-model]").forEach(r => {
    r.checked = r.value === d.resume_model;
    r.onchange = async () => {
      try { await api("/api/ai", { resume_model: r.value }); toast(`Resumes will be written with ${r.value === "opus" ? "Opus" : "Sonnet"}.`); }
      catch (e) { toast(e.message, true); }
    };
  });
  $("#cp-ref").onchange = async e => {
    const f = e.target.files[0]; if (!f) return;
    try {
      const r = await fetch("/api/career-profile/reference", { method: "POST", headers: { "Content-Type": "application/pdf" }, body: f });
      const j = await r.json(); if (!r.ok) throw new Error(j.error || "Upload failed");
      $("#cp-ref-note").textContent = "Reference resume saved."; toast(`Saved ${f.name} as your reference resume.`);
    } catch (err) { toast(err.message, true); }
  };
  let saved = ta.value;
  ta.oninput = () => { $("#cp-dirty").textContent = ta.value !== saved ? "Unsaved changes" : ""; };
  $("#cp-save").onclick = async () => {
    try { d = await api("/api/career-profile", { text: ta.value }); saved = ta.value; stats(d); ta.oninput(); toast("Profile saved."); }
    catch (e) { toast(e.message, true); }
  };
  $("#cp-tpl").onclick = () => {
    if (ta.value.trim() && !confirm("Replace what's in the box with the outline?")) return;
    ta.value = PROFILE_TEMPLATE; ta.oninput(); ta.focus();
  };
  $("#cp-file").onchange = async e => {
    const f = e.target.files[0]; if (!f) return;
    if (f.size > 400000) { toast("That file is too big for a profile.", true); return; }
    ta.value = await f.text(); ta.oninput(); toast(`Loaded ${f.name}. Press Save profile to keep it.`);
  };
}

/* ── Outreach ────────────────────────────────────────────── */

async function startOutreach(jobId, btn) {
  if (btn) { btn.disabled = true; btn.textContent = "Planning…"; }
  try { await api(`/api/outreach/${encodeURIComponent(jobId)}/plan`, {}); location.hash = "#/outreach"; }
  catch (e) { toast(e.message, true); if (btn) { btn.disabled = false; btn.textContent = "Reach out"; } }
}

const emailTag = c => c.email ? `<span class="tag ${{ verified: "good", yours: "good", found: "caution", guessed: "caution" }[c.email_status] || "plain"}">${
  { verified: "verified", yours: "yours", found: "found, unverified", guessed: "guess" }[c.email_status] || ""}</span>` : "";

function copyBtn(text, label = "Copy") {
  return `<button class="quiet" data-copy="${esc(text)}">${label}</button>`;
}

function contactCard(r, c) {
  const d = c.drafts, nx = c.next || {};
  const due = nx.due ? new Date(nx.due) : null;
  const step = nx.step === "replied" ? `<span class="tag good">Replied</span>` :
    nx.step === "done" ? `<span class="tag plain">Sequence finished</span>` :
    nx.step?.startsWith("followup") ? `<span class="tag ${nx.overdue ? "stop" : "plain"}">${esc(nx.label)} ${nx.overdue ? "due now" : "on " + due.toLocaleDateString(undefined, { month: "short", day: "numeric" })}</span>` :
    `<span class="tag caution">${esc(nx.label || "")}</span>${nx.window ? ` <span class="note">${esc(nx.window.label)}</span>` : ""}`;
  const draftBlock = (key, title) => !d || !d[key] ? "" : `<div class="draft"><div class="draft-head"><b>${title}</b>
      ${copyBtn((key === "email" ? `Subject: ${d.subject}\n\n` : "") + d[key])}
      ${c.compose && c.compose[key] ? `<a class="btn quiet" href="${esc(c.compose[key].gmail)}" target="_blank" rel="noopener">Open in Gmail</a>
        <a class="btn quiet" href="${esc(c.compose[key].mailto)}">Mail app</a>` : ""}
      ${key === "linkedin" && c.linkedin ? `<a class="btn quiet" href="${esc(c.linkedin.startsWith("http") ? c.linkedin : "https://" + c.linkedin)}" target="_blank" rel="noopener">Their profile</a>` : ""}</div>
    ${key === "email" ? `<div class="subj">${esc(d.subject)}</div>` : ""}<div class="body">${esc(d[key])}</div></div>`;
  return `<div class="contact" data-cid="${esc(c.id)}">
    <div class="top"><div><div class="name">${esc(c.name)} <span class="tag plain">${esc(c.kind)}</span></div>
      <div class="sub">${esc(c.title || "")}${c.linkedin ? ` · <a href="${esc(c.linkedin.startsWith("http") ? c.linkedin : "https://" + c.linkedin)}" target="_blank" rel="noopener">LinkedIn</a>` : ""}</div>
      <div class="sub">${c.email ? `${esc(c.email)} ${emailTag(c)}` : "No email yet"}${c.email_note ? `<br><span class="note">${esc(c.email_note)}</span>` : ""}
        ${(c.email_alternates || []).length ? `<br><span class="note">Other likely forms: ${c.email_alternates.map(a => `<a href="#" data-use-email="${esc(a)}">${esc(a)}</a>`).join(", ")}</span>` : ""}</div></div>
      <div>${step}</div></div>
    <div class="bar">
      <button data-c="email">${c.email ? "Find email again" : "Find email"}</button>
      <button data-c="draft" ${r._ai ? "" : "disabled title='Needs your Anthropic key'"}>${d ? "Rewrite drafts" : "Write drafts"}</button>
      ${d && !c.sent_at ? `<button class="primary" data-c="sent">I sent the first message</button>` : ""}
      ${nx.step?.startsWith("followup") ? `<button data-c="followup">I sent ${esc(nx.label.toLowerCase())}</button>` : ""}
      ${c.sent_at && !c.replied_at ? `<button data-c="replied">They replied</button>` : ""}
      <button class="quiet" data-c="delete" style="margin-left:auto">Remove</button></div>
    ${d ? `${d.warnings && d.warnings.length ? `<p class="note warn">Check before sending: ${d.warnings.map(esc).join("; ")}</p>` : ""}
      <details ${c.sent_at ? "" : "open"}><summary class="note">Drafts</summary>
      ${draftBlock("email", "First email")}${draftBlock("linkedin", "LinkedIn note")}
      ${draftBlock("followup1", "Follow-up 1 (day 4)")}${draftBlock("followup2", "Follow-up 2 (day 9)")}</details>` : ""}
  </div>`;
}

async function pageOutreach() {
  page.innerHTML = `<div class="head"><div><h1>Outreach</h1>
    <p class="lede">For each job: who to contact (usually the hiring manager, one level above the role), their email,
    and a short first email, LinkedIn note and two follow-ups written from your profile. You send everything yourself;
    mark it sent and follow-ups are reminded on days 4 and 9.</p></div></div>
    <div id="or-top"></div><div id="or-list">Loading…</div>`;
  await drawOutreach();
}

async function drawOutreach() {
  let d;
  try { d = await api("/api/outreach"); } catch (e) { $("#or-list").textContent = e.message; return; }
  outreachSchools = d.schools || [];
  let apps = [];
  try { apps = (await api("/api/applications")).items; } catch { }
  const started = new Set(d.records.map(r => r.job_id));
  const options = apps.filter(a => !started.has(a.id));
  $("#or-top").innerHTML = `<div class="panel"><div class="grid2">
    <div class="field"><label for="or-job">Start with a job you applied to</label>
      <select id="or-job" style="width:100%">${options.length ? options.map(a => `<option value="${esc(a.id)}">${esc(a.company)}: ${esc(a.title)}</option>`).join("") :
        "<option value=''>No other applications yet</option>"}</select>
      <div class="hint">Or press Reach out on any row under Applied.</div></div>
    <div class="field"><label for="or-hunter">Hunter API key (optional)</label>
      <input type="password" id="or-hunter" style="width:100%" placeholder="${d.hunter ? "Saved. Paste a new one to replace it" : "Free plan: 50 credits a month"}" autocomplete="off">
      <div class="hint">Emails are found in order: Hunter's finder, then Apollo, then the company's pattern, each verified when possible. Without keys they're pattern guesses marked unverified.</div></div>
    <div class="field"><label for="or-apollo">Apollo API key (optional)</label>
      <input type="password" id="or-apollo" style="width:100%" placeholder="${d.apollo ? "Saved. Paste a new one to replace it" : "Settings → Integrations → API in Apollo"}" autocomplete="off">
      <div class="hint">Second source when Hunter is unsure. A free Apollo key returns masked addresses, which are ignored; a paid seat reveals them.</div></div>
    </div><div class="bar"><button class="primary" id="or-start" ${options.length ? "" : "disabled"}>Plan outreach</button>
      <button class="quiet" id="or-key">Save keys</button>${d.hunter || d.apollo ? `<button class="quiet" id="or-key-del">Remove keys</button>` : ""}
      ${d.ai ? "" : `<span class="note warn">Add your Anthropic key on My details to get team-specific titles and drafts.</span>`}</div></div>`;
  $("#or-start").onclick = () => $("#or-job").value && startOutreach($("#or-job").value, $("#or-start")).then(drawOutreach);
  $("#or-key").onclick = async () => {
    try {
      if ($("#or-hunter").value) await api("/api/outreach/hunter-key", { key: $("#or-hunter").value });
      if ($("#or-apollo").value) await api("/api/outreach/apollo-key", { key: $("#or-apollo").value });
      toast("Saved."); drawOutreach();
    } catch (e) { toast(e.message, true); }
  };
  if ($("#or-key-del")) $("#or-key-del").onclick = async () => {
    await api("/api/outreach/hunter-key", { key: "" }); await api("/api/outreach/apollo-key", { key: "" }); toast("Removed."); drawOutreach();
  };

  const box = $("#or-list");
  if (!d.records.length) { box.innerHTML = `<div class="panel empty"><strong>No outreach yet.</strong>Pick a job above to see whom to contact.</div>`; return; }
  box.innerHTML = d.records.map(r => {
    r._ai = d.ai;
    const p = r.plan || {};
    return `<section class="outreach" data-job="${esc(r.job_id)}">
      <div class="top"><div><h2><a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.company)}: ${esc(r.title)}</a></h2>
        ${p.team ? `<p class="note">Team: <b>${esc(p.team)}</b>. ${esc(p.why || "")}</p>` : p.why ? `<p class="note">${esc(p.why)}</p>` : ""}
        ${p.angle ? `<p class="note">Lead with: ${esc(p.angle)}</p>` : ""}</div>
        <button class="quiet" data-o="plan">Plan again</button></div>
      ${(p.links || []).length ? `<div class="who-links">${p.links.map(l => `<div class="who"><b>${esc(l.title)}</b>
        <a href="${esc(l.linkedin)}" target="_blank" rel="noopener">Search LinkedIn</a>
        <a href="${esc(l.google)}" target="_blank" rel="noopener">Search Google</a></div>`).join("")}</div>` : ""}
      ${alumniLinks(r.company)}
      <div class="bar" style="margin:8px 0"><button class="primary" data-o="find">${r.candidates ? "Find people again" : "Find people (web search)"}</button>
        <span class="note">Searches public pages with Claude; about 5¢ a job. Never uses your LinkedIn login.</span></div>
      ${r.candidates ? `<div class="candidates">${(r.candidates.people || []).length ? r.candidates.people.map((x, i) => `<div class="who cand">
        <div><b>${esc(x.name)}</b> · ${esc(x.title)} <span class="pill ${esc(x.confidence)}">${esc(x.confidence)}</span> <span class="pill">${esc(x.kind)}</span>
        <div class="sub">${esc(x.evidence)} · <a href="${esc(x.url)}" target="_blank" rel="noopener">source</a></div></div>
        <div><button data-accept="${i}">Add as contact</button></div></div>`).join("") :
        `<p class="note">Nobody could be found on public pages${r.candidates.note ? `: ${esc(r.candidates.note)}` : "."} Use the LinkedIn searches above.</p>`}
        ${(r.candidates.people || []).length && r.candidates.note ? `<p class="note">${esc(r.candidates.note)}</p>` : ""}</div>` : ""}
      <form class="add-contact" autocomplete="off"><div class="grid2">
        <div class="field"><label>Name</label><input type="text" name="name" required style="width:100%" placeholder="Priya Raman"></div>
        <div class="field"><label>Their title</label><input type="text" name="title" style="width:100%" placeholder="Engineering Manager, Payments"></div>
        <div class="field"><label>LinkedIn link</label><input type="text" name="linkedin" style="width:100%" placeholder="linkedin.com/in/…"></div>
        <div class="field"><label>Email, if you have it</label><input type="text" name="email" style="width:100%"></div>
        <div class="field"><label>They are a</label><select name="kind" style="width:100%">
          <option value="manager">Hiring manager</option><option value="engineer">Engineer on the team</option>
          <option value="recruiter">Recruiter</option><option value="other">Someone else</option></select></div>
        <div class="field"><label>Company email domain</label><input type="text" name="domain" style="width:100%" value="${esc(r.domain || "")}"></div>
      </div><div class="bar"><button class="primary">Add person</button></div></form>
      ${r.contacts.map(c => contactCard(r, c)).join("")}
    </section>`;
  }).join("");

  $$(".outreach").forEach(sec => {
    const job = encodeURIComponent(sec.dataset.job);
    $("[data-o=plan]", sec).onclick = async e => {
      e.target.disabled = true; e.target.textContent = "Planning…";
      try { await api(`/api/outreach/${job}/plan`, {}); } catch (err) { toast(err.message, true); }
      drawOutreach();
    };
    $("[data-o=find]", sec).onclick = async e => {
      e.target.disabled = true; e.target.textContent = "Searching public pages… (about a minute)";
      try { await api(`/api/outreach/${job}/find`, {}); } catch (err) { toast(err.message, true); }
      drawOutreach();
    };
    $$("[data-accept]", sec).forEach(b => b.onclick = async () => {
      b.disabled = true;
      try { await api(`/api/outreach/${job}/accept`, { index: Number(b.dataset.accept) }); toast("Added. Find their email, then draft."); drawOutreach(); }
      catch (err) { toast(err.message, true); b.disabled = false; }
    });
    $("form", sec).onsubmit = async e => {
      e.preventDefault();
      const body = Object.fromEntries(new FormData(e.target).entries());
      try { await api(`/api/outreach/${job}/contacts`, body); toast(`Added ${body.name}.`); drawOutreach(); }
      catch (err) { toast(err.message, true); }
    };
    $$(".contact", sec).forEach(card => {
      const cid = card.dataset.cid;
      $$("[data-c]", card).forEach(b => b.onclick = async () => {
        const act = b.dataset.c;
        if (act === "delete" && !confirm("Remove this person and their drafts?")) return;
        const busy = { email: "Looking…", draft: "Writing…" }[act];
        if (busy) { b.disabled = true; b.textContent = busy; }
        try {
          await api(`/api/outreach/${job}/contacts/${cid}/${act}`, act === "email" ? { domain: $("[name=domain]", sec).value } : {});
          if (act === "sent") toast("Marked sent. Follow-up 1 is due in 4 days.");
          drawOutreach();
        } catch (err) { toast(err.message, true); if (busy) { b.disabled = false; b.textContent = "Try again"; } }
      });
      $$("[data-use-email]", card).forEach(a => a.onclick = async e => {
        e.preventDefault();
        try { await api(`/api/outreach/${job}/contacts/${cid}/edit`, { email: a.dataset.useEmail }); toast("Using that address (marked yours)."); drawOutreach(); }
        catch (err) { toast(err.message, true); }
      });
      $$("[data-copy]", card).forEach(b => b.onclick = async () => {
        try { await navigator.clipboard.writeText(b.dataset.copy); toast("Copied."); } catch { toast("Couldn't copy; select the text instead.", true); }
      });
    });
  });
}

let outreachSchools = [];
function alumniLinks(company) {
  // A referral beats a cold note: your schools' alumni at this company (LinkedIn people search, you click it)
  if (!outreachSchools.length || !company) return "";
  return `<div class="who-links">${outreachSchools.map(s => `<div class="who"><b>${esc(s)} alumni at ${esc(company)}</b>
    <a href="https://www.linkedin.com/search/results/people/?keywords=${encodeURIComponent(s + " " + company)}" target="_blank" rel="noopener">Find on LinkedIn</a></div>`).join("")}</div>`;
}

/* ── Gmail security codes ───────────────────────────────── */

async function mailCard() {
  const box = $("#mail"); if (!box) return;
  let m;
  try { m = await api("/api/mail"); } catch (e) { box.textContent = e.message; return; }
  box.innerHTML = `<p class="note" style="margin-top:0">When a job site emails a security code before accepting an application,
    jobpipe reads it from your inbox so unattended submits can finish. It opens Gmail read-only, looks only at mail from job sites
    that arrived after the submit, marks nothing read and keeps nothing. Without this, codes are asked for on Telegram.</p>
    ${m.configured ? `<p><span class="ready">Connected</span> ${esc(m.email)}</p>` : ""}
    <div class="grid2">
      <div class="field"><label for="m_email">Gmail address</label><input type="email" id="m_email" value="${esc(m.email)}" autocomplete="off"></div>
      <div class="field"><label for="m_pw">App password</label><input type="password" id="m_pw" autocomplete="new-password"
        placeholder="${m.has_password ? "Saved. Paste a new one to replace it" : "16 letters from Google"}">
        <div class="hint">Turn on 2-step verification, then make one at myaccount.google.com/apppasswords. Not your normal password.</div></div>
    </div>
    <div class="bar"><button class="primary" id="m_save">Save</button><button id="m_test" ${m.configured ? "" : "disabled"}>Test it</button>
      ${m.configured ? `<button class="quiet" id="m_forget">Remove</button>` : ""}<span class="note" id="m_note"></span></div>`;
  $("#m_save").onclick = async () => {
    try { await api("/api/mail", { email: $("#m_email").value, app_password: $("#m_pw").value, enabled: true }); toast("Saved."); mailCard(); }
    catch (e) { toast(e.message, true); }
  };
  if ($("#m_test")) $("#m_test").onclick = async () => {
    $("#m_note").textContent = "Connecting…";
    try { const r = await api("/api/mail/test", {}); $("#m_note").textContent = r.message; $("#m_note").className = "note " + (r.ok ? "" : "warn"); }
    catch (e) { $("#m_note").textContent = e.message; }
  };
  if ($("#m_forget")) $("#m_forget").onclick = async () => { await api("/api/mail/forget", {}); toast("Removed."); mailCard(); };
}

async function runDoctor() {
  const box = $("#doctor");
  box.innerHTML = `<p class="note">Checking (starting a browser takes a few seconds)…</p>`;
  let d;
  try { d = await api("/api/doctor?online=1"); } catch (e) { box.textContent = e.message; return; }
  const icon = { ok: "✓", warn: "!", fail: "✗" };
  box.innerHTML = `<div class="checks">${d.checks.map(c => `<div class="check-row ${c.level}">
      <span class="ic" aria-label="${c.level}">${icon[c.level]}</span><b>${esc(c.name)}</b><span>${esc(c.message)}</span></div>`).join("")}</div>
    <div class="bar"><button id="runDoctor">Check again</button></div>`;
  $("#runDoctor").onclick = runDoctor;
}

/* ── Every answer on one application ─────────────────────── */

/* ── Interview prep pack ─────────────────────────────────── */

async function showPrep(jobId) {
  const dlg = $("#modal"), body = $("#modal-body");
  body.innerHTML = `<p>Loading…</p>`;
  if (!dlg.open) dlg.showModal();
  let d;
  try { d = await api(`/api/prep/${encodeURIComponent(jobId)}`); } catch (e) { body.innerHTML = `<p>${esc(e.message)}</p>`; return; }
  const p = d.prep || {}, pack = p.pack || {}, r = p.research || {};
  const head = `<div class="modal-head"><h2>Interview prep: ${esc(p.company || "")} ${p.title ? "· " + esc(p.title) : ""}</h2>
      <button class="quiet" id="mClose" aria-label="Close">Close</button></div>`;
  if (!d.exists) {
    body.innerHTML = `${head}
      <p class="note">${d.building ? "Being written now: research on the company and its interview loop, then a pack from your career profile. Two to four minutes." :
        d.last_error ? `The last attempt failed: ${esc(d.last_error)}` :
        "No pack yet. It researches the company and its reported interview loop, then writes the questions to expect, your best stories for each (checked against your profile), the gaps they will probe, and a two-day plan."}</p>
      ${d.building ? "" : `<div class="field"><label for="prep-notes">Anything you know about this loop (recruiter's description, the rounds, who you meet)</label>
        <textarea id="prep-notes" rows="3" style="width:100%"></textarea></div>
        <div class="bar"><button class="primary" id="prep-build" ${d.ai_ready ? "" : "disabled"}>Write the prep pack</button>
        ${d.ai_ready ? `<span class="note">About 50¢ with Opus.</span>` : `<span class="note warn">Needs your Anthropic key (My details).</span>`}</div>`}`;
    $("#mClose").onclick = () => dlg.close();
    if ($("#prep-build")) $("#prep-build").onclick = async () => {
      $("#prep-build").disabled = true;
      try { await api(`/api/prep/${encodeURIComponent(jobId)}/build`, { notes: $("#prep-notes").value }); }
      catch (e) { toast(e.message, true); }
      showPrep(jobId);
    };
    if (d.building) setTimeout(() => dlg.open && showPrep(jobId), 15000);
    return;
  }
  const q = x => `<div class="prep-q"><b>${esc(x.q)}</b> <span class="pill">${esc(x.kind)}</span>
      <div class="sub">${esc(x.answer)}</div>
      ${x.unsupported ? `<div class="note warn">Not in your profile, don't use: ${x.unsupported.map(esc).join("; ")}</div>` : ""}</div>`;
  body.innerHTML = `${head}
    ${pack.headline ? `<p class="lede">${esc(pack.headline)}</p>` : ""}
    ${r.company ? `<h3>The company</h3><p>${esc(r.company)}</p>
      ${r.recent?.length ? `<ul>${r.recent.map(x => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
      ${r.stack?.length ? `<p class="note">Stack seen: ${r.stack.map(esc).join(", ")}</p>` : ""}` : ""}
    ${r.unknown ? `<p class="note">${esc(r.unknown)}</p>` : ""}
    ${(pack.rounds || []).map(rd => `<h3>${esc(rd.round)}</h3><p class="note">${esc(rd.format || "")}</p>${(rd.questions || []).map(q).join("")}`).join("")}
    ${(pack.stories || []).length ? `<h3>Your stories</h3>${pack.stories.map(st => `<div class="prep-q"><b>${esc(st.name)}</b> <span class="note">${esc(st.from)}</span>
      <div class="sub"><b>Situation.</b> ${esc(st.situation)} <b>Action.</b> ${esc(st.action)} <b>Result.</b> ${esc(st.result)}</div>
      <div class="note">Use for: ${(st.use_for || []).map(esc).join(", ")}</div>
      ${st.unsupported ? `<div class="note warn">Check this one: ${esc(st.unsupported)}</div>` : ""}</div>`).join("")}` : ""}
    ${(pack.gaps || []).length ? `<h3>What they will probe</h3>${pack.gaps.map(g => `<div class="prep-q"><b>${esc(g.probe)}</b><div class="sub">${esc(g.answer)}</div></div>`).join("")}` : ""}
    ${(pack.ask_them || []).length ? `<h3>Ask them</h3><ul>${pack.ask_them.map(x => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
    ${(pack.plan || []).length ? `<h3>Plan</h3><ul>${pack.plan.map(x => `<li><b>${esc(x.when)}</b> ${esc(x.do)}</li>`).join("")}</ul>` : ""}
    ${(r.sources || []).length ? `<p class="note">Sources: ${r.sources.map(u => `<a href="${esc(u)}" target="_blank" rel="noopener">${esc(u.replace(/^https?:\/\/(www\.)?/, "").slice(0, 40))}</a>`).join(" · ")}</p>` : ""}
    <h3>After a round: thank-you note</h3>
    <div class="grid2"><div class="field"><label for="ty-who">Interviewer</label><input id="ty-who" type="text" style="width:100%"></div>
      <div class="field"><label for="ty-notes">What you discussed (a few lines)</label><textarea id="ty-notes" rows="2" style="width:100%"></textarea></div></div>
    <div class="bar"><button id="ty-go">Draft the note</button> <button class="quiet" id="prep-again">Write the pack again</button>
      <span class="note">Written ${esc((p.built_at || "").replace("T", " "))}${p.cost ? `, $${p.cost}` : ""}</span></div>
    <div id="ty-out">${(p.thank_you || []).map(t => `<div class="draft"><div class="draft-head"><b>To ${esc(t.interviewer || "the interviewer")}: ${esc(t.subject)}</b> ${copyBtn(`Subject: ${t.subject}\n\n${t.body}`)}</div><pre>${esc(t.body)}</pre></div>`).join("")}</div>`;
  $("#mClose").onclick = () => dlg.close();
  $("#ty-go").onclick = async () => {
    $("#ty-go").disabled = true; $("#ty-go").textContent = "Writing…";
    try { await api(`/api/prep/${encodeURIComponent(jobId)}/thanks`, { interviewer: $("#ty-who").value, notes: $("#ty-notes").value }); showPrep(jobId); }
    catch (e) { toast(e.message, true); $("#ty-go").disabled = false; $("#ty-go").textContent = "Draft the note"; }
  };
  $("#prep-again").onclick = async () => {
    try { await api(`/api/prep/${encodeURIComponent(jobId)}/build`, {}); showPrep(jobId); } catch (e) { toast(e.message, true); }
  };
  $$("[data-copy]", body).forEach(b => b.onclick = async () => {
    try { await navigator.clipboard.writeText(b.dataset.copy); toast("Copied."); } catch { toast("Couldn't copy; select the text instead.", true); }
  });
}

async function showSent(jobId, finishing) {
  const dlg = $("#modal"), body = $("#modal-body");
  body.innerHTML = `<p>Loading…</p>`;
  dlg.showModal();
  let d;
  try { d = await api(`/api/sent/${encodeURIComponent(jobId)}`); } catch (e) { body.innerHTML = `<p>${esc(e.message)}</p>`; return; }
  const rows = d.answers.map(a => `<div class="sent-row"><div class="lbl">${esc(a.label)}${a.ai ? ` <span class="tag caution">Claude wrote this</span>` : ""}</div>
      <div class="val">${a.kind === "file" ? esc(a.value.split("/").pop()) : esc(a.value)}</div>
      ${a.kind === "file" ? "" : `<button class="quiet" data-copy="${esc(a.value)}">Copy</button>`}</div>`).join("");
  body.innerHTML = `<div class="modal-head"><h2>${esc(d.job.company)}: ${esc(d.job.title)}</h2>
      <button class="quiet" id="mClose" aria-label="Close">Close</button></div>
    <p class="note">${finishing ? "Open the application, then paste each answer. Press I submitted it when the site confirms."
      : d.submitted ? `Sent. ${esc(d.submit_note)}` : "Prepared, not sent yet."}</p>
    <div class="bar">${d.application_url ? `<a class="btn primary" href="${esc(d.application_url)}" target="_blank" rel="noopener">Open the application</a>` : ""}
      ${d.resume_file ? `<a class="btn" href="${esc(d.resume_file)}" target="_blank">Resume PDF</a>` : ""}
      ${d.screenshot ? `<a class="btn quiet" href="${esc(d.screenshot)}" target="_blank">Screenshot</a>` : ""}
      ${finishing ? `<button id="mDone">I submitted it</button>` : ""}</div>
    ${d.needs.length ? `<p class="note warn">Still needs you: ${d.needs.map(n => esc(n.label)).join("; ")}</p>` : ""}
    <div class="sent">${rows || `<p class="note">No answers recorded.</p>`}</div>`;
  $("#mClose").onclick = () => dlg.close();
  $$("[data-copy]", dlg).forEach(b => b.onclick = async () => {
    try { await navigator.clipboard.writeText(b.dataset.copy); b.textContent = "Copied"; } catch { toast("Couldn't copy; select the text.", true); }
  });
  if ($("#mDone")) $("#mDone").onclick = async () => { if (await setStatus(jobId, "applied")) { dlg.close(); toast("Marked applied."); route(); } };
}

/* ── Leads: hiring posts from people ─────────────────────── */

const SRC = { hn: "Hacker News", bluesky: "Bluesky", x: "X", manual: "You sent it", linkedin_email: "LinkedIn alert" };

async function pageLeads() {
  page.innerHTML = `<div class="head"><div><h1>Leads</h1>
    <p class="lede">People posting that they're hiring, often before a job board does: the monthly Hacker News
    "Who is hiring?" thread, YC job posts, Bluesky, and X if you add a key. Claude reads each post; good ones get
    their company's job board added to the crawler, so the real opening is caught and applied to like any other.
    LinkedIn isn't scraped (it bans accounts for that): forward posts you see to your Telegram bot, as text,
    a link or a screenshot, or paste them below.</p></div>
    <div class="bar"><button class="primary" id="ld-fetch">Collect now</button></div></div>
    <div class="panel"><div class="field"><label for="ld-text">Add a post you saw</label>
      <textarea id="ld-text" rows="3" placeholder="Paste a LinkedIn or X post: who's hiring, the role, any link"></textarea></div>
      <div class="bar"><button id="ld-add">Read it</button><span class="note" id="ld-note"></span></div></div>
    <div class="tabs" role="tablist" id="ld-tabs">
      <button data-st="new" class="on">New</button><button data-st="watching">Watching</button>
      <button data-st="dismissed">Dismissed</button><button data-st="all">All</button></div>
    <div id="ld-list">Loading…</div>
    <h2>Sources</h2><div class="panel" id="ld-settings"></div>`;
  let status = "new";
  $$("#ld-tabs button").forEach(b => b.onclick = () => {
    $$("#ld-tabs button").forEach(x => x.classList.toggle("on", x === b)); status = b.dataset.st; drawLeads(status);
  });
  $("#ld-fetch").onclick = async () => {
    const r = await api("/api/leads/fetch", {}); toast(r.started ? "Collecting; new leads appear in a minute." : r.note);
    setTimeout(() => drawLeads(status), 15000);
  };
  $("#ld-add").onclick = async () => {
    const t = $("#ld-text").value.trim(); if (!t) return;
    $("#ld-note").textContent = "Reading…";
    try {
      const r = await api("/api/leads/add", { text: t });
      $("#ld-note").textContent = r.found.length ? `Found ${r.found.map(f => [f.company, f.role].filter(Boolean).join(": ")).join("; ")}`
        : "That doesn't read as a hiring post.";
      $("#ld-text").value = ""; drawLeads(status);
    } catch (e) { $("#ld-note").textContent = e.message; }
  };
  await drawLeads(status);
}

async function drawLeads(status) {
  let d;
  try { d = await api(`/api/leads?status=${status}`); } catch (e) { $("#ld-list").textContent = e.message; return; }
  const box = $("#ld-list");
  if (!d.leads.length) {
    box.innerHTML = `<div class="panel empty"><strong>${status === "new" ? "No new leads." : "Nothing here."}</strong>
      ${status === "new" ? "Collection runs every " + d.settings.every_minutes + " minutes while the crawler runs, or press Collect now." : ""}</div>`;
  } else {
    box.innerHTML = d.leads.map(l => {
      const age = l.posted_at ? Math.max(0, Math.round((Date.now() - new Date(l.posted_at)) / 3600000)) : null;
      return `<div class="lead" data-id="${esc(l.id)}">
        <div class="top"><div><div class="name">${esc(l.company || "Unknown company")}${l.role ? `: ${esc(l.role)}` : ""}</div>
          <div class="sub">${SRC[l.source] || esc(l.source)}${age !== null ? ` · ${age < 24 ? age + " h" : Math.round(age / 24) + " d"} ago` : ""}
            ${l.location ? ` · ${esc(l.location)}` : ""}${l.remote ? " · remote" : ""}${l.author ? ` · by ${esc(l.author)}` : ""}</div></div>
          <div class="tags"><span class="score">${Math.round(l.score)}</span>
            ${l.visa === "yes" ? `<span class="tag good">visa mentioned</span>` : l.visa === "no" ? `<span class="tag stop">no sponsorship</span>` : ""}
            ${l.board ? `<span class="tag plain">watching ${esc(l.board)}</span>` : ""}</div></div>
        <p class="snippet">${esc(l.snippet)}</p>
        <div class="bar">${l.url ? `<a class="btn" href="${esc(l.url)}" target="_blank" rel="noopener">Open the post</a>` : ""}
          ${l.apply_url && l.apply_url !== l.url ? `<a class="btn" href="${esc(l.apply_url)}" target="_blank" rel="noopener">Apply link</a>` : ""}
          ${l.author_url ? `<a class="btn quiet" href="${esc(l.author_url)}" target="_blank" rel="noopener">Poster's profile</a>` : ""}
          ${l.status !== "watching" ? `<button data-a="watch">Watch company</button>` : ""}
          ${l.status === "dismissed" ? `<button class="quiet" data-a="restore">Restore</button>` : `<button class="quiet" data-a="dismiss">Dismiss</button>`}
        </div></div>`;
    }).join("");
    $$(".lead [data-a]", box).forEach(b => b.onclick = async () => {
      b.disabled = true;
      try { await api("/api/leads/action", { id: b.closest(".lead").dataset.id, action: b.dataset.a }); drawLeads(status); }
      catch (e) { toast(e.message, true); b.disabled = false; }
    });
  }
  const s = d.settings, c = d.counts.by_source || {};
  $("#ld-settings").innerHTML = `<div class="src-row">
      <label class="check"><input type="checkbox" id="ls-hn" ${s.hn ? "checked" : ""}> Hacker News <span class="note">${c.hn || 0} leads · free</span></label>
      <label class="check"><input type="checkbox" id="ls-bluesky" ${s.bluesky ? "checked" : ""}> Bluesky <span class="note">${c.bluesky || 0} leads · free</span></label>
      <label class="check"><input type="checkbox" id="ls-x" ${s.x ? "checked" : ""} ${s.has_x_token ? "" : "disabled"}> X <span class="note">${s.has_x_token
        ? `${c.x || 0} leads · ${s.x_today} of ${s.x_daily_posts} posts read today` : "add a token below"}</span></label>
    </div>
    <div class="grid2">
      <div class="field"><label for="ls-xtok">X API bearer token</label><input type="password" id="ls-xtok" autocomplete="off"
        placeholder="${s.has_x_token ? "Saved. Paste a new one to replace it" : "From console.x.com"}">
        <div class="hint">Pay-per-use, about $0.005 per post read.</div></div>
      <div class="field"><label for="ls-cap">X posts per day at most</label><input type="number" id="ls-cap" min="0" max="5000" value="${s.x_daily_posts}">
        <div class="hint">About $${(s.x_daily_posts * 0.005).toFixed(2)} a day at this cap.</div></div>
      <div class="field"><label for="ls-bh">Bluesky handle (optional)</label><input type="text" id="ls-bh" value="${esc(s.bsky_handle)}" placeholder="you.bsky.social">
        <div class="hint">Only needed if Bluesky stops answering searches without signing in.</div></div>
      <div class="field"><label for="ls-bp">Bluesky app password</label><input type="password" id="ls-bp" autocomplete="off"
        placeholder="${s.has_bsky_password ? "Saved" : "Settings → Privacy and security → App passwords"}"></div>
      <div class="field"><label for="ls-min">Alert me and watch the company from score</label><input type="number" id="ls-min" min="0" max="100" value="${s.alert_min_score}"></div>
    </div>
    <div class="field"><label for="ls-q">Search phrases for Bluesky and X, one per line</label><textarea id="ls-q" rows="5">${esc(s.queries.join("\n"))}</textarea></div>
    <div class="bar"><button class="primary" id="ls-save">Save</button><span class="note">${d.counts.posts_read} posts read so far.</span></div>`;
  $("#ls-save").onclick = async () => {
    const body = { hn: $("#ls-hn").checked, bluesky: $("#ls-bluesky").checked, x: $("#ls-x").checked,
      x_daily_posts: +$("#ls-cap").value, alert_min_score: +$("#ls-min").value, queries: $("#ls-q").value };
    if ($("#ls-xtok").value) { body.x_token = $("#ls-xtok").value; body.x = true; }
    body.bsky_handle = $("#ls-bh").value;
    if ($("#ls-bp").value) body.bsky_app_password = $("#ls-bp").value;
    try { await api("/api/leads/settings", body); toast("Saved."); drawLeads(status); } catch (e) { toast(e.message, true); }
  };
}

const routes = { jobs: pageJobs, attention: pageAttention, applied: pageApplied, search: pageSearch, auto: pageAuto,
  crawler: pageCrawler, career: pageCareer, outreach: pageOutreach, leads: pageLeads };

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
