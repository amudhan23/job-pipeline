"""Telegram: alerts on your phone, and answers back from it.

Setup: make a bot with @BotFather (it gives you a token), then
`jobpipe telegram setup` (or the Auto-apply page) and send /start to the bot.

What you get:
  - a message the minute a matching job opens, with buttons:
      Submit it · Prepare · Skip · Open posting
  - "Submitted" messages (with what Claude wrote), and "stopped" ones
  - each question a form needs: tap an option, or reply with your answer —
    it's saved for that company and the application is retried
  - when a site emails a security code during an unattended submit: reply
    with the code and the bot types it in
  - a morning summary; /status, /pause, /resume commands

Only messages from your own chat are obeyed. Settings and state live in
data/private/ (telegram.json, telegram_state.json).
"""

from __future__ import annotations

import fcntl
import html
import json
import os
import secrets
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import httpx

API = "https://api.telegram.org"
DEFAULTS: dict[str, Any] = {"token": "", "chat_id": None, "bot": "", "enabled": True, "min_score": 70,
                            "new_jobs": True, "applied": True, "needs": True, "summary_hour": 8}
STATE_DEFAULTS: dict[str, Any] = {"offset": 0, "tokens": {}, "replies": {}, "asks": {},
                                  "sent_questions": [], "last_summary": ""}


def _pdir(private_dir: Path | None) -> Path:
    from pipeline import applicant
    return private_dir or applicant.PRIVATE_DIR


