"""JSON API behind the new dashboard pages.

Live jobs · Needs you · Applied · Auto-apply · Crawler. Mixed into
DashboardHandler (app.py), which already handles auth and the profile API.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pipeline.ai import client as ai_client

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


def _resume_pdf(folder: Path, info: dict | None = None) -> Path | None:
    """The resume made for this job (named after you, e.g. Amudhan_Muthaiah_Resume.pdf)."""
    if not folder.exists():
        return None
    if info is None:
        try:
            info = json.loads((folder / "tailor.json").read_text())
        except (OSError, ValueError):
            info = {}
    named = folder / info["pdf_name"] if info.get("pdf_name") else None
    if named and named.exists():
        return named
    return next(iter(sorted(folder.glob("*Resume.pdf"))), None)


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
    from pipeline.odds import odds
    card["odds"] = odds({**job, "jd_blocked": card["jd_blocked"]}, years=_years())
    card["detect_delay_min"] = _delay(job)
    if pdir is not None:
        card["attempted"] = (_folder(pdir, job["id"]) / "report.json").exists()
    return card


_YEARS: list[float] = []


def _years() -> float:
    """Years of experience from config (profile.years_experience), read once."""
    if not _YEARS:
        try:
            from pipeline.utils.config import load_config
            _YEARS.append(float((load_config().get("profile") or {}).get("years_experience") or 3))
        except Exception:
            _YEARS.append(3.0)
    return _YEARS[0]


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
    """Minutes since the posting opened. Never flattering: when the company
    dates the posting earlier than our first sighting, the company's date
    counts (we didn't catch it opening); our sighting counts only when it is
    the earlier of the two or the only one."""
    seen = _parse(job.get("first_seen_at"))
    posted = _parse(job.get("posted_at"))
    if seen and not job.get("baseline"):
        if posted is not None:
            p = _minutes_since(posted)
            s = _minutes_since(seen)
            if p > s:
                return round(p, 1), "posted"
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
        if path == "/api/scorecard":
            from pipeline import outreach
            pdir = self._pdir()
            return {"week": {**self._tracker().scorecard(7), "outreach": outreach.stats(pdir, 7)},
                    "month": {**self._tracker().scorecard(30), "outreach": outreach.stats(pdir, 30)}}, 200
        if path == "/api/autoapply":
            return self._autoapply(), 200
        if path == "/api/crawler":
            return self._crawler(), 200
        if path == "/api/summary":
            return self._summary(), 200
        if path == "/api/searches":
            return self._searches(), 200
        if path == "/api/telegram":
            return self._telegram_info(), 200
        if path == "/api/career-profile":
            return self._career_profile(), 200
        if path == "/api/linkedin":
            from pipeline import linkedin
            return {"profile": linkedin.load(self._pdir()), "ai_ready": ai_client.status(self._pdir())[0]}, 200
        if path == "/api/outreach":
            from pipeline import outreach
            return {"records": outreach.overview(self._pdir()), "hunter": bool(outreach.hunter_key(self._pdir())),
                    "apollo": bool(outreach.apollo_key(self._pdir())),
                    "ai": __import__("pipeline.ai.client", fromlist=["status"]).status(self._pdir())[0],
                    "schools": outreach.schools(self._pdir())}, 200
        if path == "/api/leads":
            from pipeline import leads
            store = leads.LeadStore()
            status = q.get("status") or None
            items = store.list(status=None if status == "all" else (status or "new"), limit=300,
                               min_score=float(q.get("min") or 0))
            return {"leads": items, "settings": leads.public_settings(self._pdir()), "counts": store.counts(),
                    "running": leads._RUNNING.locked()}, 200
        if path == "/api/hirebase":
            from pipeline import hirebase
            return hirebase.public(self._pdir()), 200
        if path == "/api/doctor":
            from pipeline import doctor
            return {"checks": doctor.checks(online=q.get("online") == "1", private_dir=self._pdir())}, 200
        if path == "/api/mail":
            from pipeline.notify import mailcodes
            return mailcodes.public(self._pdir()), 200
        m = re.fullmatch(r"/api/sent/([A-Za-z0-9_.:-]+)", path)
        if m:
            return self._sent(m.group(1))
        m = re.fullmatch(r"/api/resume/([A-Za-z0-9_.:-]+)", path)
        if m:
            return self._resume_info(m.group(1)), 200
        m = re.fullmatch(r"/api/prep/([A-Za-z0-9_.:-]+)", path)
        if m:
            from pipeline import interview
            folder = _folder(self._pdir(), m.group(1))
            data = interview.load(folder)
            busy = (folder / "prep_building").exists()
            err = folder / "prep_error.txt"
            return {"exists": bool(data), "building": busy, "prep": data,
                    "last_error": err.read_text()[:600] if err.exists() and not data else "",
                    "ai_ready": ai_client.status(self._pdir())[0]}, 200
        m = re.fullmatch(r"/api/runs/(\d{8}-\d{6})/log", path)
        if m:
            from pipeline.autoapply import Runner
            text, offset = Runner(self._pdir()).log(m.group(1), int(q.get("offset", 0) or 0))
            meta = Runner(self._pdir()).get(m.group(1)) or {}
            return {"text": text, "offset": offset, "status": meta.get("status")}, 200
        return None

    def _leads_post(self, path: str, data: dict):
        import threading
        from pipeline import leads
        pdir = self._pdir()
        store = leads.LeadStore()
        try:
            if path == "/api/leads/settings":
                return leads.save_settings(data, pdir), 200
            if path == "/api/leads/add":
                found = leads.add_shared(str(data.get("text", "")), store=store, private_dir=pdir)
                for lead in found:
                    if lead["score"] >= leads.load_settings(pdir)["alert_min_score"]:
                        b = leads.watch(lead, resolve=leads._resolver())
                        if b:
                            store.update(lead["id"], board=b, status="watching")
                return {"found": found}, 200
            if path == "/api/leads/fetch":
                if leads._RUNNING.locked():
                    return {"started": False, "note": "Already collecting."}, 200
                store.put("last_run", "0")
                from pipeline.notify.telegram import Telegram
                threading.Thread(target=lambda: leads.maybe_run(Telegram(pdir), pdir, store, background=False),
                                 daemon=True).start()
                return {"started": True}, 200
            if path == "/api/leads/action":
                lead = store.lead(str(data.get("id", "")))
                if not lead:
                    return {"error": "no such lead"}, 404
                act = data.get("action")
                if act == "dismiss":
                    store.update(lead["id"], status="dismissed")
                elif act == "restore":
                    store.update(lead["id"], status="new")
                elif act == "watch":
                    b = leads.watch(lead, resolve=leads._resolver())
                    if not b:
                        return {"error": "Couldn't find this company's job board. Try Search companies."}, 400
                    store.update(lead["id"], status="watching", board=b)
                else:
                    return {"error": "unknown action"}, 400
                return store.lead(lead["id"]), 200
        except ValueError as e:
            return {"error": str(e)}, 400
        return {"error": "not found"}, 404

    def _sent(self, job_id: str):
        """Every answer on one application: what was sent, or what to paste
        if you finish it yourself (from a phone, when the dashboard runs in the cloud)."""
        from pipeline.apply.filler import application_url
        pdir = self._pdir()
        job = self._tracker().get_job(job_id)
        if not job:
            return {"error": "no such job"}, 404
        rep = _report(pdir, job_id) or {}
        ai = set(rep.get("ai_written") or [])
        resume = _resume_pdf(_folder(pdir, job_id))
        return {"job": job_card(job, pdir), "application_url": application_url(job) or job.get("url"),
                "submitted": bool(rep.get("submitted")), "submit_note": rep.get("submit_note", ""),
                "prepared_at": rep.get("updated_at"),
                "answers": [{"label": f["label"], "value": f.get("value", ""), "kind": f.get("kind", ""),
                             "ai": f["label"] in ai} for f in rep.get("filled") or [] if f.get("value")],
                "needs": [{"label": f["label"], "kind": f.get("kind", "")} for f in rep.get("needs_attention") or []],
                "screenshot": _file_url(job_id, rep.get("screenshot")),
                "resume_file": _file_url(job_id, str(resume)) if resume else None}, 200

    def _summary(self) -> dict:
        t = self._tracker()
        now = datetime.now()
        hour = (now - timedelta(hours=1)).isoformat(timespec="seconds")
        day = (now - timedelta(hours=24)).isoformat(timespec="seconds")
        new = [e for e in t.events(since=day, limit=2000) if e["kind"] == "new"]
        reach = self._outreach_queue()
        return {"new_last_hour": sum(1 for e in new if e["at"] >= hour), "new_today": len(new),
                "attention": len(self._attention_jobs()) + len(reach["waiting"]) + len(reach["due"]),
                "applied": len(t.by_status(APPLIED)),
                "crawler_running": self._crawler_status().get("running", False)}

    def _feed(self, q: dict) -> dict:
        t = self._tracker()
        hours = q.get("hours", "72")
        max_age = None if hours == "all" else float(hours)
        jobs = t.open_jobs(max_age_hours=max_age, limit=1500)
        pdir = self._pdir()
        cards = [job_card(j, pdir) for j in jobs]
        # Best bets first: odds of an answer, then fit
        cards.sort(key=lambda c: (-(c.get("odds") or {}).get("score", 0), -(c.get("fit_score") or 0)))
        counts = {"open": t.count_open(), "today": t.count_open(24), "shown": len(cards)}
        return {"jobs": cards, "now": datetime.now().isoformat(timespec="seconds"), "counts": counts,
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
        reach = self._outreach_queue()
        return {"items": items, "pending": pending, "ai_ready": ready, "ai_why": why,
                "can_open_browser": can_open_browser(), "outreach": reach, "today": self._today(items, reach),
                "working": self._working()}

    def _working(self) -> list[dict]:
        """Fills in progress, with the step they are on, read from the run
        log: a fill is a fit check, then a resume written for the job (two or
        three minutes), then the form."""
        runner = autoapply.Runner(self._pdir())
        out = []
        for meta in runner.list(limit=6):
            if meta.get("status") != "running":
                continue
            text, _ = runner.log(meta["id"], 0)
            tail = text[-6000:]
            if "📎 resume attached" in tail or "filled" in tail.split("tailored resume")[-1]:
                phase = "filling the form"
            elif "tailored resume" in tail:
                phase = "filling the form"
            elif "fit check" in tail:
                phase = "writing the resume for this job (2-3 minutes)"
            else:
                phase = "checking fit"
            if "needs you" in tail or "Your turn" in tail:
                phase = "waiting for you in the browser" if "Your turn" in tail else "done, saving"
            out.append({"id": meta["id"], "label": meta.get("label", ""), "phase": phase,
                        "started": meta.get("started_at", "")})
        return out

    def _today(self, items: list, reach: dict) -> dict:
        """The day's plan against the targets that produce interviews: ten
        direct applications and three reach-outs a day, follow-ups on time,
        and a prep pack read before every interview."""
        from pipeline import interview, outreach
        pdir = self._pdir()
        t = self._tracker()
        day = t.scorecard(1)
        sent = outreach.stats(pdir, 1)
        interviews = [j for j in t.by_status(("interview",)) if not j.get("closed_at")]
        unread = [{"id": j["id"], "company": j["company"], "title": j["title"], "has_pack": bool(interview.load(_folder(pdir, j["id"])))}
                  for j in interviews]
        from pipeline import autoapply
        auto = autoapply.load_settings(pdir)
        return {"applied": day["applied"], "applied_target": 10,
                "auto_on": bool(auto.get("enabled")), "auto_mode": auto.get("mode", "prepare"),
                "reach_outs": sent["sent"], "reach_target": 3,
                "to_approve": len(items), "followups_due": len(reach["due"]), "to_contact": len(reach["waiting"]),
                "interviews": unread, "window": outreach.send_window()}

    def _outreach_queue(self) -> dict:
        """The human step after each application: a referral or a note to the
        hiring manager turns a ~3% shot into a far better one. Lists applied
        jobs from the last 14 days nobody has been contacted about, and
        follow-ups that are due."""
        from pipeline import outreach
        pdir = self._pdir()
        recs = outreach.load(pdir)
        cutoff = (datetime.now() - timedelta(days=14)).isoformat()
        waiting = []
        for job in self._tracker().by_status(APPLIED):
            if (job.get("applied_at") or "") < cutoff or job.get("closed_at"):
                continue
            rec = recs.get(job["id"]) or {}
            if any(c.get("sent_at") for c in rec.get("contacts") or []):
                continue
            waiting.append({**job_card(job, pdir), "planned": bool(rec.get("plan")),
                            "contacts": len(rec.get("contacts") or [])})
        return {"waiting": waiting[:20], "due": outreach.due_followups(pdir)}

    def _applications(self) -> dict:
        pdir = self._pdir()
        out = []
        for job in self._tracker().by_status(APPLIED):
            rep = _report(pdir, job["id"]) or {}
            folder = _folder(pdir, job["id"])
            resume = _resume_pdf(folder)
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
                "approvals": autoapply.approvals(pdir), "needs_review": autoapply.needs_review(settings, pdir),
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
        if path.startswith("/api/leads"):
            return self._leads_post(path, data)
        if path in ("/api/hirebase", "/api/hirebase/forget"):
            from pipeline import hirebase
            try:
                if path.endswith("forget"):
                    hirebase.forget_key(pdir)
                    return hirebase.public(pdir), 200
                if data.get("key"):
                    hirebase.save_key(str(data["key"]), pdir)
                return hirebase.save_settings(data, pdir), 200
            except ValueError as e:
                return {"error": str(e)}, 400
        if path.startswith("/api/mail"):
            from pipeline.notify import mailcodes
            if path == "/api/mail":
                try:
                    return mailcodes.save(data, pdir), 200
                except ValueError as e:
                    return {"error": str(e)}, 400
            if path == "/api/mail/test":
                ok, msg = mailcodes.test_login(pdir)
                return {"ok": ok, "message": msg}, 200
            if path == "/api/mail/forget":
                mailcodes.forget(pdir)
                return mailcodes.public(pdir), 200
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
            started = False
            if status == "interview" and ai_client.status(self._pdir())[0]:
                # An interview is the moment the prep pack earns its cost: build it now
                started = self._prep_start(m.group(1))
            return {"ok": True, "prep_started": started}, 200
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
        if path == "/api/searches":
            return self._search_save(data)
        if path.startswith("/api/telegram"):
            return self._telegram_post(path, data)
        if path == "/api/career-profile":
            from pipeline.resume import writer as pw
            text = str(data.get("text", ""))
            if len(text) > 200_000:
                return {"error": "That's too long (200,000 characters at most)."}, 400
            pw.save_profile(text, pdir)
            return self._career_profile(), 200
        if path.startswith("/api/outreach"):
            return self._outreach_post(path, data)
        m = re.fullmatch(r"/api/searches/([a-f0-9]{10})/(run|delete)", path)
        if m:
            from pipeline import search as srch
            if m.group(2) == "delete":
                return {"deleted": srch.delete_search(m.group(1), pdir)}, 200
            s = next((x for x in srch.load_searches(pdir) if x["id"] == m.group(1)), None)
            if not s:
                return {"error": "no such search"}, 404
            _start_search(s, pdir)
            return {"started": True}, 200
        m = re.fullmatch(r"/api/resume/([A-Za-z0-9_.:-]+)/build", path)
        if m:
            return self._resume_build(m.group(1), bool(data.get("reword", True)),
                                      str(data.get("description") or "")[:30000])
        m = re.fullmatch(r"/api/prep/([A-Za-z0-9_.:-]+)/(build|thanks)", path)
        if m:
            if not ai_client.status(self._pdir())[0]:
                return {"error": "Interview prep needs your Anthropic key (My details, AI)."}, 400
            if not self._tracker().get_job(m.group(1)):
                return {"error": "no such job"}, 404
            if m.group(2) == "build":
                if not self._prep_start(m.group(1), str(data.get("notes") or "")[:3000]):
                    return {"error": "A pack is already being written for this job."}, 409
                return {"started": True}, 200
            from pipeline import interview
            try:
                note = interview.thank_you(self._tracker().get_job(m.group(1)), _folder(self._pdir(), m.group(1)),
                                           str(data.get("interviewer") or "")[:80], str(data.get("notes") or "")[:2000],
                                           self._pdir())
            except (ai_client.AIUnavailable, ValueError, RuntimeError) as e:
                return {"error": str(e)}, 400
            return note, 200
        return None

    def _prep_start(self, job_id: str, notes: str = "") -> bool:
        """Write the interview pack in the background (research + pack take a
        few minutes). False when one is already being written."""
        import threading
        from pipeline import interview
        pdir = self._pdir()
        folder = _folder(pdir, job_id)
        flag = folder / "prep_building"
        if flag.exists() and (datetime.now().timestamp() - flag.stat().st_mtime) < 900:
            return False
        folder.mkdir(parents=True, exist_ok=True)
        flag.write_text(datetime.now().isoformat())
        job = self._tracker().get_job(job_id)

        def run():
            try:
                interview.prep(job, folder, pdir, notes=notes)
                (folder / "prep_error.txt").unlink(missing_ok=True)
            except Exception as e:
                logging.getLogger(__name__).warning("interview prep failed for %s: %s", job_id, e)
                (folder / "prep_error.txt").write_text(f"{type(e).__name__}: {e}"[:600])
            finally:
                flag.unlink(missing_ok=True)
        threading.Thread(target=run, daemon=True, name=f"prep-{job_id}").start()
        return True

    # ── Search ─────────────────────────────────────────────────────────

    def _searches(self) -> dict:
        from pipeline import search as srch
        pdir, t = self._pdir(), self._tracker()
        out = []
        for s in srch.load_searches(pdir):
            res = []
            for company, r in (s.get("results") or {}).items():
                jobs = [j for j in (t.get_job(i) for i in r.get("jobs", [])) if j and not j.get("closed_at")]
                jobs.sort(key=lambda j: -(j.get("fit_score") or 0))
                res.append({"company": company, "boards": r.get("boards", []), "error": r.get("error", ""),
                            "jobs": [job_card(j, pdir) for j in jobs[:40]]})
            out.append({**{k: s.get(k) for k in ("id", "companies", "roles", "anywhere", "watch", "last_run")},
                        "running": _SEARCH_PROGRESS.get(s["id"]), "results": res})
        return {"searches": out}

    def _search_save(self, data: dict):
        from pipeline import search as srch
        pdir = self._pdir()
        companies = srch.parse_list(data.get("companies", ""))
        if not companies:
            return {"error": "Add at least one company."}, 400
        s = srch.upsert_search(companies, data.get("roles", ""), anywhere=bool(data.get("anywhere")),
                               watch=bool(data.get("watch", True)), search_id=data.get("id"), private_dir=pdir)
        _start_search(s, pdir)
        return {"search": s}, 200

    # ── Telegram ───────────────────────────────────────────────────────

    def _telegram_info(self) -> dict:
        from pipeline.notify.telegram import Telegram
        s = Telegram(self._pdir()).settings()
        return {**{k: v for k, v in s.items() if k != "token"}, "has_token": bool(s["token"]),
                "connected": bool(s["token"] and s["chat_id"])}

    def _telegram_post(self, path: str, data: dict):
        from pipeline.notify.telegram import Telegram
        tg = Telegram(self._pdir())
        try:
            if path == "/api/telegram/connect":
                bot = tg.connect(str(data.get("token", "")))
                return {"bot": bot}, 200
            if path == "/api/telegram/wait":
                chat = tg.wait_for_start(25)
                return {"connected": bool(chat)}, 200
            if path == "/api/telegram/test":
                return {"sent": bool(tg.send("👋 Test from jobpipe. Alerts are working."))}, 200
            if path == "/api/telegram":
                tg.save_settings({k: v for k, v in data.items() if k not in ("token", "chat_id", "bot")})
                return self._telegram_info(), 200
        except RuntimeError as e:
            return {"error": f"Telegram said: {e}"}, 400
        return {"error": "unknown"}, 404

    # ── Resumes ────────────────────────────────────────────────────────

    def _career_profile(self) -> dict:
        from pipeline.resume import writer as pw
        text = pw.load_profile(self._pdir())
        secs = pw.sections(text) if text else {}
        return {"text": text, "ready": pw.has_profile(self._pdir()),
                "confirm": len(re.findall(r"\[confirm(?! \.\.\.)", text, re.I)),
                "roles": [h for h in secs if not h.startswith("project:") and h not in
                          ("_top", "Rules", "Experience", "Projects", "Skills by level", "Education")][:20],
                "never_claim": pw.never_claim(text) if text else [],
                "has_reference": (self._pdir() / pw.REFERENCE_FILE).exists(),
                "resume_model": __import__("pipeline.ai.client", fromlist=["x"]).load_settings(self._pdir())
                .get("resume_model", "opus")}

    def _outreach_post(self, path: str, data: dict):
        from pipeline import outreach
        from pipeline.ai import client as ai_client
        pdir = self._pdir()
        if path == "/api/outreach/hunter-key":
            try:
                outreach.save_hunter_key(str(data.get("key", "")), pdir)
            except ValueError as e:
                return {"error": str(e)}, 400
            return {"hunter": bool(outreach.hunter_key(pdir))}, 200
        if path == "/api/outreach/apollo-key":
            try:
                outreach.save_apollo_key(str(data.get("key", "")), pdir)
            except ValueError as e:
                return {"error": str(e)}, 400
            return {"apollo": bool(outreach.apollo_key(pdir))}, 200
        m = re.fullmatch(r"/api/outreach/([A-Za-z0-9_.:-]+)/(plan|find|accept|contacts|domain)", path)
        m2 = re.fullmatch(r"/api/outreach/([A-Za-z0-9_.:-]+)/contacts/([a-f0-9]{8})/(\w+)", path)
        job_id = (m or m2).group(1) if (m or m2) else None
        job = self._tracker().get_job(job_id) if job_id else None
        if not job:
            return {"error": "no such job"}, 404
        try:
            if m and m.group(2) == "plan":
                return outreach.plan(job, pdir), 200
            if m and m.group(2) == "find":
                if not ai_client.status(pdir)[0]:
                    return {"error": "Finding people needs your Anthropic key (My details, AI)."}, 400
                return outreach.find_people(job, pdir), 200
            if m and m.group(2) == "accept":
                return outreach.accept_candidate(job, int(data.get("index", -1)), pdir), 200
            if m and m.group(2) == "contacts":
                return outreach.add_contact(job, data, pdir), 200
            if m and m.group(2) == "domain":
                rec = outreach.record(job, pdir)
                rec["domain"] = outreach.clean_domain(str(data.get("domain", "")))
                return outreach._put(rec, pdir), 200
            cid, action = m2.group(2), m2.group(3)
            if action == "email":
                return outreach.find_email(job, cid, pdir, domain=data.get("domain")), 200
            if action == "draft":
                if not ai_client.status(pdir)[0]:
                    return {"error": "Drafts need your Anthropic key (My details, AI)."}, 400
                return outreach.draft(job, cid, pdir), 200
            return outreach.update_contact(job, cid, action, pdir, data), 200
        except KeyError as e:
            return {"error": str(e).strip("'")}, 404
        except ValueError as e:
            return {"error": str(e)}, 400
        except (ai_client.AIUnavailable, RuntimeError) as e:
            return {"error": str(e)}, 400

    def _resume_info(self, job_id: str) -> dict:
        folder = _folder(self._pdir(), job_id)
        info = folder / "tailor.json"
        data = json.loads(info.read_text()) if info.exists() else {}
        pdf = _resume_pdf(folder, data)
        failed = folder / "last_error.txt"
        last_error = failed.read_text()[:600] if failed.exists() and not pdf else ""
        return {"exists": bool(pdf), "file": _file_url(job_id, str(pdf)) if pdf else None,
                "last_error": last_error, **data}

    def _resume_build(self, job_id: str, reword: bool, description: str = ""):
        from pipeline import applicant
        from pipeline.resume.tailor import tailored_resume
        from pipeline.utils.config import load_config
        job = self._tracker().get_job(job_id)
        if not job:
            return {"error": "no such job"}, 404
        folder = _folder(self._pdir(), job_id)
        pasted = folder / "job_description.txt"       # a description you pasted, kept for rebuilds
        if description.strip():
            folder.mkdir(parents=True, exist_ok=True)
            pasted.write_text(description.strip())
        if pasted.exists() and len(pasted.read_text().split()) > len((job.get("description") or "").split()):
            job = {**job, "description": pasted.read_text()}
        for old in list(folder.glob("*Resume.pdf")) + [folder / "tailor.json", folder / "last_error.txt"] \
                if folder.exists() else []:
            old.unlink(missing_ok=True)
        import logging
        import time as _time
        t0 = _time.time()
        logging.getLogger(__name__).info(f"Building a resume for {job.get('company')} — {job.get('title')}")
        try:
            pdf, note = tailored_resume(job, load_config(None), applicant.load(self._pdir()), folder,
                                        private_dir=self._pdir(), rewrite=reword)
        except Exception as e:             # say what broke rather than a bare 500
            logging.getLogger(__name__).exception("resume build crashed")
            pdf, note = None, f"{type(e).__name__}: {e}"
        if not pdf:
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "last_error.txt").write_text(str(note))
        logging.getLogger(__name__).info(f"Resume for {job.get('company')}: "
                                         f"{'built' if pdf else 'failed'} in {_time.time() - t0:.0f}s ({note})")
        if not pdf:
            return {"error": f"The resume didn't build: {note}"}, 400
        return self._resume_info(job_id), 200

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


# Searches run in a background thread; progress messages by search id
_SEARCH_PROGRESS: dict[str, str] = {}


def _start_search(search: dict, pdir: Path) -> None:
    import asyncio
    import threading
    from pipeline.search import run_search
    from pipeline.utils.config import load_config
    if _SEARCH_PROGRESS.get(search["id"]):
        return
    _SEARCH_PROGRESS[search["id"]] = "Starting…"

    def work() -> None:
        try:
            asyncio.run(run_search(search, load_config(None), private_dir=pdir,
                                   progress=lambda m: _SEARCH_PROGRESS.__setitem__(search["id"], m)))
        except Exception as e:
            import logging
            logging.getLogger(__name__).exception(f"search failed: {e}")
        finally:
            _SEARCH_PROGRESS.pop(search["id"], None)

    threading.Thread(target=work, name=f"search-{search['id']}", daemon=True).start()
