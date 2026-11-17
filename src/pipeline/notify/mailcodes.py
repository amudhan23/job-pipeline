"""Read the security code a job site emails you, so unattended submits can finish.

Greenhouse (and sometimes Lever, Ashby, Workday) emails a one-time code
before it accepts an application. With a Gmail app password saved, jobpipe
looks for that email itself:

  - read-only: the mailbox is opened with EXAMINE and messages are fetched
    with BODY.PEEK, so nothing is marked read, moved or deleted;
  - narrow: only mail that arrived after the form was submitted, from job
    sites or with "security/verification code" in the subject;
  - nothing is stored: the code is handed to the form and forgotten.

Gmail needs 2-step verification on, then an app password
(myaccount.google.com/apppasswords). Settings live in
data/private/mail.json (or JOBPIPE_IMAP_USER / JOBPIPE_IMAP_PASSWORD).
"""

from __future__ import annotations

import email
import imaplib
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable

FILE = "mail.json"
JOB_SITES = ("greenhouse", "lever.co", "ashbyhq", "ashby", "myworkday", "workday", "smartrecruiters",
             "icims", "jobvite", "workable", "bamboohr", "rippling", "recruitee")
_SUBJECT = re.compile(r"security code|verification code|verify your|confirm your (email|application)|one[- ]time", re.I)
# The code sits right after words like "code" / "security code:"; Greenhouse uses 8 letters and digits.
_CODE_NEAR = re.compile(r"(?:security|verification|one[- ]time|confirmation)?\s*code\b"
                        r"(?:[^A-Za-z0-9]|\b(?:is|below|here|was|reads)\b){0,60}?"
                        r"\b([A-Za-z0-9]{6,10})\b", re.I)
_LONE = re.compile(r"^\s*([A-Za-z0-9]{6,10})\s*$", re.M)
_NOT_CODES = {"security", "application", "verification", "password", "following", "greenhouse", "required",
              "below", "please", "submit", "confirm", "expires", "minutes", "account", "careers"}


def _pdir(private_dir: Path | None) -> Path:
    from pipeline import applicant
    return private_dir or applicant.PRIVATE_DIR


def settings(private_dir: Path | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {"email": "", "app_password": "", "host": "imap.gmail.com", "enabled": True}
    try:
        data.update(json.loads((_pdir(private_dir) / FILE).read_text()))
    except (OSError, ValueError):
        pass
    if os.environ.get("JOBPIPE_IMAP_USER"):
        data["email"] = os.environ["JOBPIPE_IMAP_USER"]
    if os.environ.get("JOBPIPE_IMAP_PASSWORD"):
        data["app_password"] = os.environ["JOBPIPE_IMAP_PASSWORD"]
    return data


def configured(private_dir: Path | None = None) -> bool:
    s = settings(private_dir)
    return bool(s.get("enabled") and s.get("email") and s.get("app_password"))


def save(values: dict[str, Any], private_dir: Path | None = None) -> dict[str, Any]:
    s = settings(private_dir)
    if "email" in values:
        addr = str(values["email"]).strip()
        if addr and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", addr, re.I):
            raise ValueError("That email address doesn't look right.")
        s["email"] = addr
    if values.get("app_password"):
        pw = re.sub(r"\s+", "", str(values["app_password"]))      # Google shows it in groups of four
        if not re.fullmatch(r"[A-Za-z0-9]{12,64}", pw):
            raise ValueError("A Gmail app password is 16 letters (spaces are fine).")
        s["app_password"] = pw
    if "enabled" in values:
        s["enabled"] = bool(values["enabled"])
    if values.get("host"):
        s["host"] = str(values["host"]).strip()[:100]
    p = _pdir(private_dir) / FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({k: s[k] for k in ("email", "app_password", "host", "enabled")}, indent=2))
    os.chmod(p, 0o600)
    return public(private_dir)


def forget(private_dir: Path | None = None) -> None:
    (_pdir(private_dir) / FILE).unlink(missing_ok=True)


def public(private_dir: Path | None = None) -> dict[str, Any]:
    """Settings safe to show: never the password."""
    s = settings(private_dir)
    return {"email": s["email"], "has_password": bool(s["app_password"]), "enabled": bool(s["enabled"]),
            "configured": configured(private_dir)}


# ── Reading ────────────────────────────────────────────────────────────

def _text(msg: email.message.Message) -> str:
    parts = []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            body = part.get_payload(decode=True) or b""
            s = body.decode(part.get_content_charset() or "utf-8", "replace")
        except (LookupError, ValueError):
            continue
        if ctype == "text/html":
            s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
            s = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|td|h\d)>", "\n", s)
            s = re.sub(r"<[^>]+>", " ", s)
            s = re.sub(r"&nbsp;", " ", s)
        parts.append(s)
    return "\n".join(parts)


def extract_code(text: str) -> str | None:
    """The one-time code in an email body, or None."""
    for rx in (_CODE_NEAR, _LONE):
        for m in rx.finditer(text):
            c = m.group(1)
            if c.lower() in _NOT_CODES or c.isalpha() and c.lower() == c and len(c) > 8:
                continue
            if not any(ch.isdigit() for ch in c) and not (c.isupper() and len(c) >= 6):
                continue                    # a word, not a code
            return c
    return None


def _decode(h: str | None) -> str:
    try:
        return str(make_header(decode_header(h or "")))
    except (ValueError, UnicodeDecodeError):
        return h or ""


