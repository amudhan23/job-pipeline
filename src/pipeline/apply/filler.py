"""Fill a job application form in a real (headless) browser.

How it works:
  1. Open the job's application page (Greenhouse, Lever or Ashby).
  2. A script in the page lists every question with the text a person sees
     next to it — not internal field names, which differ per site and change.
  3. answers.py picks the answer from your saved details.
  4. Each control is filled the way a person would: type, choose a dropdown
     option, click a radio / Yes-No button, attach the resume.
  5. Practice mode: full-page screenshot + report, and it never submits.
  6. Submit mode (only when every required question is answered and you
     said yes): clicks Submit and waits for the site's confirmation. A
     CAPTCHA or emailed code is handed to you in the open browser — never
     worked around.

Anything it can't answer, plus every legal consent box, is listed under
"needs your attention" instead of being guessed.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from pipeline.apply.answers import RESUME, answer_for, is_consent, pick_option

# Shared by the scripts below: what a person would read as each control's
# question, whether it's visible, and what to leave alone.
_HELPERS_JS = r"""
  const txt = s => (s || '').replace(/\s+/g, ' ').trim();
  const visible = el => {
    if (el.type === 'file') return true;           // file inputs are often hidden behind a button
    // Hidden "shadow" inputs that dropdown widgets keep for form validation
    // (Greenhouse's react-select) would otherwise count every question twice
    if (el.closest('[aria-hidden="true"]')) return false;
    const r = el.getBoundingClientRect(), st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') return false;
    if (parseFloat(st.opacity) === 0 && el.getAttribute('role') !== 'combobox') return false;
    if (el.tabIndex === -1 && r.width <= 2 && r.height <= 2) return false;
    return r.width > 0 || r.height > 0;
  };
  // Button captions that say what to DO, not what the question is
  // (Greenhouse labels its resume file input "Attach")
  const GENERIC = /^(attach|upload|browse|choose( a)? file|select( a)? file|drop files?( here)?|enter manually|add file)\s*$/i;
  const labelOf = el => {
    const own = labelOfRaw(el);
    if (!GENERIC.test(own)) return own;
    // The real question sits on the surrounding group / fieldset
    const grp = el.closest('[role="group"][aria-labelledby], [role="group"][aria-label], fieldset');
    if (grp) {
      if (grp.getAttribute('aria-labelledby')) {
        const t = grp.getAttribute('aria-labelledby').split(/\s+/)
          .map(id => document.getElementById(id)?.innerText || '').join(' ');
        if (txt(t)) return txt(t);
      }
      if (grp.getAttribute('aria-label')) return txt(grp.getAttribute('aria-label'));
      const lg = grp.querySelector('legend');
      if (lg && txt(lg.innerText)) return txt(lg.innerText);
    }
    let c = el.parentElement;
    for (let i = 0; i < 6 && c; i++, c = c.parentElement) {
      const l = [...c.querySelectorAll('label, legend, [class*="label" i]')]
        .find(x => !x.contains(el) && txt(x.innerText) && !GENERIC.test(txt(x.innerText)));
      if (l) return txt(l.innerText);
    }
    return own;
  };
  const labelOfRaw = el => {
    if (el.getAttribute('aria-labelledby')) {
      const t = el.getAttribute('aria-labelledby').split(/\s+/)
        .map(id => document.getElementById(id)?.innerText || '').join(' ');
      if (txt(t)) return txt(t);
    }
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l && txt(l.innerText)) return txt(l.innerText);
    }
    if (el.getAttribute('aria-label')) return txt(el.getAttribute('aria-label'));
    const wrap = el.closest('label');
    if (wrap && txt(wrap.innerText)) return txt(wrap.innerText);
    const fs = el.closest('fieldset');
    if (fs && fs.querySelector('legend')) return txt(fs.querySelector('legend').innerText);
    let c = el.parentElement;
    for (let i = 0; i < 5 && c; i++, c = c.parentElement) {
      const l = c.querySelector('label, legend, [class*="label" i], [class*="question" i]');
      if (l && !l.contains(el) && txt(l.innerText)) return txt(l.innerText);
    }
    return txt(el.getAttribute('placeholder') || el.name || '');
  };
  const required = (el, label) => el.required || el.getAttribute('aria-required') === 'true'
                                  || el.closest('[role="group"]')?.getAttribute('aria-required') === 'true'
                                  || /\*\s*$/.test(label) || /\(required\)/i.test(label);

  // Controls that aren't questions and are never touched:
  //  - CAPTCHA plumbing (reCAPTCHA / hCaptcha / Turnstile keep a hidden
  //    textarea in the form) — a CAPTCHA is for the human;
  //  - Ashby's "Autofill from resume" uploader, which parses a resume into
  //    the form (the real Resume question comes later).
  const CAPTCHA = /g-recaptcha-response|h-captcha-response|cf-turnstile-response|captcha/i;
  const ignored = el =>
    CAPTCHA.test((el.name || '') + ' ' + (el.id || '') + ' ' + (typeof el.className === 'string' ? el.className : ''))
    || !!el.closest('.g-recaptcha, .h-captcha, .cf-turnstile, [class*="captcha" i]')
    || (el.type === 'file' && !!el.closest('[class*="autofill" i]'));
  // Group label for a set of controls: fieldset legend, else the nearest
  // container holding all of them that has a label-like element
  const groupLabel = group => {
    const fs = group[0].closest('fieldset');
    if (fs && group.every(g => fs.contains(g)) && fs.querySelector('legend'))
      return txt(fs.querySelector('legend').innerText);
    let c = group[0].parentElement;
    for (let i = 0; i < 6 && c; i++, c = c.parentElement) {
      if (group.every(g => c.contains(g))) {
        const l = [...c.querySelectorAll('legend, [class*="label" i], [class*="question" i], label')]
          .find(x => !group.some(g => x.contains(g)) && txt(x.innerText));
        if (l) return txt(l.innerText);
      }
    }
    return '';
  };

  // The checkboxes / radios that answer one question together
  const groupOf = el => {
    const sel = `input[type=${el.type}]`;
    let g = el.name ? [...document.querySelectorAll(`${sel}[name="${CSS.escape(el.name)}"]`)] : [];
    if (g.length < 2) {
      const fs = el.closest('fieldset');
      g = fs ? [...fs.querySelectorAll(sel)] : [el];
    }
    return g;
  };
