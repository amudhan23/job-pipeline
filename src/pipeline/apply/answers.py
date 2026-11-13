"""Decide how to answer an application question from its visible wording.

Every rule maps question text → a field in your saved details. Anything
not covered returns None, and the bot reports it as "needs your attention"
instead of guessing. Legal consent boxes are never answered automatically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pipeline.applicant import DECLINE


@dataclass
class Answer:
    value: str          # text to type, option to pick, or "__RESUME__" for uploads
    source: str         # which saved field it came from (for the report)
    # Other ways to say the same thing when a dropdown has no option for
    # `value` — e.g. Twitch's "Open to relocation?" lists cities, not Yes/No
    alternates: list[str] = field(default_factory=list)


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
    # Specific immigration questions before the generic ones
    (r"h-?1b (status|petition)|held h-?1b|h-?1b .*approved", "h1b_before"),
    # Export-control "U.S. person" status (citizen / green card / refugee /
    # asylee / none): a legal answer — only filled for U.S. citizens
    (r"u\.?s\.? citizen or national|refugee|asylee|\bu\.?s\.? person\b", "us_person"),
    (r"permanent resident.*\b(other|another)\b|\b(other|another) country.*permanent resident", "other_pr"),
    (r"security clearance|\bclearance\b", "clearance"),
    (r"\b(located|based|reside|residing|living|live|currently) in the (us|u\.s\.?|united states)\b", "in_us"),
    # Sponsorship before authorization: "authorized ... without sponsorship" is a sponsorship question
    (r"sponsor", "sponsorship"),
    (r"(legally )?authori[sz]ed to work|eligib\w* to (work|begin employment)|right to work|"
     r"work authori[sz]ation|legally eligible", "authorized_us"),
    (r"citizenship|citizen of|export (licens|control)", "citizenship"),
    (r"visa|immigration status|work status", "visa_status"),
    (r"non-?compet|restrictive covenant|agreement that would (prevent|restrict|limit)", "noncompete"),
    (r"(future|other) (opportunit|roles?|positions?|openings)|considered for (other|future)", "future_roles"),
    (r"\b(current(ly)?|presently)\b.*\b(employee|employed)\b", "current_employer"),
    (r"\b(previously|ever|formerly|in the past)\b.*\b(employed|worked)\b", "past_employers"),
    (r"^country\b|country of residence|country you live|\bcountry\b(?!.*citizen)", "country"),
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


def answer_for(label: str, details: dict[str, str], company: str = "",
               saved: dict | None = None) -> Answer | None:
    """Best answer for a question, or None if we shouldn't guess.

    Your saved answers win over the rules: this company's own answers
    first, then answers you chose to use for every company."""
    from pipeline.applicant import lookup_answer, normalize_question
    text = " ".join(label.lower().replace("*", " ").split())
    if not text or is_consent(text):
        return None
    if saved:
        found = lookup_answer(saved, normalize_question(label, company), company)
        if found is not None:
            return Answer(found, "your saved answer")
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
        if field in ("current_employer", "past_employers"):
            return _employer(text, field, details, company)
        if field in ("us_person", "clearance", "in_us"):
            return _derived(field, details)
        value = details.get(field, "")
        if not value:
            return None
        if field == "relocate" and value == "Yes" and details.get("city"):
            # Willing to relocate, but the form lists locations: pick your city
            city = details["city"]
            return Answer(value, field, alternates=[city, city.split(",")[0].strip()])
        if field == "referral_source":
            # Forms list their own sources. Jobs here come from the company's
            # own job board, so "careers page / website" is the true fallback.
            return Answer(value, field, alternates=[
                "Company careers page", "Company website", "Careers page", "Job board", "Online"])
        return Answer(value, field)
    return None


_US = {"united states", "united states of america", "usa", "us", "u.s.", "u.s.a."}


def _derived(field: str, details: dict[str, str]) -> Answer | None:
    """Answers that follow from what you already saved."""
    citizenship = details.get("citizenship", "").strip().lower()
    country = details.get("country", "").strip().lower()
    if field == "in_us":
        if not country:
            return None
        return Answer("Yes" if country in _US else "No", "country")
    if field == "clearance":
        # Clearances are only granted to U.S. citizens
        if citizenship and not any(c.strip() in _US for c in citizenship.split(",")):
            return Answer("No", "citizenship (clearances need U.S. citizenship)")
        return None
    if field == "us_person":
        if citizenship and any(c.strip() in _US for c in citizenship.split(",")):
            return Answer("U.S. citizen or national", "citizenship")
        return None            # green card / refugee / "none of the above": yours to pick
    return None


def _employer(text: str, field: str, details: dict[str, str], company: str) -> Answer | None:
    """"Are you a current <X> employee?" / "Have you previously worked for <X>?"
    Yes only if X (or this company) is in your saved employers."""
    if not re.match(r"(are|have|do|did|were|is)\b", text):
        return None                      # "Where are you currently employed?" wants a name
    raw = details.get(field, "").strip()
    if not raw:
        return None                      # unknown — ask rather than guess
    names = [n.strip().lower() for n in raw.split(",")
             if n.strip() and n.strip().lower() not in ("none", "n/a", "no")]
    mentioned = [n for n in names if re.search(rf"\b{re.escape(n)}\b", text)]
    is_this_company = company and any(n == company.lower() for n in names)
    return Answer("Yes" if mentioned or is_this_company else "No", field)


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
    # The answer inside an option: the shortest such option is the closest
    # ("United States" → "United States +1", not "United States Minor
    # Outlying Islands +1", wherever they sit in the list)
    holders = [o for o in opts if a in o.strip().lower()]
    if holders:
        return min(holders, key=len)
    # An option inside the answer: the longest one says the most
    inside = [o for o in opts if len(o.strip()) > 2 and o.strip().lower() in a]
    if inside:
        return max(inside, key=len)
    return None
