"""Tests for encrypted state export/import (cloud runs)."""

import io
import tarfile

import pytest
from cryptography.fernet import Fernet

from pipeline.state import StateError, export_state, import_state, new_key


@pytest.fixture
def data(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    (d / "registry.db").write_bytes(b"registry-v1")
    (d / "tracker.db").write_bytes(b"tracker-v1")
    return d


def test_round_trip(data, tmp_path):
    key = new_key()
    blob = export_state(key, data)
    assert b"registry-v1" not in blob and b"tracker-v1" not in blob   # actually encrypted

    dest = tmp_path / "restore"
    restored = import_state(blob, key, dest)
    assert sorted(restored) == ["registry.db", "tracker.db"]
    assert (dest / "tracker.db").read_bytes() == b"tracker-v1"


def test_wrong_key_refused(data, tmp_path):
    blob = export_state(new_key(), data)
    with pytest.raises(StateError, match="wrong JOBPIPE_STATE_KEY"):
        import_state(blob, new_key(), tmp_path / "x")


def test_existing_databases_backed_up_and_pruned(data):
    key = new_key()
    for i in range(5):
        (data / "tracker.db").write_bytes(f"local-{i}".encode())
        import_state(export_state(key, data), key, data)
    backups = sorted(data.glob("backup-*"))
    assert len(backups) == 3                                      # only the newest kept
    assert (backups[-1] / "tracker.db").read_bytes() == b"local-4"


def test_archive_paths_outside_data_dir_ignored(tmp_path):
    key = new_key()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, payload in [("../evil.sh", b"rm -rf"), ("tracker.db", b"ok")]:
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    blob = Fernet(key.encode()).encrypt(buf.getvalue())

    dest = tmp_path / "data"
    assert import_state(blob, key, dest) == ["tracker.db"]
    assert not (tmp_path / "evil.sh").exists()
