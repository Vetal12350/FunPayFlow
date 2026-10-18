"""Read-only, secret-free VPS preflight for an EXISTING FunPayFlow installation.

Run on the authorized VPS via SSH after verifying its host key.
Never connects to FunPay or Telegram; never starts, stops or restarts services.
Does not print process command lines, environment variables, private paths or logs.
"""
from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess

CODE_DIR = Path("/root/FunPayFlow-next")
DATA_DIR = Path("/root/FunPayFlow-data")
PROC = Path("/proc")
MAX_DB_FOR_QUICK_CHECK = 256 * 1024 * 1024


def _execute(command: list[str], *, timeout: int = 5) -> str | None:
    try:
        result = subprocess.run(command, text=True, encoding="utf-8",
                                errors="replace", capture_output=True,
                                timeout=timeout, check=False, shell=False)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def service_status(unit: str) -> str:
    status = _execute(["systemctl", "show", "--property=ActiveState", "--value", unit])
    return status if status in {"active", "inactive", "failed", "activating", "deactivating"} else "unknown"


def git_state(code: Path) -> tuple[str, str]:
    if not (code / ".git").exists():
        return "missing", "unknown"
    commit = _execute(["git", "-C", str(code), "rev-parse", "--verify", "HEAD"])
    if not commit or len(commit) != 40 or any(ch not in "0123456789abcdef" for ch in commit):
        return "unknown", "unknown"
    status = _execute(["git", "-C", str(code), "status", "--porcelain=v1", "--untracked-files=no"])
    return commit, "unknown" if status is None else ("dirty" if status else "clean")


def runtime_count(proc: Path = PROC) -> str:
    if not proc.is_dir():
        return "unknown"
    count = 0
    try:
        entries = list(proc.iterdir())
    except OSError:
        return "unknown"
    for entry in entries:
        if not entry.name.isdecimal():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except (OSError, PermissionError):
            continue
        # Match the expected package entrypoint only. Never print argv.
        if b"-m" in argv and b"funpayflow.main" in argv:
            count += 1
    return "none" if count == 0 else ("one" if count == 1 else "multiple")


def sqlite_integrity(data: Path) -> str:
    db = data / "state.sqlite3"
    if not db.is_file():
        return "missing"
    try:
        if db.stat().st_size > MAX_DB_FOR_QUICK_CHECK:
            return "skipped_large"
        uri = db.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=3)) as connection:
            connection.execute("PRAGMA query_only=ON")
            result = connection.execute("PRAGMA quick_check").fetchone()
        return "ok" if result == ("ok",) else "failed"
    except (OSError, sqlite3.Error):
        return "unknown"


def inspect(code: Path | None = None, data: Path | None = None,
            proc: Path | None = None) -> dict[str, object]:
    # Resolve defaults at call time so offline tests never touch /root.
    code = CODE_DIR if code is None else code
    data = DATA_DIR if data is None else data
    proc = PROC if proc is None else proc
    current, worktree = git_state(code)
    disk_available = "unknown"
    try:
        disk = os.statvfs(str(code if code.exists() else Path("/")))
        disk_available = "at_least_512_mb" if disk.f_bavail * disk.f_frsize >= 512 * 1024 * 1024 else "under_512_mb"
    except (OSError, AttributeError):
        pass
    return {
        "report": "funpayflow_vps_preflight_v1",
        "read_only": True,
        "service": service_status("funpayflow.service"),
        "legacy_service": service_status("funpay-bot.service"),
        "main_runtime_processes": runtime_count(proc),
        "code_commit": current,
        "tracked_worktree": worktree,
        "data_directory": "present" if data.is_dir() else "missing",
        "env_file": "present" if (data / ".env").is_file() else "missing",
        "settings_file": "present" if (data / "bot_settings.json").is_file() else "missing",
        "sqlite": sqlite_integrity(data),
        "disk_free": disk_available,
        "deployment_performed": False,
    }


def main() -> int:
    # This program intentionally has no arguments, secrets, deployment or write paths.
    report = inspect()
    print(json.dumps(report, ensure_ascii=True, sort_keys=True))
    return 0 if report["service"] != "unknown" else 2


if __name__ == "__main__":
    raise SystemExit(main())
