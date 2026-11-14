"""JSON API behind the new dashboard pages.

Live jobs · Needs you · Applied · Auto-apply · Crawler. Mixed into
DashboardHandler (app.py), which already handles auth and the profile API.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

TERMINAL = ("applied", "interview", "offer", "rejected", "skipped")
APPLIED = ("applied", "interview", "offer", "rejected")
STATUSES = ("scraped", "applied", "interview", "offer", "rejected", "skipped")
_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


def can_open_browser() -> bool:
    """A visible browser can only open where the dashboard runs on your own computer."""
    return os.environ.get("JOBPIPE_HEADLESS_ONLY", "") not in ("1", "true", "yes")


def _folder(pdir: Path, job_id: str) -> Path:
    return pdir / "applications" / _SAFE.sub("_", job_id)


def _report(pdir: Path, job_id: str) -> dict | None:
    p = _folder(pdir, job_id) / "report.json"
    try:
        data = json.loads(p.read_text())
        data["updated_at"] = datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")
        return data
    except (OSError, ValueError):
        return None


def _file_url(job_id: str, path: str | None) -> str | None:
    if not path:
        return None
    return f"/files/{_SAFE.sub('_', job_id)}/{Path(path).name}"


def _fit(job: dict) -> dict | None:
    try:
        return json.loads(job.get("ai_fit") or "null")
    except ValueError:
        return None


def job_card(job: dict, pdir: Path | None = None) -> dict:
    """What the pages need to know about a job."""
    from pipeline.apply.filler import application_url
    from pipeline.filters.sponsorship import check_jd_blocks_sponsorship
    fit = _fit(job)
    card = {k: job.get(k) for k in (
        "id", "title", "company", "location", "url", "platform", "fit_score", "sponsorship",
        "h1b_filings_count", "posted_at", "first_seen_at", "baseline", "status", "applied_at",
        "notes", "closed_at", "source")}
    card["fit_score"] = round(card["fit_score"] or 0)
    card["ai_verdict"] = (fit or {}).get("verdict", "")
    card["ai_summary"] = (fit or {}).get("summary", "")
    card["ai_dealbreakers"] = (fit or {}).get("dealbreakers", [])
    card["jd_blocked"] = check_jd_blocks_sponsorship(job.get("description") or "")
    card["apply_supported"] = bool(application_url(job))
    card["age_min"], card["age_basis"] = _age(job)
    card["detect_delay_min"] = _delay(job)
    if pdir is not None:
        card["attempted"] = (_folder(pdir, job["id"]) / "report.json").exists()
    return card


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _minutes_since(dt: datetime) -> float:
    from datetime import timezone
    now = datetime.now(timezone.utc) if dt.tzinfo else datetime.now()
    return max(0.0, (now - dt).total_seconds() / 60)


def _age(job: dict) -> tuple[float | None, str | None]:
    """Minutes since the posting opened: since we first saw it when we caught
    it going live, else since the company says it was posted."""
    seen = _parse(job.get("first_seen_at"))
    posted = _parse(job.get("posted_at"))
    if seen and not job.get("baseline"):
        if posted is not None:
            p = _minutes_since(posted)
            s = _minutes_since(seen)
            if p < s:                       # the board's own date is more recent? trust the earlier sighting
                return round(s, 1), "seen"
        return round(_minutes_since(seen), 1), "seen"
    if posted:
        return round(_minutes_since(posted), 1), "posted"
    return None, None


def _delay(job: dict) -> float | None:
    """How long after posting we caught it (when we saw it go live)."""
    seen, posted = _parse(job.get("first_seen_at")), _parse(job.get("posted_at"))
    if job.get("baseline") or not seen or not posted:
        return None
    from datetime import timezone
    if posted.tzinfo:
        seen = seen.astimezone().astimezone(timezone.utc) if seen.tzinfo else seen.astimezone(timezone.utc)
        posted = posted.astimezone(timezone.utc)
    return round(max(0.0, (seen - posted).total_seconds() / 60), 1)


class ApiMixin:
    """GET/POST handlers. Each returns (payload, status) or None if the path isn't ours."""

    # Provided by DashboardHandler
    def _pdir(self) -> Path: ...                       # noqa: E704
    def _tracker(self): ...                            # noqa: E704

    # ── GET ────────────────────────────────────────────────────────────

    def api_get(self, path: str, query: dict[str, list[str]]):
        q = {k: v[0] for k, v in query.items()}
        if path == "/api/feed":
            return self._feed(q), 200
        if path == "/api/attention":
            return self._attention(), 200
        if path == "/api/applications":
            return self._applications(), 200
        if path == "/api/autoapply":
            return self._autoapply(), 200
        if path == "/api/crawler":
            return self._crawler(), 200
        if path == "/api/summary":
            return self._summary(), 200
        m = re.fullmatch(r"/api/runs/(\d{8}-\d{6})/log", path)
        if m:
            from pipeline.autoapply import Runner
            text, offset = Runner(self._pdir()).log(m.group(1), int(q.get("offset", 0) or 0))
            meta = Runner(self._pdir()).get(m.group(1)) or {}
            return {"text": text, "offset": offset, "status": meta.get("status")}, 200
        return None

    def _summary(self) -> dict:
        t = self._tracker()
        now = datetime.now()
        hour = (now - timedelta(hours=1)).isoformat(timespec="seconds")
        day = (now - timedelta(hours=24)).isoformat(timespec="seconds")
        new = [e for e in t.events(since=day, limit=2000) if e["kind"] == "new"]
        return {"new_last_hour": sum(1 for e in new if e["at"] >= hour), "new_today": len(new),
                "attention": len(self._attention_jobs()), "applied": len(t.by_status(APPLIED)),
                "crawler_running": self._crawler_status().get("running", False)}

    def _feed(self, q: dict) -> dict:
        t = self._tracker()
        hours = q.get("hours", "72")
        max_age = None if hours == "all" else float(hours)
        jobs = t.open_jobs(max_age_hours=max_age, limit=1500)
        pdir = self._pdir()
        cards = [job_card(j, pdir) for j in jobs]
        return {"jobs": cards, "now": datetime.now().isoformat(timespec="seconds"),
                "crawler": self._crawler_status(), "can_open_browser": can_open_browser()}

    # Needs you: applications the bot prepared that are waiting on you

    def _attention_jobs(self) -> list[dict]:
        pdir = self._pdir()
        root = pdir / "applications"
        if not root.exists():
            return []
        t = self._tracker()
        out = []
        for rp in root.glob("*/report.json"):
            try:
                rep = json.loads(rp.read_text())
            except (OSError, ValueError):
                continue
            job = t.get_job(rep.get("job_id", ""))
            if not job or job["status"] in TERMINAL or job.get("closed_at") or rep.get("submitted"):
                continue
            if rep.get("closed"):
                continue
            needs = rep.get("needs_attention") or []
            if not needs and not rep.get("ready_to_submit"):
                continue
            out.append((job, rep, rp))
        return out

    def _attention(self) -> dict:
        from pipeline import applicant
        from pipeline.ai import client as ai_client
        pdir = self._pdir()
        items = []
        for job, rep, rp in self._attention_jobs():
            items.append({
                "job": job_card(job, pdir),
                "needs": [{"label": f["label"], "source": f["source"], "kind": f["kind"],
                           "options": f.get("options") or []} for f in rep.get("needs_attention") or []],
                "filled": len(rep.get("filled") or []),
                "ai_written": rep.get("ai_written") or [],
                "ready": bool(rep.get("ready_to_submit")),
                "captcha": bool(rep.get("captcha")),
                "resume": rep.get("resume_attached"),
                "error": rep.get("error") or "",
                "screenshot": _file_url(job["id"], rep.get("screenshot")),
                "prepared_at": datetime.fromtimestamp(rp.stat().st_mtime).isoformat(timespec="seconds"),
            })
        items.sort(key=lambda i: (not i["ready"], -(i["job"]["fit_score"] or 0)))
        pending = applicant.load_pending(pdir)
        ready, why = ai_client.status(pdir)
        return {"items": items, "pending": pending, "ai_ready": ready, "ai_why": why,
                "can_open_browser": can_open_browser()}

    def _applications(self) -> dict:
        pdir = self._pdir()
        out = []
        for job in self._tracker().by_status(APPLIED):
            rep = _report(pdir, job["id"]) or {}
            folder = _folder(pdir, job["id"])
            resume = next(iter(sorted(folder.glob("*_Resume.pdf"))), None) if folder.exists() else None
            ai = [f for f in rep.get("filled") or [] if f.get("label") in (rep.get("ai_written") or [])]
            out.append({**job_card(job, pdir),
                        "submit_note": rep.get("submit_note", ""), "mode": rep.get("mode", ""),
                        "screenshot": _file_url(job["id"], rep.get("screenshot")),
                        "resume_file": _file_url(job["id"], str(resume)) if resume else None,
                        "ai_answers": [{"label": f["label"], "value": f["value"]} for f in ai],
                        "by_bot": bool(rep.get("submitted"))})
        return {"items": out}

    def _autoapply(self) -> dict:
        from pipeline import autoapply
        pdir = self._pdir()
        settings = autoapply.load_settings(pdir)
        picks = autoapply.candidates(self._tracker(), settings, pdir)[:12]
        return {"settings": settings, "today": autoapply.today_attempts(pdir),
                "candidates": [job_card(j, pdir) for j in picks],
                "runs": autoapply.Runner(pdir).list(limit=15),
                "can_open_browser": can_open_browser(),
                "crawler_running": self._crawler_status().get("running", False)}

    def _crawler_status(self) -> dict:
        from pipeline.crawler.worker import read_status
        return read_status()

    def _crawler(self) -> dict:
        from pipeline.discovery.registry import BoardRegistry
        st = self._crawler_status()
        reg = BoardRegistry()
        with reg._conn() as conn:
            conn.row_factory = __import__("sqlite3").Row
            from pipeline.discovery.registry import pretty_slug
            boards = [dict(r) for r in conn.execute(
                "SELECT platform, slug, name, rate, interval_s, last_fetch_at, next_due_at, "
                "last_change_at, new_total, fetches, not_modified, last_status, fail_count "
                "FROM boards ORDER BY COALESCE(last_change_at, '') DESC, rate DESC LIMIT 60")]
            for b in boards:
                b["name"] = b["name"] or pretty_slug(b["slug"])
            by_platform = dict(conn.execute(
                "SELECT platform, COUNT(*) FROM boards WHERE fail_count < 3 GROUP BY platform").fetchall())
        events = self._tracker().events(limit=60)
        return {"status": st, "boards": boards, "by_platform": by_platform, "events": events}

    # ── POST ───────────────────────────────────────────────────────────

    def api_post(self, path: str, data: dict):
        from pipeline import autoapply
        pdir = self._pdir()
        m = re.fullmatch(r"/api/jobs/([A-Za-z0-9_.:-]+)/status", path)
        if m:
            status = data.get("status")
            if status not in STATUSES:
                return {"error": f"status must be one of {', '.join(STATUSES)}"}, 400
            from pipeline.models import ApplicationStatus
            t = self._tracker()
            if not t.get_job(m.group(1)):
                return {"error": "no such job"}, 404
            t.update_status(m.group(1), ApplicationStatus(status), notes=str(data.get("notes", ""))[:500])
            return {"ok": True}, 200
        if path == "/api/apply/run":
            ids = [str(i) for i in data.get("job_ids") or []][:25]
            mode = data.get("mode", "practice")
            headed = bool(data.get("headed"))
            if (headed or mode == "handoff") and not can_open_browser():
                return {"error": "This dashboard runs on a server without a screen, so it can't open a "
                                 "browser for you. Run it on your computer for that."}, 400
            try:
                args = autoapply.apply_args(ids, top=int(data.get("top", 5)),
                                            min_score=float(data.get("min_score", 70)), mode=mode,
                                            headed=headed, trust_ai=bool(data.get("trust_ai")),
                                            fit=bool(data.get("fit", True)), tailor=bool(data.get("tailor", True)))
                label = data.get("label") or {"practice": "Fill", "submit": "Submit", "handoff": "Open & finish"}[mode]
                meta = autoapply.Runner(pdir).start(args, str(label)[:120])
            except (ValueError, RuntimeError) as e:
                return {"error": str(e)}, 409 if isinstance(e, RuntimeError) else 400
            return meta, 200
        m = re.fullmatch(r"/api/runs/(\d{8}-\d{6})/stop", path)
        if m:
            return {"stopped": autoapply.Runner(pdir).stop(m.group(1))}, 200
        if path == "/api/autoapply":
            try:
                return {"settings": autoapply.save_settings(data, pdir)}, 200
            except (ValueError, TypeError) as e:
                return {"error": str(e)}, 400
        if path == "/api/attention/suggest":
            return self._suggest(data)
        return None

    def _suggest(self, data: dict):
        """AI suggestions for one company's waiting questions, for you to review."""
        from pipeline import applicant
        from pipeline.ai import client as ai_client
        from pipeline.ai.context import load_resume_text
        from pipeline.ai.writer import can_pick, draft_answer, is_open_question, pick_answer
        pdir = self._pdir()
        ready, why = ai_client.status(pdir)
        if not ready:
            return {"error": why}, 400
        company = str(data.get("company", ""))
        pending = applicant.load_pending(pdir).get(company, {})
        wanted = data.get("questions") or list(pending)
        resume_text = load_resume_text(pdir)
        if not resume_text.strip():
            return {"error": "Upload your resume on My details first — suggestions are built from it."}, 400
        details = applicant.load(pdir)
        t = self._tracker()
        out: dict[str, Any] = {}
        for q in wanted[:20]:
            entry = pending.get(q)
            if not entry:
                continue
            job = t.get_job(entry.get("job_id", "")) or self._latest_job_for(company) or {"company": company}
            label, opts = entry.get("label", q), entry.get("options") or []
            kind = "checkboxes" if entry.get("multi") else ("select" if opts else "textarea")
            try:
                if not opts and is_open_question(label, "textarea"):
                    text = draft_answer(label, job, details, resume_text, pdir)
                    out[q] = {"value": text, "how": "draft", "basis": "written from your resume"} if text else None
                elif can_pick(label, kind):
                    a = pick_answer(label, kind, opts, job, details, resume_text, pdir)
                    out[q] = {"value": a.value, "how": "pick", "basis": a.basis} if a else None
                else:
                    out[q] = None
            except ai_client.AIUnavailable as e:
                return {"suggestions": out, "error": str(e)}, 200
            except Exception as e:                      # API hiccup on one question
                out[q] = {"error": str(e)[:160]}
        return {"suggestions": out}, 200

    def _latest_job_for(self, company: str) -> dict | None:
        t = self._tracker()
        conn = t._connect()
        conn.row_factory = __import__("sqlite3").Row
        row = conn.execute("SELECT * FROM jobs WHERE company = ? ORDER BY COALESCE(last_seen, '') DESC LIMIT 1",
                           (company,)).fetchone()
        conn.close()
        return dict(row) if row else None

    # ── Files (screenshots, tailored resumes) ──────────────────────────

    def file_response(self, path: str) -> tuple[bytes, str] | None:
        m = re.fullmatch(r"/files/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+\.(?:png|pdf))", path)
        if not m:
            return None
        root = (self._pdir() / "applications").resolve()
        target = (root / m.group(1) / m.group(2)).resolve()
        if root not in target.parents or not target.is_file():
            return None
        return target.read_bytes(), mimetypes.guess_type(target.name)[0] or "application/octet-stream"
