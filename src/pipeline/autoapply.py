"""Auto-apply: settings, picking jobs, and running `jobpipe apply` in the
background (from the dashboard or the crawler).

Settings (data/private/autoapply.json):
  enabled           the crawler applies on its own as fresh jobs arrive
  mode              "prepare" — fill each form (resume, saved + AI answers),
                    record what's missing, send nothing; the job then waits
                    under Needs you, ready to open and finish.
                    "submit" — also submit forms that are complete
                    (never consent boxes; CAPTCHAs stop it).
  min_score         only jobs scoring at least this
  fresh_hours       only jobs that appeared within this many hours
  max_per_day       daily cap on auto attempts
  max_per_company   per company per day
  trust_ai          submit forms containing AI-written answers
  fit_check/tailor  AI fit check first / tailored resume per job
  craft             also reword resume bullets toward each job (fact-guarded)
  review_first      in submit mode, the first N complete applications come to
                    Telegram with every answer for a Submit/Skip tap; after N
                    approvals it submits on its own (0 = never ask, 1000 = always)
  never_companies   companies auto-apply must never touch
  repost_days       don't re-apply to the same company + role within this many days

Runs are subprocesses (`python -m pipeline.cli apply …`), one at a time,
logged to data/private/runs/<id>.log.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "enabled": False, "mode": "prepare", "min_score": 65, "fresh_hours": 24,
    "max_per_day": 15, "max_per_company": 2, "trust_ai": False,
    "fit_check": True, "tailor": True, "craft": True, "batch": 3,
    "review_first": 15, "never_companies": [], "repost_days": 45,
}
JOB_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")
TERMINAL = ("applied", "interview", "offer", "rejected", "skipped")


def _pdir(private_dir: Path | None = None) -> Path:
    from pipeline import applicant
    return private_dir or applicant.PRIVATE_DIR


def load_settings(private_dir: Path | None = None) -> dict[str, Any]:
    path = _pdir(private_dir) / "autoapply.json"
    data = dict(DEFAULTS)
    if path.exists():
        try:
            data.update(json.loads(path.read_text()))
        except ValueError:
            pass
    return data


def save_settings(values: dict[str, Any], private_dir: Path | None = None) -> dict[str, Any]:
    data = load_settings(private_dir)
    for key, default in DEFAULTS.items():
        if key not in values:
            continue
        v = values[key]
        if isinstance(default, bool):
            data[key] = bool(v)
        elif isinstance(default, int):
            data[key] = max(0, min(int(float(v)), 1000))
        elif key == "mode":
            if v not in ("prepare", "submit"):
                raise ValueError("mode must be prepare or submit")
            data[key] = v
        elif isinstance(default, list):
            items = v if isinstance(v, list) else re.split(r"[\n,]", str(v))
            data[key] = sorted({" ".join(str(i).split())[:80] for i in items if str(i).strip()})[:300]
    path = _pdir(private_dir) / "autoapply.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    os.chmod(path, 0o600)
    return data


def attempted(job_id: str, private_dir: Path | None = None) -> bool:
    folder = _pdir(private_dir) / "applications" / re.sub(r"[^A-Za-z0-9_.-]", "_", job_id)
    return (folder / "report.json").exists()


MAX_ATTEMPTS = 3


# ── Phone approvals before it runs on its own ──────────────────────────

def approvals(private_dir: Path | None = None) -> int:
    try:
        return int(json.loads((_pdir(private_dir) / "approvals.json").read_text()).get("count", 0))
    except (OSError, ValueError, AttributeError):
        return 0


def needs_review(settings: dict[str, Any], private_dir: Path | None = None) -> bool:
    """Submit mode, but still in the first N that you approve on your phone."""
    return settings.get("mode") == "submit" and approvals(private_dir) < int(settings.get("review_first", 0))


def approve(job: dict, private_dir: Path | None = None) -> int:
    """You tapped Submit on a prepared application: keep the exact answers you
    saw (Claude's included) for this company, queue the submit, count it."""
    from pipeline import applicant
    try:
        rep = json.loads((_folder(job["id"], private_dir) / "report.json").read_text())
    except (OSError, ValueError):
        rep = {}
    ai = set(rep.get("ai_written") or [])
    keep = [{"question": applicant.normalize_question(f["label"], job["company"]), "answer": f["value"],
             "company": job["company"], "everywhere": False}
            for f in rep.get("filled") or [] if f.get("label") in ai and f.get("value")]
    if keep:
        applicant.save_answers(keep, private_dir)
    p = _pdir(private_dir) / "approvals.json"
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        data = {"count": 0, "jobs": []}
    if job["id"] not in data.get("jobs", []):
        data["count"] = int(data.get("count", 0)) + 1
        data["jobs"] = (data.get("jobs", []) + [job["id"]])[-500:]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data))
    enqueue(job["id"], "submit", private_dir)
    return data["count"]


