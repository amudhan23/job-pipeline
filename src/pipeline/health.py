"""Is everything working? Checked after every crawler pass; problems go to
Telegram (each at most every 6 hours, plus a note when it's fixed), and the
cloud process restarts itself if the crawler stops."""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

REPEAT_HOURS = 6


def _pdir(private_dir: Path | None) -> Path:
    from pipeline import applicant
    return private_dir or applicant.PRIVATE_DIR


def problems(private_dir: Path | None = None, status: dict | None = None,
             now: datetime | None = None) -> list[dict[str, str]]:
    """Each problem: key (stable id), what (plain words), fix (what to do)."""
    from pipeline import autoapply
    from pipeline.ai import client as ai_client
    from pipeline.crawler.worker import parse_iso, read_status, utcnow
    out: list[dict[str, str]] = []
    st = status if status is not None else read_status()
    pdir = _pdir(private_dir)

    upd = parse_iso(st.get("updated_at")) if st.get("updated_at") else None
    if upd and (utcnow() - upd).total_seconds() > 20 * 60:
        out.append({"key": "crawler_stalled", "what": "The crawler hasn't finished a pass in over 20 minutes.",
                    "fix": "It restarts itself in the cloud; on your computer, restart `jobpipe watch`."})
    errs = st.get("errors") or []
    reads = (st.get("last_hour") or {}).get("reads", 0)
    bad = sum((st.get("last_hour") or {}).get("outcomes", {}).get(k, 0) for k in ("error", "blocked", "timeout"))
    if reads >= 20 and bad / max(reads, 1) > 0.5:
        out.append({"key": "fetch_errors", "what": f"{bad} of {reads} board reads failed in the last hour.",
                    "fix": "Usually the network or a site blocking the server; latest: "
                           + (errs[0].get("error", "")[:80] if errs else "unknown")})

    settings = autoapply.load_settings(pdir)
    runs = [m for m in autoapply.Runner(pdir).list(limit=10) if m.get("auto")]
    if len(runs) >= 3 and all(m.get("status") == "failed" for m in runs[:3]):
        out.append({"key": "apply_failing", "what": "The last 3 auto-apply runs crashed.",
                    "fix": "Open Auto-apply on the dashboard and read the latest run's log."})
    if settings.get("enabled") and (settings.get("fit_check") or settings.get("tailor")):
        ok, why = ai_client.status(pdir)
        if not ok and ai_client.get_key(pdir):
            out.append({"key": "ai_down", "what": f"AI is off: {why}",
                        "fix": "Raise the monthly budget or check the key on My details → AI."})
    if settings.get("enabled") and autoapply.needs_review(settings, pdir):
        from pipeline.notify.telegram import Telegram
        if not Telegram(pdir).configured():
            out.append({"key": "review_no_phone",
                        "what": "Auto-apply waits for your OK on Telegram, but Telegram isn't connected.",
                        "fix": "Connect Telegram (`jobpipe telegram setup`), or approve on Needs you."})
    try:
        free = shutil.disk_usage(pdir.parent if pdir.parent.exists() else Path(".")).free
        if free < 300 * 1024 * 1024:
            out.append({"key": "disk_low", "what": f"Only {free // (1024 * 1024)} MB of disk left.",
                        "fix": "Old screenshots live in data/private/applications; or grow the volume."})
    except OSError:
        pass
    return out


def alert(found: list[dict[str, str]], tg: Any, private_dir: Path | None = None,
          now: datetime | None = None) -> list[str]:
    """Send new or still-open problems (at most every 6 h each) and fixes. Returns keys sent."""
    now = now or datetime.now()
    p = _pdir(private_dir) / "health_state.json"
    try:
        state = json.loads(p.read_text())
    except (OSError, ValueError):
        state = {}
    sent, current = [], {f["key"] for f in found}
    if tg is None or not tg.configured():
        return sent
    for f in found:
        last = state.get(f["key"])
        if last and now - datetime.fromisoformat(last) < timedelta(hours=REPEAT_HOURS):
            continue
        if tg.send(f"⚠️ <b>jobpipe needs a look</b>\n{f['what']}\n<i>{f['fix']}</i>"):
            state[f["key"]] = now.isoformat(timespec="seconds")
            sent.append(f["key"])
    for key in [k for k in state if k not in current]:
        tg.send(f"✅ Fixed: {key.replace('_', ' ')}.")
        del state[key]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state))
    os.chmod(p, 0o600)
    return sent
