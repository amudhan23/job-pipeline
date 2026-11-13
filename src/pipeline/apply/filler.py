"""Fill a job application form in a real (headless) browser.

How it works:
  1. Open the job's application page (Greenhouse, Lever or Ashby).
  2. A script in the page lists every question with the text a person sees
     next to it — not internal field names, which differ per site and change.
  3. answers.py picks the answer from your saved details.
  4. Each control is filled the way a person would: type, choose a dropdown
     option, click a radio / Yes-No button, attach the resume.
  5. Practice mode: full-page screenshot + report, and it never submits.

Anything it can't answer, plus every legal consent box, is listed under
"needs your attention" instead of being guessed.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from pipeline.apply.answers import RESUME, answer_for, is_consent, pick_option

# Lists the form's questions. Every control gets a data-jp attribute so
# Python can find it again. Returns one entry per question.
_ENUMERATE_JS = r"""
() => {
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
  const labelOf = el => {
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
                                  || /\*\s*$/.test(label) || /\(required\)/i.test(label);

  const out = []; let n = 0;
  const tag = el => { const id = 'jp' + (n++); el.setAttribute('data-jp', id); return id; };
  const seenRadio = new Set();
  const seenBox = new Set();

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

  for (const el of document.querySelectorAll('input, textarea, select')) {
    const type = (el.type || el.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'reset', 'image', 'search'].includes(type)) continue;
    if (!visible(el) || el.disabled) continue;

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
      let group = el.name ? [...document.querySelectorAll(`input[type=checkbox][name="${CSS.escape(el.name)}"]`)] : [];
      if (group.length < 2) {
        const fs = el.closest('fieldset');
        group = fs ? [...fs.querySelectorAll('input[type=checkbox]')] : [el];
      }
      group = group.filter(visible);
      if (group.length >= 2) {
        group.forEach(g => seenBox.add(g));
        const gl = groupLabel(group);
        out.push({kind: 'checkboxes', label: gl, required: /\*\s*$/.test(gl),
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

    @property
    def ready_to_submit(self) -> bool:
        return not self.error and not self.needs_attention and bool(self.filled)

    def save(self, folder: Path) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "report.json"
        path.write_text(json.dumps({**asdict(self), "ready_to_submit": self.ready_to_submit}, indent=2))
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
              report: FillReport, saved: dict[str, str] | None = None) -> None:
    """Fill every question on an already-open page; records results in report."""
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
        done = False
        if ans:
            try:
                done = _apply(page, f, ans.value, resume_path)
            except Exception as e:  # one stubborn field shouldn't stop the rest
                result.source = f"couldn't fill: {e.__class__.__name__}"
            if done:
                result.value = "resume.pdf" if ans.value == RESUME else ans.value
                result.source = ans.source
                report.filled.append(result)
                continue
            if not result.source:
                result.source = f"no option matching '{ans.value}'"
        elif not result.source:
            result.source = "no saved answer for this question"
        (report.needs_attention if required else report.skipped_optional).append(result)


def _apply(page, f: dict, value: str, resume_path: Path | None) -> bool:
    kind = f["kind"]
    if value == RESUME:
        if kind != "file" or not resume_path or not resume_path.exists():
            return False
        page.locator(f'[data-jp="{f["id"]}"]').set_input_files(str(resume_path))
        return True
    if kind == "file":
        return False
    if kind in ("text", "textarea"):
        page.locator(f'[data-jp="{f["id"]}"]').fill(value)
        return True
    if kind == "select":
        choice = pick_option(value, f["options"])
        if not choice:
            return False
        page.locator(f'[data-jp="{f["id"]}"]').select_option(label=choice)
        return True
    if kind in ("radio", "buttons"):
        choice = pick_option(value, [o["text"] for o in f["options"]])
        if not choice:
            return False
        opt = next(o for o in f["options"] if o["text"] == choice)
        target = page.locator(f'[data-jp="{opt["id"]}"]')
        target.check() if kind == "radio" else target.click()
        return True
    if kind == "combobox":
        return _fill_combobox(page, page.locator(f'[data-jp="{f["id"]}"]'), value, f.get("label", ""))
    if kind == "checkboxes":
        # Saved answer like "YouTube, Other" — tick each matching option
        wanted = [w.strip() for w in value.split(",") if w.strip()]
        texts = [o["text"] for o in f["options"]]
        picks = [pick_option(w, texts) for w in wanted]
        if not wanted or None in picks:
            return False
        for p in picks:
            page.locator(f'[data-jp="{f["options"][texts.index(p)]["id"]}"]').check()
        return True
    if kind == "checkbox":
        if value.lower() in ("yes", "true"):
            page.locator(f'[data-jp="{f["id"]}"]').check()
            return True
        return False
    return False


def _visible_options(page) -> tuple[Any, list[str]]:
    opts = page.locator('[role="option"]:visible')
    return opts, [t.strip() for t in opts.all_inner_texts()]


def _fill_combobox(page, box, value: str, label: str) -> bool:
    """Searchable or click-only dropdown (react-select and friends).

    1. Click it open and look for the option among those shown — click-only
       Yes/No dropdowns don't accept typing at all.
    2. If not there and the box accepts typing, type a short query (just the
       city for location searches) and wait for suggestions.
    """
    box.click()
    try:
        page.locator('[role="option"]:visible').first.wait_for(timeout=4000)
    except Exception:
        pass
    opts, texts = _visible_options(page)
    choice = pick_option(value, texts)

    if not choice and box.is_editable():
        query = value
        if re.search(r"location|city|where", label, re.I):
            query = value.split(",")[0]                  # "New York, NY" → "New York"
        elif value == "Decline to self-identify":
            query = "decline"
        box.fill(query)
        try:
            page.locator('[role="option"]:visible').first.wait_for(timeout=6000)
        except Exception:
            pass
        opts, texts = _visible_options(page)
        choice = pick_option(value, texts) or (
            # location suggestions read "New York, NY, USA" — accept the first that starts with the city
            next((t for t in texts if t.lower().startswith(query.lower())), None)
            if re.search(r"location|city|where", label, re.I) else None)

    if not choice:
        box.press("Escape")
        if box.is_editable():
            box.fill("")
        return False
    opts.nth(texts.index(choice)).click()
    return True


def practice_apply(job: dict[str, Any], details: dict[str, str], resume_path: Path | None,
                   out_dir: Path, url: str | None = None, headless: bool = True,
                   saved: dict[str, str] | None = None) -> FillReport:
    """Open the application, fill it, screenshot it. Never submits."""
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
            page.goto(target, wait_until="domcontentloaded", timeout=45000)
            out_dir.mkdir(parents=True, exist_ok=True)
            shot = out_dir / "filled.png"
            # Forms on Ashby/new Greenhouse are drawn by JavaScript after load
            try:
                page.wait_for_selector(
                    "form input:not([type=hidden]), form textarea, input[type=email], input[type=file]",
                    timeout=20000)
            except PWError:
                page.screenshot(path=str(shot), full_page=True)
                report.screenshot = str(shot)
                report.error = ("no application form on the page (it may need a click, a login, "
                                "or the job closed) — see the screenshot")
                report.save(out_dir)
                return report
            page.wait_for_timeout(1500)          # let late widgets finish rendering
            fill_form(page, details, resume_path, report, saved)
            if not report.filled and not report.needs_attention:
                report.error = "found the page but no questions it could read — see the screenshot"
            page.screenshot(path=str(shot), full_page=True)
            report.screenshot = str(shot)
        except PWError as e:
            report.error = str(e).splitlines()[0][:300]
        finally:
            browser.close()
    report.save(out_dir)
    return report


def _launch(pw, headless: bool):
    """Playwright's own Chromium if installed, else the system Chrome."""
    try:
        return pw.chromium.launch(headless=headless)
    except Exception:
        return pw.chromium.launch(headless=headless, channel="chrome")