def _norm_company(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", re.sub(r"\b(inc|llc|ltd|corp|co|technologies|labs)\b\.?", "",
                                             (name or "").lower()))


def excluded(job: dict, settings: dict[str, Any]) -> bool:
    c = _norm_company(job.get("company", ""))
    return any(n and (n == c or (len(n) >= 4 and n in c)) for n in map(_norm_company, settings.get("never_companies") or []))


def recently_applied(tracker, days: int) -> set[tuple[str, str]]:
    """(company, role) pairs you applied to in the last `days` days, so a
    reposted or duplicated opening isn't sent twice."""
    from pipeline.orchestrator import _title_key
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    out = set()
    for j in tracker.by_status(("applied", "interview", "offer", "rejected")):
        when = j.get("applied_at") or j.get("last_seen") or ""
        if not when or when >= cutoff:
            out.add((_norm_company(j["company"]), _title_key(j["title"])))
    return out


def _folder(job_id: str, private_dir: Path | None) -> Path:
    return _pdir(private_dir) / "applications" / re.sub(r"[^A-Za-z0-9_.-]", "_", job_id)


def attempts(job_id: str, private_dir: Path | None = None) -> int:
    try:
        return int((_folder(job_id, private_dir) / "attempts").read_text())
    except (OSError, ValueError):
        return 1 if attempted(job_id, private_dir) else 0


def count_attempt(job_id: str, private_dir: Path | None = None) -> None:
    f = _folder(job_id, private_dir)
    f.mkdir(parents=True, exist_ok=True)
    (f / "attempts").write_text(str(attempts(job_id, private_dir) + 1))


def worth_retrying(job: dict, settings: dict[str, Any], private_dir: Path | None = None) -> bool:
    """A job already tried once comes back when trying again can now succeed:
    every question it stopped on has an answer (you replied on your phone or
    on Needs you), or it was prepared complete and auto-apply now submits."""
    from pipeline import applicant
    if attempts(job["id"], private_dir) >= MAX_ATTEMPTS:
        return False
    try:
        rep = json.loads((_folder(job["id"], private_dir) / "report.json").read_text())
    except (OSError, ValueError):
        return False
    if rep.get("submitted") or rep.get("closed") or rep.get("error"):
        return False
    if rep.get("mode") == "practice" and rep.get("ready_to_submit"):
        # During the approval period it waits for your tap instead of being redone
        return settings["mode"] == "submit" and not needs_review(settings, private_dir)
    needs = rep.get("needs_attention") or []
    if not needs:
        return False
    saved = applicant.load_answers(private_dir)
    company = job["company"]
    for n in needs:
        if n.get("kind") in ("file", "checkbox") or "consent" in (n.get("source") or ""):
            return False                      # only you can do these
        q = applicant.normalize_question(n["label"], company)
        if not applicant.lookup_answer(saved, q, company):
            return False
    return True


