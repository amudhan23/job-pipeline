"""Copy your private setup from this computer to your jobpipe in the cloud.

One zip holds exactly the files below (nothing else is accepted on the
server side, and nothing can be written outside data/private), plus the
list of job boards you watch so the cloud crawler starts with all of them.
"""

from __future__ import annotations

import io
import json
import os
import zipfile
from pathlib import Path
from typing import Any

# What a cloud copy needs to work on its own.
FILES = [
    "applicant.json", "resume.pdf", "career_profile.md", "answers.json", "pending_questions.json",
    "ai_drafts.json", "autoapply.json", "approvals.json", "ai_settings.json", "telegram.json",
    "telegram_state.json", "mail.json", "outreach.json", "searches.json",
]
SECRETS = ["anthropic_key", "hunter_key"]
BOARDS = "boards.json"
MAX_BYTES = 30 * 1024 * 1024


def _pdir(private_dir: Path | None) -> Path:
    from pipeline import applicant
    return private_dir or applicant.PRIVATE_DIR


def build(private_dir: Path | None = None, with_secrets: bool = True,
          registry: Any = None) -> tuple[bytes, list[str]]:
    """The zip to send, and the names it contains."""
    pdir = _pdir(private_dir)
    names = []
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in FILES + (SECRETS if with_secrets else []):
            p = pdir / name
            if p.is_file():
                z.write(p, name)
                names.append(name)
        try:
            if registry is None:
                from pipeline.discovery.registry import BoardRegistry
                registry = BoardRegistry()
            with registry._conn() as conn:
                rows = conn.execute("SELECT platform, slug FROM boards WHERE fail_count < 3").fetchall()
            if rows:
                z.writestr(BOARDS, json.dumps([{"platform": r[0], "slug": r[1]} for r in rows]))
                names.append(f"{BOARDS} ({len(rows)} boards)")
        except Exception:
            pass
    return buf.getvalue(), names


def apply(data: bytes, private_dir: Path | None = None, registry: Any = None) -> dict[str, Any]:
    """Unpack a zip from `build` into data/private. Unknown names are refused."""
    if len(data) > MAX_BYTES:
        raise ValueError("That bundle is too large.")
    pdir = _pdir(private_dir)
    pdir.mkdir(parents=True, exist_ok=True)
    allowed = set(FILES) | set(SECRETS)
    written, boards = [], 0
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("That isn't a jobpipe bundle.")
    with z:
        infos = z.infolist()
        bad = [i.filename for i in infos if i.filename not in allowed and i.filename != BOARDS]
        if bad:
            raise ValueError(f"Unexpected file in the bundle: {bad[0][:60]}")
        if sum(i.file_size for i in infos) > 4 * MAX_BYTES:
            raise ValueError("That bundle unpacks too large.")
        for info in infos:
            content = z.read(info)
            if info.filename == BOARDS:
                items = json.loads(content)
                if registry is None:
                    from pipeline.discovery.registry import BoardRegistry
                    registry = BoardRegistry()
                for b in items[:20000]:
                    if isinstance(b, dict) and b.get("platform") and b.get("slug"):
                        boards += bool(registry.add(str(b["platform"])[:30], str(b["slug"])[:120], "sync"))
                continue
            target = pdir / info.filename
            tmp = target.with_name(target.name + ".sync")
            tmp.write_bytes(content)
            os.chmod(tmp, 0o600)
            tmp.replace(target)
            written.append(info.filename)
    return {"written": written, "boards_added": boards}