"""

# Lists the form's questions. Every control gets a data-jp attribute so
# Python can find it again. Returns one entry per question.
_ENUMERATE_JS = "() => {\n" + _HELPERS_JS + r"""
  const g0req = group => group[0].closest('[role="group"]')?.getAttribute('aria-required') === 'true';
  const out = []; let n = 0;
  const tag = el => { const id = 'jp' + (n++); el.setAttribute('data-jp', id); return id; };
  const seenRadio = new Set();
  const seenBox = new Set();

  for (const el of document.querySelectorAll('input, textarea, select')) {
    const type = (el.type || el.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'reset', 'image', 'search'].includes(type)) continue;
    if (!visible(el) || el.disabled) continue;
    if (ignored(el)) continue;

    if (type === 'radio') {
      if (!el.name || seenRadio.has(el.name)) continue;
      seenRadio.add(el.name);
      const group = [...document.querySelectorAll(`input[type=radio][name="${CSS.escape(el.name)}"]`)];
      const fs = el.closest('fieldset');
      let label = fs && fs.querySelector('legend') ? txt(fs.querySelector('legend').innerText) : '';
      if (!label) {
        let c = el.parentElement;
        for (let i = 0; i < 6 && c && !label; i++, c = c.parentElement) {
          if (group.every(r => c.contains(r))) {
            const l = c.querySelector('legend, [class*="label" i], [class*="question" i], label:not(:has(input))');
            if (l) label = txt(l.innerText);
          }
        }
      }
      out.push({kind: 'radio', label, required: group.some(r => r.required) || /\*\s*$/.test(label),
                options: group.map(r => ({id: tag(r), text: labelOf(r)}))});
      continue;
    }
    const label = labelOf(el);
    if (el.getAttribute('role') === 'combobox' || el.getAttribute('aria-autocomplete')) {
      out.push({kind: 'combobox', id: tag(el), label, required: required(el, label)});
    } else if (el.tagName === 'SELECT') {
      out.push({kind: 'select', id: tag(el), label, required: required(el, label),
                options: [...el.options].map(o => txt(o.text))});
    } else if (type === 'checkbox') {
      if (seenBox.has(el)) continue;
      // Several checkboxes answering one question ("Which platforms do you use?")
      const group = groupOf(el).filter(visible);
      if (group.length >= 2) {
        group.forEach(g => seenBox.add(g));
        const gl = groupLabel(group);
        // Greenhouse marks every box in a "select all that apply" list required
        out.push({kind: 'checkboxes', label: gl,
                  required: /\*\s*$/.test(gl) || group.some(g => g.required)
                            || g0req(group),
                  options: group.map(g => ({id: tag(g), text: labelOf(g)}))});
      } else {
        out.push({kind: 'checkbox', id: tag(el), label, required: required(el, label)});
      }
    } else if (type === 'file') {
      out.push({kind: 'file', id: tag(el), label, required: required(el, label)});
    } else {
      out.push({kind: el.tagName === 'TEXTAREA' ? 'textarea' : 'text', id: tag(el), label,
                required: required(el, label), filled: !!el.value});
    }
  }

  // Yes/No button pairs (Ashby-style boolean questions)
  for (const yes of document.querySelectorAll('button')) {
    if (!/^yes$/i.test(txt(yes.innerText)) || yes.hasAttribute('data-jp')) continue;
    const box = yes.parentElement;
    const no = [...box.querySelectorAll('button')].find(b => /^no$/i.test(txt(b.innerText)));
    if (!no) continue;
    let label = '', c = box;
    for (let i = 0; i < 5 && c && !label; i++, c = c.parentElement) {
      const l = c.querySelector('label, legend, [class*="label" i], [class*="question" i]');
      if (l && !l.contains(yes)) label = txt(l.innerText);
    }
    out.push({kind: 'buttons', label, required: /\*\s*$/.test(label),
              options: [{id: tag(yes), text: 'Yes'}, {id: tag(no), text: 'No'}]});
  }
  return out;
}
"""


# The options of ONE dropdown: the list its combobox points at
# (aria-controls → react-select's "react-select-<id>-listbox"), else a
# listbox inside the same field. Reading every option on the page instead
# mixes in menus that belong to other questions. Tags each option with
# data-jp-opt so Python can click it. null = no list found for this box.
_MENU_OPTIONS_JS = "(box) => {\n" + _HELPERS_JS + r"""
  document.querySelectorAll('[data-jp-opt]').forEach(o => o.removeAttribute('data-jp-opt'));
  const ids = (box.getAttribute('aria-controls') || box.getAttribute('aria-owns') || '')
    .split(/\s+/).filter(Boolean);
  if (box.id) ids.push(`react-select-${box.id}-listbox`);
  let list = null;
  for (const id of ids) { list = document.getElementById(id); if (list) break; }
  for (let c = box.parentElement, i = 0; !list && c && i < 5; c = c.parentElement, i++)
    list = c.querySelector('[role="listbox"]');
  if (!list) return null;
  let opts = [...list.querySelectorAll('[role="option"]')];
  if (!opts.length) opts = [...list.querySelectorAll('[id*="-option-"], [class*="__option"]')];
  opts = opts.filter(o => { const r = o.getBoundingClientRect(); return r.width > 0 || r.height > 0; });
  opts.forEach((o, i) => o.setAttribute('data-jp-opt', String(i)));
  return opts.map(o => txt(o.innerText));
}
"""

# Does a dropdown now show `choice` as its answer? react-select shows it in a
# "single-value" element next to the (empty) input; simple widgets put it in
# the input itself. Stops climbing at the question's own label.
_COMBO_VALUE_JS = r"""
(box) => {
  const txt = s => (s || '').replace(/\s+/g, ' ').trim();
  if (txt(box.value)) return txt(box.value);
  const labelIds = (box.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean);
  const isLabel = el => el.tagName === 'LABEL' || labelIds.includes(el.id);
  for (let c = box.parentElement, i = 0; c && i < 4; c = c.parentElement, i++) {
    const sv = c.querySelector('[class*="single-value" i], [class*="singleValue"], [class*="multi-value__label" i]');
    if (sv && txt(sv.innerText)) return txt(sv.innerText);
    if ([...c.querySelectorAll('label, [id]')].some(isLabel)) break;
  }
  return '';
}
"""

# Final pass before saying "ready": required questions that are still empty
# (given the data-jp ids of required questions), plus anything the browser's
# own form validation would refuse.
_CHECK_JS = "(ids) => {\n" + _HELPERS_JS + r"""
  const comboValue = """ + _COMBO_VALUE_JS + r""";
  const out = [], seen = new Set();
  const add = (el, why) => {
    const label = labelOf(el);
    if (seen.has(label)) return;
    seen.add(label); out.push({label, why});
  };
  for (const id of ids) {
    const el = document.querySelector(`[data-jp="${id}"]`);
    if (!el) continue;
    let empty;
    if (el.type === 'file') empty = !el.files || !el.files.length;
    else if (el.getAttribute('role') === 'combobox' || el.getAttribute('aria-autocomplete'))
      empty = !comboValue(el);
    else empty = !txt(el.value);
    if (empty) add(el, 'still empty');
  }
  for (const el of document.querySelectorAll('input, textarea, select')) {
    if (['hidden', 'submit', 'button', 'reset', 'image'].includes(el.type)) continue;
    if (el.disabled || ignored(el) || !el.willValidate || el.checkValidity()) continue;
    if (el.type === 'checkbox' || el.type === 'radio') {
      // One question, many boxes: report the question once, and only if
      // nothing in it is ticked (a lone box is its own question)
      const g = groupOf(el);
      if (g.length >= 2) {
        if (g.some(x => x.checked)) continue;
        const label = groupLabel(g);
        if (!seen.has(label)) { seen.add(label); out.push({label, why: 'nothing ticked'}); }
        continue;
      }
    }
    add(el, el.validationMessage || 'the form rejects this answer');
  }
  return out;
}
"""

_CAPTCHA_JS = r"""
() => !!document.querySelector(
  '[name="g-recaptcha-response"], [name="h-captcha-response"], [name="cf-turnstile-response"], '
  + 'iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="turnstile"], '
  + '.g-recaptcha, .h-captcha, .cf-turnstile')
