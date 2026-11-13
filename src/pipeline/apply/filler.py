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
    const r = el.getBoundingClientRect(), st = getComputedStyle(el);
    return st.display !== 'none' && st.visibility !== 'hidden' && (r.width > 0 || r.height > 0);
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
      out.push({kind: 'checkbox', id: tag(el), label, required: required(el, label)});
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


def fill_form(page, details: dict[str, str], resume_path: Path | None,
              report: FillReport) -> None:
    """Fill every question on an already-open page; records results in report."""
    fields = page.evaluate(_ENUMERATE_JS)
    for f in fields:
        label, kind, required = f.get("label", ""), f["kind"], bool(f.get("required"))
        result = FieldResult(label=label, kind=kind, required=required)

        if kind == "checkbox" and is_consent(label):
            result.source = "legal consent — read and tick it yourself"
            (report.needs_attention if required else report.skipped_optional).append(result)
            continue

        ans = answer_for(label, details)
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
        box = page.locator(f'[data-jp="{f["id"]}"]')
        box.click()
        box.fill(value if value != "Decline to self-identify" else "decline")
        options = page.locator('[role="option"]')
        options.first.wait_for(timeout=3000)
        texts = options.all_inner_texts()
        choice = pick_option(value, texts)
        if not choice:
            box.press("Escape")
            box.fill("")
            return False
        options.nth(texts.index(choice)).click()
        return True
    if kind == "checkbox":
        if value.lower() in ("yes", "true"):
            page.locator(f'[data-jp="{f["id"]}"]').check()
            return True
        return False
    return False


def practice_apply(job: dict[str, Any], details: dict[str, str], resume_path: Path | None,
                   out_dir: Path, url: str | None = None, headless: bool = True) -> FillReport:
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
            page.goto(target, wait_until="networkidle", timeout=45000)
            fill_form(page, details, resume_path, report)
            out_dir.mkdir(parents=True, exist_ok=True)
            shot = out_dir / "filled.png"
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
