"""Encrypted save/restore of the pipeline's databases.

Scheduled runs happen on GitHub Actions, which starts from a blank machine
every time. Between runs the databases (board registry, job tracker, H-1B
cache) are saved to the repo's `pipeline-data` branch. The repo is public,
so they're encrypted first (Fernet: AES-128-CBC + HMAC) with a key that only
lives in your GitHub secrets and on your laptop.

Key lookup order: $JOBPIPE_STATE_KEY, then ~/.config/jobpipe/state_key.
"""

from __future__ import annotations

import io
import os
import shutil
import tarfile
from datetime import datetime
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"
STATE_FILES = ("registry.db", "tracker.db", "h1b_cache.db")
KEY_FILE = Path.home() / ".config" / "jobpipe" / "state_key"
KEEP_BACKUPS = 3


class StateError(Exception):
    pass


def new_key() -> str:
    return Fernet.generate_key().decode()


def load_key() -> str:
    key = os.environ.get("JOBPIPE_STATE_KEY", "").strip()
    if not key and KEY_FILE.exists():
        key = KEY_FILE.read_text().strip()
    if not key:
        raise StateError(
            "No state key. Run `jobpipe state-key --save` once, and add the printed key "
            "to GitHub → Settings → Secrets → Actions as JOBPIPE_STATE_KEY."
        )
    return key


def save_key(key: str) -> Path:
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_text(key + "\n")
    KEY_FILE.chmod(0o600)
    return KEY_FILE


def export_state(key: str, data_dir: Path = DATA_DIR) -> bytes:
    """Pack the databases that exist into one encrypted blob."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in STATE_FILES:
            path = data_dir / name
            if path.exists():
                tar.add(path, arcname=name)
    return Fernet(key.encode()).encrypt(buf.getvalue())


def import_state(blob: bytes, key: str, data_dir: Path = DATA_DIR) -> list[str]:
    """Decrypt a blob and restore its databases into data_dir.

    Existing databases are moved to data/backup-<timestamp>/ first.
    Returns the restored file names.
    """
    try:
        raw = Fernet(key.encode()).decrypt(blob)
    except (InvalidToken, ValueError) as e:
        raise StateError("Couldn't decrypt the saved state — wrong JOBPIPE_STATE_KEY?") from e

    data_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        members = [m for m in tar.getmembers()
                   if m.isfile() and m.name in STATE_FILES]  # never trust paths in the archive
        existing = [data_dir / m.name for m in members if (data_dir / m.name).exists()]
        if existing:
            backup = data_dir / f"backup-{datetime.now():%Y%m%d-%H%M%S-%f}"
            backup.mkdir()
            for path in existing:
                shutil.move(str(path), backup / path.name)
            for old in sorted(data_dir.glob("backup-*"))[:-KEEP_BACKUPS]:
                shutil.rmtree(old, ignore_errors=True)
        for member in members:
            src = tar.extractfile(member)
            (data_dir / member.name).write_bytes(src.read())
    return [m.name for m in members]