"""

# Pages that mean the posting is gone even though they load fine
_CLOSED_TEXT = re.compile(
    r"no longer (available|accepting|open|active)|job (posting )?(was )?not found|"
    r"position has been filled|this job (has been|is) closed|couldn.t find (that|this) job|"
    r"page you.re looking for", re.I)


class FillProblem(Exception):
    """A field we tried and couldn't fill; the message says why."""

@dataclass
class FieldResult:
    label: str
    kind: str
    required: bool
    value: str = ""
    source: str = ""
    options: list[str] = field(default_factory=list)


@dataclass
class FillReport:
    job_id: str
    company: str
    title: str
    url: str
    mode: str
    started_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    filled: list[FieldResult] = field(default_factory=list)
    needs_attention: list[FieldResult] = field(default_factory=list)   # required, couldn't answer
    skipped_optional: list[FieldResult] = field(default_factory=list)
    screenshot: str = ""
    error: str = ""
    closed: bool = False         # posting is gone (404/410, or redirected away)
    captcha: bool = False        # the site checks for bots on submit — may need you then
    submitted: bool = False      # the site confirmed the application
    submit_note: str = ""        # what happened when submitting (or why it didn't)

    @property
    def resume_attached(self) -> bool:
        return any(f.kind == "file" and f.value == "resume.pdf" for f in self.filled)

    @property
    def ready_to_submit(self) -> bool:
        return not self.error and not self.needs_attention and bool(self.filled)

    def save(self, folder: Path) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "report.json"
        path.write_text(json.dumps({**asdict(self), "ready_to_submit": self.ready_to_submit,
                                    "resume_attached": self.resume_attached}, indent=2))
        return path