def _from_job_site(sender: str, subject: str, company: str) -> bool:
    s = sender.lower()
    if any(site in s for site in JOB_SITES):
        return True
    words = [w for w in re.findall(r"[a-z0-9]+", company.lower()) if len(w) > 2]
    return bool(_SUBJECT.search(subject)) or (bool(words) and words[0] in s and "code" in subject.lower())


def check_once(company: str, since: datetime, imap: Any) -> str | None:
    """One look through the inbox for a code that arrived after `since`."""
    imap.select("INBOX", readonly=True)
    day = (since - timedelta(days=1)).strftime("%d-%b-%Y")
    typ, data = imap.search(None, "SINCE", day)
    if typ != "OK" or not data or not data[0]:
        return None
    ids = data[0].split()[-25:][::-1]                       # newest first
    for mid in ids:
        typ, head = imap.fetch(mid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
        if typ != "OK" or not head or not isinstance(head[0], tuple):
            continue
        h = email.message_from_bytes(head[0][1])
        try:
            when = parsedate_to_datetime(h.get("Date"))
        except (TypeError, ValueError):
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when < since - timedelta(minutes=2):
            break                                            # older than the submit: stop looking
        sender, subject = _decode(h.get("From")), _decode(h.get("Subject"))
        if not _from_job_site(sender, subject, company):
            continue
        typ, body = imap.fetch(mid, "(BODY.PEEK[])")
        if typ != "OK" or not body or not isinstance(body[0], tuple):
            continue
        code = extract_code(_text(email.message_from_bytes(body[0][1][:300_000])))
        if code:
            return code
    return None


def find_code(company: str, since: datetime | None = None, timeout_s: int = 240, poll_s: int = 10,
              private_dir: Path | None = None,
              connect: Callable[[str], Any] | None = None, sleep: Callable[[float], None] = time.sleep) -> str | None:
    """Wait up to `timeout_s` for the job site's code email; return the code or None."""
    s = settings(private_dir)
    if not configured(private_dir):
        return None
    since = since or datetime.now(timezone.utc)
    if since.tzinfo is None:
        since = since.astimezone(timezone.utc)
    connect = connect or (lambda host: imaplib.IMAP4_SSL(host, 993, timeout=30))
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            imap = connect(s["host"])
            try:
                imap.login(s["email"], s["app_password"])
                code = check_once(company, since, imap)
            finally:
                try:
                    imap.logout()
                except Exception:
                    pass
            if code:
                return code
        except imaplib.IMAP4.error:
            return None                                      # wrong password: waiting won't help
        except (OSError, TimeoutError):
            pass                                             # network blip: try again
        if time.monotonic() + poll_s > deadline:
            return None
        sleep(poll_s)


def test_login(private_dir: Path | None = None, connect: Callable[[str], Any] | None = None) -> tuple[bool, str]:
    """Log in once and count recent job-site emails (for the settings page)."""
    s = settings(private_dir)
    if not configured(private_dir):
        return False, "Add your Gmail address and an app password first."
    connect = connect or (lambda host: imaplib.IMAP4_SSL(host, 993, timeout=30))
    try:
        imap = connect(s["host"])
        imap.login(s["email"], s["app_password"])
        imap.select("INBOX", readonly=True)
        typ, data = imap.search(None, "SINCE", (datetime.now() - timedelta(days=30)).strftime("%d-%b-%Y"),
                                "FROM", "greenhouse")
        n = len(data[0].split()) if typ == "OK" and data and data[0] else 0
        imap.logout()
        return True, f"Connected. {n} email{'s' if n != 1 else ''} from Greenhouse in the last 30 days."
    except imaplib.IMAP4.error as e:
        return False, ("Gmail refused the login. Use an app password (not your normal password), and check "
                       f"IMAP is on in Gmail settings. ({str(e)[:80]})")
    except OSError as e:
        return False, f"Couldn't reach {s['host']} ({type(e).__name__})."


def latest_code(days: int = 60, private_dir: Path | None = None,
                connect: Callable[[str], Any] | None = None) -> dict[str, str] | None:
    """For the rehearsal: the newest security code already in your inbox
    (proves the reader works on your real mail). Code masked."""
    s = settings(private_dir)
    if not configured(private_dir):
        return None
    connect = connect or (lambda host: imaplib.IMAP4_SSL(host, 993, timeout=30))
    imap = connect(s["host"])
    try:
        imap.login(s["email"], s["app_password"])
        imap.select("INBOX", readonly=True)
        typ, data = imap.search(None, "SINCE", (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y"))
        if typ != "OK" or not data or not data[0]:
            return None
        for mid in data[0].split()[::-1][:400]:
            typ, head = imap.fetch(mid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            if typ != "OK" or not head or not isinstance(head[0], tuple):
                continue
            h = email.message_from_bytes(head[0][1])
            sender, subject = _decode(h.get("From")), _decode(h.get("Subject"))
            if not (any(site in sender.lower() for site in JOB_SITES) and _SUBJECT.search(subject)):
                continue
            typ, body = imap.fetch(mid, "(BODY.PEEK[])")
            if typ == "OK" and body and isinstance(body[0], tuple):
                code = extract_code(_text(email.message_from_bytes(body[0][1][:300_000])))
                if code:
                    return {"code": code[:2] + "•" * (len(code) - 2), "subject": subject[:80],
                            "from": sender[:80], "date": h.get("Date", "")}
        return None
    finally:
        try:
            imap.logout()
        except Exception:
            pass