class Telegram:
    def __init__(self, private_dir: Path | None = None, client: httpx.Client | None = None) -> None:
        self.dir = _pdir(private_dir)
        self.client = client or httpx.Client(timeout=30)

    # ── Settings & state (shared by the crawler and apply runs) ────────

    @property
    def _settings_path(self) -> Path:
        return self.dir / "telegram.json"

    def settings(self) -> dict[str, Any]:
        data = dict(DEFAULTS)
        try:
            data.update(json.loads(self._settings_path.read_text()))
        except (OSError, ValueError):
            pass
        return data

    def save_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        data = self.settings()
        for k, default in DEFAULTS.items():
            if k not in values:
                continue
            v = values[k]
            if isinstance(default, bool):
                data[k] = bool(v)
            elif k in ("min_score", "summary_hour"):
                data[k] = max(0, min(int(v), 100 if k == "min_score" else 23))
            else:
                data[k] = v
        self.dir.mkdir(parents=True, exist_ok=True)
        self._settings_path.write_text(json.dumps(data, indent=2))
        os.chmod(self._settings_path, 0o600)
        return data

    def configured(self) -> bool:
        s = self.settings()
        return bool(s["token"] and s["chat_id"] and s["enabled"])

    @contextmanager
    def state(self):
        """Read-modify-write the shared state under a file lock."""
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / "telegram_state.json"
        with open(self.dir / ".telegram.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = dict(STATE_DEFAULTS)
            try:
                data.update(json.loads(path.read_text()))
            except (OSError, ValueError):
                pass
            yield data
            path.write_text(json.dumps(data))
            os.chmod(path, 0o600)

    # ── Bot API ────────────────────────────────────────────────────────

    def api(self, method: str, token: str | None = None, **params) -> Any:
        token = token or self.settings()["token"]
        r = self.client.post(f"{API}/bot{token}/{method}", json=params)
        data = r.json() if r.content else {}
        if not data.get("ok"):
            raise RuntimeError(data.get("description") or f"Telegram answered {r.status_code}")
        return data["result"]

    def send(self, text: str, buttons: list[list[dict]] | None = None, force_reply: bool = False,
             reply_to: int | None = None) -> int | None:
        s = self.settings()
        if not (s["token"] and s["chat_id"]):
            return None
        params: dict[str, Any] = {"chat_id": s["chat_id"], "text": text[:4000], "parse_mode": "HTML",
                                  "disable_web_page_preview": True}
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        elif force_reply:
            params["reply_markup"] = {"force_reply": True, "input_field_placeholder": "Your answer"}
        if reply_to:
            params["reply_to_message_id"] = reply_to
        try:
            return self.api("sendMessage", **params)["message_id"]
        except Exception:
            return None

    # ── Setup ──────────────────────────────────────────────────────────

    def connect(self, token: str) -> str:
        """Check the token; returns the bot's @username."""
        token = token.strip()
        me = self.api("getMe", token=token)
        self.save_settings({"token": token, "bot": me.get("username", ""), "chat_id": None})
        # Skip anything sent to the bot before now
        upd = self.api("getUpdates", token=token, offset=-1, timeout=0)
        with self.state() as st:
            st["offset"] = (upd[-1]["update_id"] + 1) if upd else 0
        return me.get("username", "")

    def wait_for_start(self, timeout_s: int = 120) -> int | None:
        """Wait for you to message the bot; remember your chat."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            with self.state() as st:
                offset = st["offset"]
            wait = int(min(20, max(1, deadline - time.monotonic())))
            try:
                updates = self.api("getUpdates", offset=offset, timeout=wait,
                                   allowed_updates=["message"])
            except Exception:
                time.sleep(2)
                continue
            for u in updates:
                with self.state() as st:
                    st["offset"] = u["update_id"] + 1
                msg = u.get("message") or {}
                chat = (msg.get("chat") or {}).get("id")
                if chat and msg.get("chat", {}).get("type") == "private":
                    self.save_settings({"chat_id": chat})
                    self.send("✅ Connected. You'll get job alerts here.\n"
                              "Commands: /status, /pause (stop auto-apply), /resume.")
                    return chat
        return None

    # ── Messages ───────────────────────────────────────────────────────

    def _token_for(self, job_id: str) -> str:
        with self.state() as st:
            for tok, jid in st["tokens"].items():
                if jid == job_id:
                    return tok
            tok = secrets.token_hex(4)
            st["tokens"][tok] = job_id
            if len(st["tokens"]) > 2000:              # forget the oldest
                st["tokens"] = dict(list(st["tokens"].items())[-1500:])
            return tok

    def notify_new(self, jobs: list[dict]) -> int:
        s = self.settings()
        if not (self.configured() and s["new_jobs"]):
            return 0
        jobs = [j for j in jobs if (j.get("fit_score") or 0) >= s["min_score"]
                and j.get("status") not in ("skipped", "applied")]
        jobs.sort(key=lambda j: -(j.get("fit_score") or 0))
        for j in jobs[:5]:
            tok = self._token_for(j["id"])
            text = (f"🆕 <b>{html.escape(j['company'])}</b> just opened a role\n"
                    f"{html.escape(j['title'])}\n{html.escape(j.get('location') or '')}\n"
                    f"Score <b>{j.get('fit_score', 0):.0f}</b>")
            buttons = [[{"text": "Submit it", "callback_data": f"j:{tok}:s"},
                        {"text": "Prepare", "callback_data": f"j:{tok}:p"},
                        {"text": "Skip", "callback_data": f"j:{tok}:k"}]]
            if j.get("url"):
                buttons.append([{"text": "Open posting", "url": j["url"]}])
            self.send(text, buttons)
        if len(jobs) > 5:
            self.send(f"…and {len(jobs) - 5} more matching jobs opened. See Live jobs on the dashboard.")
        return min(len(jobs), 5)

    def notify_applied(self, job: dict, report) -> None:
        s = self.settings()
        if not (self.configured() and s["applied"]):
            return
        ai = [f for f in report.filled if f.label in (report.ai_written or [])]
        text = (f"✅ <b>Applied: {html.escape(job['company'])}</b>\n{html.escape(job['title'])}\n"
                f"{html.escape(report.submit_note)}")
        for f in ai[:3]:
            text += f"\n\n<i>{html.escape(f.label[:80])}</i>\n{html.escape(f.value[:500])}"
        self.send(text)

    def notify_stopped(self, job: dict, why: str) -> None:
        if self.configured() and self.settings()["needs"]:
            self.send(f"⏸ <b>{html.escape(job['company'])}</b> — {html.escape(job['title'])}\n"
                      f"Not sent: {html.escape(why)}\nIt's waiting under Needs you.")

    def notify_questions(self, job: dict, questions: list[dict]) -> int:
        """One message per new question; tap an option or reply."""
        s = self.settings()
        if not (self.configured() and s["needs"]):
            return 0
        sent = 0
        for q in questions:
            key = f"{q['company']}|{q['question']}"
            with self.state() as st:
                if key in st["sent_questions"]:
                    continue
                st["sent_questions"] = (st["sent_questions"] + [key])[-3000:]
            opts = [o for o in q.get("options") or [] if o and not o.lower().startswith(("select", "choose"))]
            head = (f"❓ <b>{html.escape(job['company'])}</b> asks:\n{html.escape(q['label'])}\n"
                    f"<i>Your answer is saved for {html.escape(job['company'])} and the application retried.</i>")
            ref = {"kind": "question", "company": q["company"], "question": q["question"],
                   "options": opts, "job_id": job["id"]}
            if opts and len(opts) <= 8 and not q.get("multi"):
                tok = secrets.token_hex(4)
                with self.state() as st:
                    st["replies"][f"q{tok}"] = ref
                rows = [[{"text": o[:60], "callback_data": f"q:{tok}:{i}"}] for i, o in enumerate(opts)]
                mid = self.send(head, rows)
            else:
                hint = f"\nOptions: {html.escape(', '.join(opts))}" if opts else ""
                mid = self.send(head + hint + "\n\n<b>Reply to this message</b> with your answer.", force_reply=True)
                if mid:
                    with self.state() as st:
                        st["replies"][str(mid)] = ref
            sent += bool(mid)
        return sent

    def summary(self, tracker) -> None:
        from datetime import timedelta
        day = (datetime.now() - timedelta(hours=24)).isoformat(timespec="seconds")
        new = [e for e in tracker.events(since=day, limit=3000) if e["kind"] == "new"]
        applied = [j for j in tracker.by_status(("applied",)) if (j.get("applied_at") or "") >= day]
        self.send(f"☀️ <b>Last 24 hours</b>\n{len(new)} new matching postings\n{len(applied)} applications sent\n"
                  + "".join(f"\n• {html.escape(j['company'])}: {html.escape(j['title'][:60])}" for j in applied[:10]))

    def maybe_summary(self, tracker) -> None:
        s = self.settings()
        if not self.configured():
            return
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        with self.state() as st:
            due = now.hour >= s["summary_hour"] and st["last_summary"] != today
            if due:
                st["last_summary"] = today
        if due:
            self.summary(tracker)

    # ── Asking you something and waiting (security codes) ──────────────

    def ask(self, text: str, timeout_s: int = 600, crawler_polls: bool | None = None) -> str | None:
        """Send a question that needs a typed reply and wait for it. When the
        crawler is running it reads replies and hands this one over through
        the shared state; otherwise this polls Telegram itself."""
        if not self.configured():
            return None
        ask_id = secrets.token_hex(5)
        mid = self.send(text + "\n\n<b>Reply to this message.</b>", force_reply=True)
        if not mid:
            return None
        with self.state() as st:
            st["replies"][str(mid)] = {"kind": "ask", "id": ask_id}
            st["asks"][ask_id] = None
        if crawler_polls is None:
            from pipeline.crawler.worker import read_status
            crawler_polls = bool(read_status().get("running"))
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if not crawler_polls:
                self.poll_once(lambda *a: None, wait=10)
            else:
                time.sleep(2)
            with self.state() as st:
                answer = st["asks"].get(ask_id)
                if answer:
                    st["asks"].pop(ask_id, None)
                    return answer
        return None

    # ── Reading your taps and replies ──────────────────────────────────

    def poll_once(self, on_action: Callable[[str, str], str], wait: int = 0) -> int:
        """Handle new updates. on_action(job_id, action) runs Submit/Prepare/Skip
        and returns a short status line. Returns how many updates were handled."""
        s = self.settings()
        if not (s["token"] and s["chat_id"]):
            return 0
        with self.state() as st:
            offset = st["offset"]
        try:
            updates = self.api("getUpdates", offset=offset, timeout=wait,
                               allowed_updates=["message", "callback_query"])
        except Exception:
            return 0
        for u in updates:
            with self.state() as st:
                st["offset"] = max(st["offset"], u["update_id"] + 1)
            try:
                self._handle(u, s["chat_id"], on_action)
            except Exception as e:
                self.send(f"Couldn't do that: {html.escape(str(e)[:200])}")
        return len(updates)

    def _handle(self, u: dict, chat_id: int, on_action: Callable[[str, str], str]) -> None:
        from pipeline import applicant
        cb = u.get("callback_query")
        if cb:
            if (cb.get("message") or {}).get("chat", {}).get("id") != chat_id:
                return
            kind, tok, arg = (cb.get("data", "") + "::").split(":")[:3]
            note = "Done"
            if kind == "j":
                with self.state() as st:
                    job_id = st["tokens"].get(tok)
                if job_id:
                    note = on_action(job_id, {"s": "submit", "p": "prepare", "k": "skip"}.get(arg, ""))
            elif kind == "q":
                with self.state() as st:
                    ref = st["replies"].get(f"q{tok}")
                if ref and arg.isdigit() and int(arg) < len(ref["options"]):
                    answer = ref["options"][int(arg)]
                    applicant.save_answers([{"question": ref["question"], "answer": answer,
                                             "company": ref["company"], "everywhere": False}], self.dir)
                    note = f"Saved: {answer}"
            try:
                self.api("answerCallbackQuery", callback_query_id=cb["id"], text=note[:190])
                self.send(html.escape(note), reply_to=(cb.get("message") or {}).get("message_id"))
            except Exception:
                pass
            return
        msg = u.get("message") or {}
        if (msg.get("chat") or {}).get("id") != chat_id:
            return
        text = (msg.get("text") or "").strip()
        reply = msg.get("reply_to_message")
        if reply:
            with self.state() as st:
                ref = st["replies"].get(str(reply.get("message_id")))
                if ref and ref["kind"] == "ask":
                    st["asks"][ref["id"]] = text
            if ref and ref["kind"] == "question" and text:
                applicant.save_answers([{"question": ref["question"], "answer": text,
                                         "company": ref["company"], "everywhere": False}], self.dir)
                self.send(f"Saved for {html.escape(ref['company'])}. The application will be retried.")
            elif ref and ref["kind"] == "ask":
                self.send("Got it — entering it now.")
            return
        if text.startswith("/"):
            self._command(text.split()[0].lower())

    def _command(self, cmd: str) -> None:
        from pipeline import autoapply
        if cmd == "/pause":
            autoapply.save_settings({"enabled": False}, self.dir)
            self.send("⏸ Auto-apply paused. /resume to turn it back on.")
        elif cmd == "/resume":
            autoapply.save_settings({"enabled": True}, self.dir)
            self.send("▶️ Auto-apply is on.")
        elif cmd in ("/status", "/start"):
            s = autoapply.load_settings(self.dir)
            today = autoapply.today_attempts(self.dir)
            self.send(f"Auto-apply is <b>{'on' if s['enabled'] else 'off'}</b> ({s['mode']} mode, score ≥ {s['min_score']}).\n"
                      f"{sum(today.values())} of {s['max_per_day']} tries used today.")