def application_url(job: dict[str, Any]) -> str | None:
    """The page that shows the application form, per platform."""
    platform, url = job.get("platform"), (job.get("url") or "").split("?")[0].rstrip("/")
    if platform == "greenhouse":
        # Job id from "gh-{slug}-{id}"; the hosted board page always carries the form,
        # even when the company links to its own careers site.
        m = re.match(r"gh-(.+)-(\d+)$", job.get("id", ""))
        if m:
            return f"https://job-boards.greenhouse.io/{m.group(1)}/jobs/{m.group(2)}"
        return url
    if platform == "lever":
        return url if url.endswith("/apply") else f"{url}/apply"
    if platform == "ashby":
        return url if url.endswith("/application") else f"{url}/application"
    return None


def _dedupe(fields: list[dict]) -> list[dict]:
    """Drop twins of the same question (a dropdown widget plus its plain
    input, or one question rendered twice) — keep the most capable control."""
    rank = {"combobox": 0, "select": 1, "radio": 2, "buttons": 2, "checkboxes": 2}
    best: dict[str, dict] = {}
    order: list[str] = []
    for f in fields:
        key = f.get("label", "").strip().lower()
        if not key or f["kind"] in ("file", "checkbox"):
            order.append(id(f)); best[id(f)] = f
            continue
        if key not in best:
            order.append(key); best[key] = f
        elif rank.get(f["kind"], 9) < rank.get(best[key]["kind"], 9):
            best[key] = f
    return [best[k] for k in order]


