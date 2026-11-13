"""Decide how to answer an application question from its visible wording.

Every rule maps question text → a field in your saved details. Anything
not covered returns None, and the bot reports it as "needs your attention"
instead of guessing. Legal consent boxes are never answered automatically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pipeline.applicant import DECLINE


@dataclass
class Answer:
    value: str          # text to type, option to pick, or "__RESUME__" for uploads
    source: str         # which saved field it came from (for the report)


RESUME = "__RESUME__"

# Order matters: first match wins (e.g. "LinkedIn" before generic "website").
_RULES: list[tuple[str, str]] = [
    (r"\bresume\b|\bcv\b|curriculum vitae", "resume"),
    (r"linkedin", "linkedin"),
    (r"github", "github"),
    (r"portfolio|personal (web)?site|\bwebsite\b|other url", "website"),
    (r"preferred (first )?name|\bfirst name\b|given name", "first_name"),
    (r"\blast name\b|surname|family name", "last_name"),
    (r"^(full )?name\b|^your name|legal name", "full_name"),
    (r"e-?mail", "email"),
    (r"phone|mobile|telephone", "phone"),
    # Sponsorship before authorization: "authorized ... without sponsorship" is a sponsorship question
    (r"sponsor", "sponsorship"),
    (r"(legally )?authori[sz]ed to work|eligib\w* to work|right to work|work authori[sz]ation", "authorized_us"),
    (r"visa|immigration status|work status|citizenship status", "visa_status"),
    (r"veteran", "veteran"),
    (r"disabilit", "disability"),
    (r"gender|\bsex\b", "gender"),
    (r"\brace\b|ethnicit|hispanic|latino", "race"),
    (r"hear about|how did you (find|learn)|referr?al source|^source\b|application source", "referral_source"),
    (r"start date|available to start|earliest|notice period|when can you start", "start_date"),
    (r"relocat", "relocate"),
    (r"salary|compensation|pay (range|expectation)|desired pay", "salary"),
    (r"\bgpa\b|grade point", "gpa"),
    (r"graduat", "graduation"),
    (r"discipline|major|field of study|area of study", "major"),
    (r"\bdegree\b", "degree"),
    (r"school|university|college|institution", "school"),
    (r"location|city|where are you (based|located)|current address|address", "city"),
]

# Never auto-tick these — they're legal statements you should read yourself
_CONSENT = re.compile(r"\b(i agree|i consent|consent to|i certify|certify that|i acknowledge|"
                      r"acknowledge that|terms|privacy (policy|notice)|attest|accurate and complete)\b", re.I)


def is_consent(label: str) -> bool:
    return bool(_CONSENT.search(label))


def answer_for(label: str, details: dict[str, str]) -> Answer | None:
    """Best answer for a question, or None if we shouldn't guess."""
    text = " ".join(label.lower().replace("*", " ").split())
    if not text or is_consent(text):
        return None
    for pattern, field in _RULES:
        if not re.search(pattern, text):
            continue
        if field == "resume":
            return Answer(RESUME, "resume.pdf")
        if field == "full_name":
            name = f"{details.get('first_name', '')} {details.get('last_name', '')}".strip()
            return Answer(name, "first_name + last_name") if name else None
        if field == "sponsorship":
            return _sponsorship(text, details)
        value = details.get(field, "")
        return Answer(value, field) if value else None
    return None


def _sponsorship(text: str, details: dict[str, str]) -> Answer | None:
    now, future = details.get("sponsorship_now", ""), details.get("sponsorship_future", "")
    asks_future = "future" in text
    asks_now = bool(re.search(r"\bnow\b|current|currently|today", text))
    if asks_future or not asks_now:
        # "now or in the future" (or unspecified) → Yes if either is Yes
        if not (now or future):
            return None
        value = "Yes" if "Yes" in (now, future) else "No"
        return Answer(value, "sponsorship_now/future")
    return Answer(now, "sponsorship_now") if now else None


# ── Matching an answer to a dropdown / radio option ────────────────────

_DECLINE_OPT = re.compile(r"decline|do not wish|don.t wish|prefer not|not to (answer|disclose|say)|"
                          r"choose not|rather not", re.I)


def pick_option(answer: str, options: list[str]) -> str | None:
    """Choose the option that means `answer`. None if nothing fits safely."""
    opts = [o for o in options if o and o.strip() and not re.fullmatch(r"(select|choose)\b.*|-+|—.*", o.strip(), re.I)]
    a = answer.strip().lower()
    if not a or not opts:
        return None
    if a == DECLINE.lower() or _DECLINE_OPT.search(a):
        return next((o for o in opts if _DECLINE_OPT.search(o)), None)
    for o in opts:                                   # exact
        if o.strip().lower() == a:
            return o
    if a in ("yes", "no"):                           # "Yes, I am authorized…"
        return next((o for o in opts if re.match(rf"\s*{a}\b", o, re.I)), None)
    for o in opts:                                   # containment either way
        ol = o.strip().lower()
        if a in ol or ol in a:
            return o
    return None
