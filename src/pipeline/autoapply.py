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
    "fit_check": True, "tailor": True, "batch": 3,
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
    path = _pdir(private_dir) / "autoapply.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    os.chmod(path, 0o600)
    return data


def attempted(job_id: str, private_dir: Path | None = None) -> bool:
    folder = _pdir(private_dir) / "applications" / re.sub(r"[^A-Za-z0-9_.-]", "_", job_id)
    return (folder / "report.json").exists()


def candidates(tracker, settings: dict[str, Any], private_dir: Path | None = None) -> list[dict]:
    """Fresh, open, well-scoring jobs not yet tried, best first, one per application."""
    from pipeline.ai.fitcheck import stored
    from pipeline.apply.filler import application_url
    jobs = tracker.open_jobs(max_age_hours=settings["fresh_hours"])
    out = []
    for j in jobs:
        if j["status"] in TERMINAL or j["fit_score"] < settings["min_score"]:
            continue
        if not application_url(j) or attempted(j["id"], private_dir):
            continue
        fit = stored(j)
        if fit and fit.get("verdict") == "skip":
            continue
        out.append(j)
    out.sort(key=lambda j: j["fit_score"], reverse=True)
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


def maybe_autoapply(tracker, private_dir: Path | None = None, runner: Runner | None = None) -> dict | None:
    """Called by the crawler after each tick: start one background batch if
    auto-apply is on, nothing is running, and today's caps allow it."""
    settings = load_settings(private_dir)
    if not settings["enabled"]:
        return None
    runner = runner or Runner(private_dir)
    if runner.busy():
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
    mode = "submit" if settings["mode"] == "submit" else "practice"
    args = apply_args([j["id"] for j in picked], mode=mode, trust_ai=settings["trust_ai"],
                      fit=settings["fit_check"], tailor=settings["tailor"])
    label = ("Auto-submit: " if mode == "submit" else "Auto-prepare: ") + \
        ", ".join(j["company"] for j in picked)
    return runner.start(args, label, auto=True, companies=[j["company"] for j in picked])