def fill_form(page, details: dict[str, str], resume_path: Path | None,
              report: FillReport, saved: dict[str, str] | None = None) -> list[dict]:
    """Fill every question on an already-open page; records results in report.
    Returns the questions found (for re-checking the form later)."""
    fields = _dedupe(page.evaluate(_ENUMERATE_JS))
    for f in fields:
        label, kind, required = f.get("label", ""), f["kind"], bool(f.get("required"))
        options = f.get("options") or []
        result = FieldResult(label=label, kind=kind, required=required,
                             options=[o["text"] if isinstance(o, dict) else o for o in options])

        if kind == "checkbox" and is_consent(label):
            result.source = "legal consent — read and tick it yourself"
            (report.needs_attention if required else report.skipped_optional).append(result)
            continue

        ans = answer_for(label, details, report.company, saved)
        done, used, entered = False, "", ""
        if ans:
            for candidate in [ans.value, *ans.alternates]:
                try:
                    entered = _apply(page, f, candidate, resume_path)
                    done = bool(entered)
                except FillProblem as e:
                    result.source = str(e)
                    _close_open_menus(page)
                except Exception as e:  # one stubborn field shouldn't stop the rest
                    result.source = f"couldn't fill: {e.__class__.__name__}"
                    _close_open_menus(page)
                if done:
                    used = candidate
                    break
            if done:
                result.value = "resume.pdf" if used == RESUME else entered
                result.source = ans.source if used == ans.value else f"{ans.source} (closest option)"
                report.filled.append(result)
                continue
            if not result.source:
                result.source = f"no option matching '{ans.value}'"
        elif not result.source:
            result.source = "no saved answer for this question"
        (report.needs_attention if required else report.skipped_optional).append(result)

    _final_check(page, fields, report)
    return fields


def _norm_label(label: str) -> str:
    return " ".join(label.replace("*", " ").lower().split())


def _final_check(page, fields: list[dict], report: FillReport) -> None:
    """Look at the form again the way the site will: any required question
    still empty (or rejected by the browser's own validation) goes under
    "needs you", so "ready to submit" is never claimed for a form that
    would bounce. Catches choices that didn't stick and missed questions."""
    _close_open_menus(page)
    ids = [f["id"] for f in fields if f.get("required") and f.get("id")
           and f["kind"] in ("text", "textarea", "select", "combobox", "file")]
    try:
        problems = page.evaluate(_CHECK_JS, ids)
    except Exception:
        return
    flagged = {_norm_label(f.label) for f in report.needs_attention}
    for p in problems:
        key = _norm_label(p.get("label", ""))
        if key in flagged:
            continue
        flagged.add(key)
        was = next((f for f in report.filled if _norm_label(f.label) == key), None)
        if was:
            report.filled.remove(was)
            was.source = f"filled '{was.value}' but the form still shows it as {p['why'].lower()} — check it"
            report.needs_attention.append(was)
        else:
            report.needs_attention.append(FieldResult(
                label=p.get("label", "") or "(unlabelled field)", kind="?", required=True,
                source=f"required — {p['why'].lower()}"))


def _apply(page, f: dict, value: str, resume_path: Path | None) -> str | None:
    """Enter one answer. Returns what the form now shows (the option text
    actually chosen, which can differ from the saved wording), or None."""
    kind = f["kind"]
    loc = lambda i: page.locator(f'[data-jp="{i}"]')  # noqa: E731
    if value == RESUME:
        if kind != "file" or not resume_path or not resume_path.exists():
            return None
        loc(f["id"]).set_input_files(str(resume_path))
        return RESUME
    if kind == "file":
        return None
    if kind in ("text", "textarea"):
        loc(f["id"]).fill(value)
        return value
    if kind == "select":
        choice = pick_option(value, f["options"])
        if not choice:
            return None
        loc(f["id"]).select_option(label=choice)
        return choice
    if kind in ("radio", "buttons"):
        choice = pick_option(value, [o["text"] for o in f["options"]])
        if not choice:
            return None
        opt = next(o for o in f["options"] if o["text"] == choice)
        loc(opt["id"]).check() if kind == "radio" else loc(opt["id"]).click()
        return choice
    if kind == "combobox":
        return _fill_combobox(page, loc(f["id"]), value, f.get("label", ""))
    if kind == "checkboxes":
        # Saved answer like "YouTube, Other" — tick each matching option
        wanted = [w.strip() for w in value.split(",") if w.strip()]
        texts = [o["text"] for o in f["options"]]
        picks = [pick_option(w, texts) for w in wanted]
        if not wanted or None in picks:
            return None
        for p in picks:
            loc(f["options"][texts.index(p)]["id"]).check()
        return ", ".join(picks)
    if kind == "checkbox":
        if value.lower() in ("yes", "true"):
            loc(f["id"]).check()
            return "Yes"
        return None
    return None


