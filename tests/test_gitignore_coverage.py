"""Offline: private runtime data, backups and key material must stay untracked."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]

IGNORED = [
    ".env",
    "sub/.env",
    ".env.production",
    "bot_settings.json",
    "nested/bot_settings.json",
    "state.sqlite3",
    "state.sqlite3-wal",
    "nested/state.sqlite3",
    "nested/state.sqlite3-shm",
    "other.db",
    "logs/app.log",
    "nested/logs/app.log",
    "exports/orders.json",
    "orders-export.csv",
    "backups/private-data.tar.gz",
    "nested/backups/x.zip",
    "funpayflow-backup-2026.tar.gz",
    "state.sqlite3.bak",
    "deploy.pem",
    "id_ed25519",
    "id_ed25519.pub",
    "id_rsa",
]

TRACKED = [
    ".env.example",
    "tests/fixtures/sample.csv",
    "scripts/vps_backup_create.py",
    "docs/VPS_BACKUP_CREATE.md",
    "src/funpayflow/state.py",
]


def _git():
    git = shutil.which("git")
    if git is None or not (ROOT / ".git").exists():
        pytest.skip("source checkout with git required")
    return git


def _ignored(git, path):
    result = subprocess.run(
        [git, "-C", str(ROOT), "check-ignore", "-q", "--no-index", "--", path],
        capture_output=True, timeout=30)
    assert result.returncode in (0, 1), result.stderr
    return result.returncode == 0


@pytest.mark.parametrize("path", IGNORED)
def test_private_paths_are_ignored(path):
    assert _ignored(_git(), path)


@pytest.mark.parametrize("path", TRACKED)
def test_source_paths_stay_trackable(path):
    assert not _ignored(_git(), path)


def test_no_private_files_are_tracked():
    git = _git()
    files = subprocess.run(
        [git, "-C", str(ROOT), "ls-files", "-z"],
        capture_output=True, check=True, timeout=30).stdout.decode().split("\0")
    leaked = [f for f in files if f and f != ".env.example" and _ignored(git, f)]
    assert leaked == []