def candidates(tracker, settings: dict[str, Any], private_dir: Path | None = None) -> list[dict]:
    """Fresh, open, well-scoring jobs — new ones, plus earlier tries that can
    now succeed — best first, one per application."""
    from pipeline.ai.fitcheck import stored
    from pipeline.apply.filler import application_url
    from pipeline.orchestrator import _title_key
    jobs = tracker.open_jobs(max_age_hours=settings["fresh_hours"])
    done = recently_applied(tracker, int(settings.get("repost_days", 45)))
    out = []
    for j in jobs:
        if j["status"] in TERMINAL or j["fit_score"] < settings["min_score"]:
            continue
        if excluded(j, settings) or (_norm_company(j["company"]), _title_key(j["title"])) in done:
            continue
        if not application_url(j):
            continue
        if attempted(j["id"], private_dir) and not worth_retrying(j, settings, private_dir):
            continue
        fit = stored(j)
        if fit and fit.get("verdict") == "skip":
            continue
        out.append(j)
    # Newest first, in six-hour bands, best fit within a band: an application in
    # the first hours of a posting is worth several a day later (LinkedIn,
    # TalentWorks data), and everything here already clears the fit bar.
    now = datetime.now()

    def band(j):
        at = j.get("first_seen_at") or j.get("posted_at") or ""
        try:
            hours = (now - datetime.fromisoformat(str(at)[:19])).total_seconds() / 3600
        except ValueError:
            hours = 24.0
        return int(max(hours, 0) // 6)
    # Interview odds first (age, company size, level, sponsorship), then fit:
    # the day's allowance goes where an application is most likely to be read
    from pipeline.odds import odds
    from pipeline.filters.sponsorship import check_jd_blocks_sponsorship
    try:
        from pipeline.utils.config import load_config
        years = float((load_config().get("profile") or {}).get("years_experience") or 3)
    except Exception:
        years = 3.0
    out.sort(key=lambda j: (-odds({**j, "jd_blocked": check_jd_blocks_sponsorship(j.get("description") or "")},
                                  years=years, now=now)["score"], band(j), -j["fit_score"]))
    from pipeline.cli import _one_per_application
    return _one_per_application(out)


def today_attempts(private_dir: Path | None = None) -> dict[str, int]:
    """Auto attempts per company today (from run records)."""
    counts: dict[str, int] = {}
    today = datetime.now().strftime("%Y-%m-%d")
    for meta in Runner(private_dir).list(limit=200):
        if meta.get("auto") and meta.get("started_at", "").startswith(today):
            for c in meta.get("companies", []):
                counts[c] = counts.get(c, 0) + 1
    return counts


# ── Background runs ────────────────────────────────────────────────────

class Runner:
    _procs: dict[str, subprocess.Popen] = {}

    def __init__(self, private_dir: Path | None = None) -> None:
        self.dir = _pdir(private_dir) / "runs"

    def _meta_path(self, run_id: str) -> Path:
        return self.dir / f"{run_id}.json"

    def start(self, args: list[str], label: str, auto: bool = False,
              companies: list[str] | None = None, env: dict | None = None) -> dict:
        if self.busy():
            raise RuntimeError("Another apply run is still going — wait for it or stop it.")
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        log = open(self.dir / f"{run_id}.log", "w")
        cmd = [sys.executable, "-m", "pipeline.cli", *args]
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                start_new_session=True,
                                env={**os.environ, "COLUMNS": "110", "PYTHONUNBUFFERED": "1", **(env or {})})
        Runner._procs[run_id] = proc
        meta = {"id": run_id, "label": label, "args": args, "pid": proc.pid, "auto": auto,
                "companies": companies or [], "status": "running",
                "started_at": datetime.now().isoformat(timespec="seconds")}
        self._write(meta)
        return meta

    def _write(self, meta: dict) -> None:
        self._meta_path(meta["id"]).write_text(json.dumps(meta, indent=1))

    def _refresh(self, meta: dict) -> dict:
        if meta.get("status") != "running":
            return meta
        proc = Runner._procs.get(meta["id"])
        code = None
        if proc is not None:
            code = proc.poll()
            alive = code is None
        else:
            try:
                os.kill(meta["pid"], 0)
                alive = True
            except OSError:
                alive = False
        if not alive:
            meta["status"] = "finished" if code in (0, None) else "failed"
            meta["exit_code"] = code
            meta["finished_at"] = datetime.now().isoformat(timespec="seconds")
            self._write(meta)
        return meta

    def list(self, limit: int = 20) -> list[dict]:
        if not self.dir.exists():
            return []
        metas = []
        for p in sorted(self.dir.glob("*.json"), reverse=True)[:limit]:
            try:
                metas.append(self._refresh(json.loads(p.read_text())))
            except (OSError, ValueError):
                continue
        return metas

    def get(self, run_id: str) -> dict | None:
        if not re.fullmatch(r"\d{8}-\d{6}", run_id or ""):
            return None
        p = self._meta_path(run_id)
        return self._refresh(json.loads(p.read_text())) if p.exists() else None

    def log(self, run_id: str, offset: int = 0) -> tuple[str, int]:
        if not self.get(run_id):
            return "", 0
        p = self.dir / f"{run_id}.log"
        with p.open("rb") as f:
            f.seek(max(0, offset))
            data = f.read(200_000)
        return data.decode("utf-8", "replace"), offset + len(data)

    def busy(self) -> bool:
        return any(m.get("status") == "running" for m in self.list(limit=5))

    def stop(self, run_id: str) -> bool:
        meta = self.get(run_id)
        if not meta or meta.get("status") != "running":
            return False
        try:
            os.killpg(meta["pid"], signal.SIGTERM)
        except OSError:
            return False
        time.sleep(0.5)
        meta["status"] = "stopped"
        meta["finished_at"] = datetime.now().isoformat(timespec="seconds")
        self._write(meta)
        return True