def _menu_options(page, box, wait_ms: int) -> tuple[Any, list[str], bool]:
    """Options of THIS dropdown's own list, waiting up to wait_ms for them.
    Returns (locator, texts, scoped). scoped=False means the box doesn't say
    which list is its own, so every visible option on the page is used."""
    deadline = wait_ms
    while True:
        texts = box.evaluate(_MENU_OPTIONS_JS)
        if texts:
            return page.locator("[data-jp-opt]"), texts, True
        if texts is None:                 # no list linked to this box — page-wide fallback
            opts = page.locator('[role="option"]:visible')
            loose = [t.strip() for t in opts.all_inner_texts()]
            if loose:
                return opts, loose, False
        if deadline <= 0:
            return None, [], texts is not None
        page.wait_for_timeout(200)
        deadline -= 200


def _fill_combobox(page, box, value: str, label: str) -> str:
    """Searchable or click-only dropdown (react-select and friends).

    1. Click it open and look for the option in its own list — click-only
       Yes/No dropdowns don't accept typing at all.
    2. If not there and the box accepts typing, type a short query (just the
       city for location searches) and wait for suggestions.
    3. Click the option, then check the dropdown really shows it.
    Raises FillProblem with what it saw when it can't.
    """
    is_place = bool(re.search(r"location|city|where", label, re.I))
    _close_open_menus(page)
    box.click()
    opts, texts, _ = _menu_options(page, box, 4000)
    choice = pick_option(value, texts)
    seen = texts

    typed = False
    if not choice and box.is_editable():
        typed = True
        query = value
        if is_place:
            query = value.split(",")[0]                  # "New York, NY" → "New York"
        elif value == "Decline to self-identify":
            query = "decline"
        box.fill(query)
        opts, texts, _ = _menu_options(page, box, 6000)
        seen = texts or seen
        choice = pick_option(value, texts) or (
            # location suggestions read "New York, NY, USA" — accept the first that starts with the city
            next((t for t in texts if t.lower().startswith(query.lower())), None) if is_place else None)

    if not choice:
        # Leave the question blank and the menu closed. Deleting the typed
        # search reopens react-select's menu (any input change does), so
        # always finish by closing it — a menu left open covers the
        # questions below it.
        if typed:
            box.press("ControlOrMeta+a")
            box.press("Delete")
        _close_open_menus(page)
        if not seen:
            raise FillProblem(f"the dropdown didn't show any options for '{value}'")
        shown = ", ".join(f"'{t}'" for t in seen[:6]) + (f" (+{len(seen) - 6} more)" if len(seen) > 6 else "")
        raise FillProblem(f"no option matching '{value}' — it offered {shown}")

    opts.nth(texts.index(choice)).click()
    page.wait_for_timeout(150)
    shown = box.evaluate(_COMBO_VALUE_JS)
    if not shown:
        _close_open_menus(page)
        raise FillProblem(f"picked '{choice}' but the dropdown didn't keep it")
    _close_open_menus(page)
    return choice


def _close_open_menus(page) -> None:
    """Close any dropdown left open so it can't cover the next question.
    Escape goes to the open box itself — pressed elsewhere it may be ignored."""
    for _ in range(3):
        expanded = page.locator('[aria-expanded="true"]')
        if not expanded.count():
            return
        try:
            expanded.first.press("Escape", timeout=1000)
        except Exception:
            page.keyboard.press("Escape")
        page.wait_for_timeout(150)


# ── Submitting ─────────────────────────────────────────────────────────

# What a site shows after it accepted an application. Only text that wasn't
# on the page before clicking counts (job pages often say "thank you for
# your interest" up front).
_CONFIRM_TEXT = re.compile(
    r"thank(s| you) for (applying|your application|submitting|applying to)|"
    r"application (was |has been )?(successfully )?(submitted|received|sent)|"
    r"we.ve received your application|successfully (applied|submitted)", re.I)
_CONFIRM_URL = re.compile(r"confirmation|thank|submitted|success", re.I)
_CODE_TEXT = re.compile(
    r"(security|verification) code|code (has been |was )?sent|enter the (\d+.character )?code|"
    r"check your (e-?mail|inbox) for (a|the) code", re.I)
_ERROR_TEXT = re.compile(
    r"there (was|were) (an? )?(error|problem)s?|please (fix|correct|complete)|"
    r"(is|are) required|can.t be blank|something went wrong|could not (be )?submit|"
    r"unable to submit|try again", re.I)
_SUBMIT_NAME = re.compile(r"^\s*(submit( your)?( application)?|send application|apply( now)?)\s*$", re.I)

_CHALLENGE_JS = r"""
() => [...document.querySelectorAll('iframe[src*="recaptcha"][src*="bframe"], iframe[src*="hcaptcha"], '
                                    + 'iframe[src*="challenges.cloudflare"]')]
  .some(f => {
    const r = f.getBoundingClientRect();
    const shown = f.checkVisibility ? f.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}) : true;
    return shown && r.width > 50 && r.height > 50;
  })
"""


def _new_match(rx: re.Pattern, text: str, before: str) -> str:
    """First match of rx in text that wasn't already on the page before."""
    for m in rx.finditer(text):
        if m.group(0).lower() not in before.lower():
            start = max(0, m.start() - 60)
            return " ".join(text[start:m.end() + 80].split())
    return ""


def _body_text(page) -> str:
    try:
        return page.inner_text("body", timeout=3000)
    except Exception:
        return ""                          # mid-navigation


def _submit_button(page):
    btn = page.get_by_role("button", name=_SUBMIT_NAME)
    for i in range(btn.count()):
        if btn.nth(i).is_visible():
            return btn.nth(i)
    loose = page.locator("form button[type=submit], form input[type=submit]")
    for i in range(loose.count()):
        if loose.nth(i).is_visible():
            return loose.nth(i)
    return None


def submit_application(page, report: FillReport, out_dir: Path,
                       ask_human: Callable[[str], bool] | None = None,
                       wait_seconds: int = 60) -> None:
    """Click Submit and wait for the site to confirm. A CAPTCHA challenge or
    an emailed verification code is handed to the person (ask_human shows
    the message and returns once they're done) — never worked around."""
    report.mode = "submit"
    btn = _submit_button(page)
    if btn is None:
        report.submit_note = "couldn't find the Submit button — nothing was sent"
        return
    before_url, before = page.url, _body_text(page)
    btn.scroll_into_view_if_needed()
    btn.click()

    asked: set[str] = set()
    waited, limit = 0, wait_seconds
    while waited < limit:
        page.wait_for_timeout(1000)
        waited += 1
        text, url = _body_text(page), page.url
        if (url != before_url and _CONFIRM_URL.search(url)) or _new_match(_CONFIRM_TEXT, text, before):
            report.submitted = True
            report.submit_note = "the site confirmed your application"
            break
        handoff = None
        if "code" not in asked and _new_match(_CODE_TEXT, text, before):
            handoff = ("code", "The site emailed you a verification code. Type it into the "
                               "browser and finish submitting there.")
        elif "captcha" not in asked and page.evaluate(_CHALLENGE_JS):
            handoff = ("captcha", "The site is showing a CAPTCHA. Solve it in the browser "
                                  "(and press Submit again if it asks).")
        if handoff:
            asked.add(handoff[0])
            if ask_human and ask_human(handoff[1]):
                limit = waited + 30            # give the site time to confirm after you
                continue
            report.submit_note = (f"stopped: the site wants a {handoff[0]} — "
                                  "run with --headed to handle it yourself")
            break
        if waited >= 8:
            problem = _new_match(_ERROR_TEXT, text, before)
            if problem:
                report.submit_note = f"the site reported a problem: “{problem[:160]}”"
                break
    else:
        report.submit_note = ("clicked Submit but saw no confirmation — check your email "
                              "and the screenshot before trying again")
    shot = out_dir / "submitted.png"
    try:
        page.screenshot(path=str(shot), full_page=True)
        report.screenshot = str(shot)
    except Exception:
        pass