def apply_args(job_ids: list[str] | None = None, *, top: int = 5, min_score: float = 70,
               mode: str = "practice", headed: bool = False, trust_ai: bool = False,
               fit: bool = True, tailor: bool = True) -> list[str]:
    """Command-line arguments for one apply run. mode: practice | submit | handoff."""
    args = ["apply"]
    for j in job_ids or []:
        if not JOB_ID.match(j):
            raise ValueError(f"bad job id: {j!r}")
        args += ["-j", j]
    if not job_ids:
        args += ["--top", str(int(top)), "--min-score", str(float(min_score))]
    if mode == "submit":
        args += ["--submit", "--yes"]
        if trust_ai:
            args.append("--trust-ai")
    elif mode == "handoff":
        args.append("--handoff")
    elif mode != "practice":
        raise ValueError("mode must be practice, submit or handoff")
    if headed or mode == "handoff":
        args.append("--headed")
    if not fit:
        args.append("--no-fit")
    if not tailor:
        args.append("--no-tailor")
    return args


# Jobs you asked for from your phone (Submit it / Prepare), run before anything else
def enqueue(job_id: str, mode: str, private_dir: Path | None = None) -> None:
    if not JOB_ID.match(job_id) or mode not in ("submit", "practice"):
        raise ValueError("bad queue entry")
    p = _pdir(private_dir) / "apply_queue.json"
    try:
        q = json.loads(p.read_text())
    except (OSError, ValueError):
        q = []
    q = [x for x in q if x["job_id"] != job_id] + [{"job_id": job_id, "mode": mode}]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(q))


def _take_queue(private_dir: Path | None) -> list[dict]:
    p = _pdir(private_dir) / "apply_queue.json"
    try:
        q = json.loads(p.read_text())
    except (OSError, ValueError):
        return []
    p.write_text("[]")
    return q


def maybe_autoapply(tracker, private_dir: Path | None = None, runner: Runner | None = None,
                    allow_auto: bool = True) -> dict | None:
    """Called by the crawler after each tick: run what you queued from your
    phone first; otherwise start one background batch if auto-apply is on,
    nothing is running, and today's caps allow it."""
    settings = load_settings(private_dir)
    runner = runner or Runner(private_dir)
    if runner.busy():
        return None
    queued = _take_queue(private_dir)
    if queued:
        mode = "submit" if any(q["mode"] == "submit" for q in queued) else "practice"
        ids = [q["job_id"] for q in queued if q["mode"] == mode]
        rest = [q for q in queued if q["mode"] != mode]
        for q in rest:                                   # the other mode waits for the next pass
            enqueue(q["job_id"], q["mode"], private_dir)
        jobs = [tracker.get_job(i) for i in ids]
        companies = [j["company"] for j in jobs if j]
        args = apply_args(ids, mode=mode, trust_ai=settings["trust_ai"], fit=False, tailor=settings["tailor"])
        return runner.start(args, ("From your phone — submit: " if mode == "submit" else "From your phone — prepare: ")
                            + ", ".join(companies), auto=False, companies=companies)
    if not settings["enabled"] or not allow_auto:
        return None
    done_today = today_attempts(private_dir)
    room = settings["max_per_day"] - sum(done_today.values())
    if room <= 0:
        return None
    picked: list[dict] = []
    per_company = dict(done_today)
    for j in candidates(tracker, settings, private_dir):
        c = j["company"]
        if per_company.get(c, 0) >= settings["max_per_company"]:
            continue
        picked.append(j)
        per_company[c] = per_company.get(c, 0) + 1
        if len(picked) >= min(room, settings["batch"]):
            break
    if not picked:
        return None
    review = needs_review(settings, private_dir)
    mode = "submit" if settings["mode"] == "submit" and not review else "practice"
    for j in picked:
        count_attempt(j["id"], private_dir)
    args = apply_args([j["id"] for j in picked], mode=mode, trust_ai=settings["trust_ai"],
                      fit=settings["fit_check"], tailor=settings["tailor"])
    if review:
        args.append("--notify-ready")
    label = ("Auto-submit: " if mode == "submit" else
             "Prepared for your OK: " if review else "Auto-prepare: ") + ", ".join(j["company"] for j in picked)
    return runner.start(args, label, auto=True, companies=[j["company"] for j in picked])