def recheck(page, fields: list[dict]) -> list[str]:
    """Labels of required questions still empty right now (after you've
    had a chance to fill them in the browser)."""
    ids = [f["id"] for f in fields if f.get("required") and f.get("id")
           and f["kind"] in ("text", "textarea", "select", "combobox", "file")]
    try:
        return [p.get("label") or "(unlabelled field)" for p in page.evaluate(_CHECK_JS, ids)]
    except Exception:
        return ["(couldn't read the form)"]


def practice_apply(job: dict[str, Any], details: dict[str, str], resume_path: Path | None,
                   out_dir: Path, url: str | None = None, headless: bool = True,
                   saved: dict[str, str] | None = None,
                   on_filled: Callable[[FillReport], bool] | None = None,
                   ask_human: Callable[[str], bool] | None = None) -> FillReport:
    """Open the application, fill it, screenshot it.

    Submits only if on_filled(report) — called while the filled form is
    still open — returns True, and then only once every required question
    is answered."""
    from playwright.sync_api import sync_playwright, Error as PWError

    report = FillReport(job_id=job["id"], company=job.get("company", ""),
                        title=job.get("title", ""), url="", mode="practice")
    target = url or application_url(job)
    if not target:
        report.error = f"auto-apply doesn't support {job.get('platform')} yet"
        report.save(out_dir)
        return report
    report.url = target

    with sync_playwright() as pw:
        browser = _launch(pw, headless)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.set_default_timeout(8000)      # no 30-second hangs on one stubborn field
            response = page.goto(target, wait_until="domcontentloaded", timeout=45000)
            out_dir.mkdir(parents=True, exist_ok=True)
            shot = out_dir / "filled.png"
            if response is not None and response.status in (404, 410):
                report.closed = True
                report.error = f"job closed — the posting returns {response.status} (feeds can lag)"
                report.save(out_dir)
                return report
            if _redirected_away(target, page.url):
                report.closed = True
                report.error = "job closed — the site sends you back to its job list (feeds can lag)"
                report.save(out_dir)
                return report
            # Forms on Ashby/new Greenhouse are drawn by JavaScript after load
            try:
                page.wait_for_selector(
                    "form input:not([type=hidden]), form textarea, input[type=email], input[type=file]",
                    timeout=20000)
            except PWError:
                page.screenshot(path=str(shot), full_page=True)
                report.screenshot = str(shot)
                body = page.inner_text("body") if page.locator("body").count() else ""
                if _redirected_away(target, page.url) or _CLOSED_TEXT.search(body or ""):
                    report.closed = True
                    report.error = "job closed — the page says the posting is gone (feeds can lag)"
                else:
                    report.error = ("no application form on the page (it may need a click, a login, "
                                    "or the job closed) — see the screenshot")
                report.save(out_dir)
                return report
            page.wait_for_timeout(1500)          # let late widgets finish rendering
            report.captcha = bool(page.evaluate(_CAPTCHA_JS))
            fields = fill_form(page, details, resume_path, report, saved)
            if not report.filled and not report.needs_attention:
                report.error = "found the page but no questions it could read — see the screenshot"
            page.screenshot(path=str(shot), full_page=True)
            report.screenshot = str(shot)
            if on_filled and not report.error and on_filled(report):
                missing = recheck(page, fields)
                if missing:
                    report.submit_note = ("not sent — still empty: " + "; ".join(m[:60] for m in missing[:5]))
                else:
                    submit_application(page, report, out_dir, ask_human)
        except PWError as e:
            report.error = str(e).splitlines()[0][:300]
        finally:
            browser.close()
    report.save(out_dir)
    return report


def _redirected_away(target: str, final: str) -> bool:
    """Greenhouse answers a closed job by redirecting to the board with
    ?error=true; other boards drop you back on the job list."""
    if not final or final == target:
        return False
    if "error=true" in final:
        return True
    if "/jobs/" in target and "/jobs/" not in final:
        return True
    return False


def _launch(pw, headless: bool):
    """Playwright's own Chromium if installed, else the system Chrome."""
    try:
        return pw.chromium.launch(headless=headless)
    except Exception:
        return pw.chromium.launch(headless=headless, channel="chrome")
