"""Offline contracts for the deliberately read-only, secret-free VPS preflight."""
import importlib.util
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "vps_preflight.py"
spec = importlib.util.spec_from_file_location("funpayflow_vps_preflight", SCRIPT)
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


def test_service_status_uses_safe_systemctl_show(monkeypatch):
    seen = []

    def fake(command, **kwargs):
        seen.append(command)
        if command[-1] == "funpayflow.service":
            return "active"
        return "inactive"

    monkeypatch.setattr(check, "_execute", fake)
    assert check.service_status("funpayflow.service") == "active"
    assert check.service_status("funpay-bot.service") == "inactive"
    assert all(cmd[:4] == ["systemctl", "show", "--property=ActiveState", "--value"] for cmd in seen)


def test_git_state_never_returns_status_output_or_private_paths(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    seen = []

    def fake(cmd, **kwargs):
        seen.append(cmd)
        if "rev-parse" in cmd:
            return "a" * 40
        return " M docs/private_notes.txt\n"

    monkeypatch.setattr(check, "_execute", fake)
    assert check.git_state(tmp_path) == ("a" * 40, "dirty")
    assert check.git_state(tmp_path)[1] != " M docs/private_notes.txt"
    assert check.git_state(tmp_path / "absent") == ("missing", "unknown")
    assert all(isinstance(cmd, list) and cmd[0] == "git" for cmd in seen)


def test_runtime_count_does_not_print_process_arguments(tmp_path, capsys):
    for pid, argv in ((100, b"python3\0-m\0funpayflow.main\0SYNTHETIC_PRIVATE\0"),
                      (101, b"python3\0worker.py\0SYNTHETIC_PRIVATE\0"),
                      (102, b"python\0-m\0funpayflow.main\0")):
        p = tmp_path / str(pid)
        p.mkdir()
        (p / "cmdline").write_bytes(argv)
    assert check.runtime_count(tmp_path) == "multiple"
    assert "SYNTHETIC_PRIVATE" not in capsys.readouterr().out
    (tmp_path / "102" / "cmdline").write_bytes(b"python\0-m\0module\0")
    assert check.runtime_count(tmp_path) == "one"
    assert check.runtime_count(tmp_path / "notfound") == "unknown"


def test_read_only_sqlite_integrity_can_detect_corrupted_or_missing_database(tmp_path):
    assert check.sqlite_integrity(tmp_path) == "missing"
    db = tmp_path / "state.sqlite3"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE marker (value TEXT)")
        connection.execute("INSERT INTO marker VALUES ('SYNTHETIC_PRIVATE')")
    before = db.read_bytes()
    assert check.sqlite_integrity(tmp_path) == "ok"
    assert db.read_bytes() == before
    assert check.sqlite_integrity(tmp_path) == "ok"
    db.write_bytes(b"not a sqlite database")
    assert check.sqlite_integrity(tmp_path) in {"unknown", "failed"}


def test_report_redacts_secrets_and_only_uses_fixed_keys(tmp_path, monkeypatch, capsys):
    code = tmp_path / "code"
    data = tmp_path / "data"
    proc = tmp_path / "proc"
    code.mkdir()
    data.mkdir()
    proc.mkdir()
    (data / ".env").write_text("BOT_TOKEN=SYNTHETIC_PRIVATE\n", encoding="utf-8")
    (data / "bot_settings.json").write_text('{"secret": "SYNTHETIC_PRIVATE"}', encoding="utf-8")
    monkeypatch.setattr(check, "git_state", lambda _: ("a" * 40, "clean"))
    monkeypatch.setattr(check, "runtime_count", lambda _: "one")
    monkeypatch.setattr(check, "sqlite_integrity", lambda _: "ok")
    monkeypatch.setattr(check, "service_status", lambda unit: "active" if unit == "funpayflow.service" else "inactive")
    monkeypatch.setattr(check, "CODE_DIR", code)
    monkeypatch.setattr(check, "DATA_DIR", data)
    monkeypatch.setattr(check, "PROC", proc)
    assert check.main() == 0
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["service"] == "active"
    assert report["legacy_service"] == "inactive"
    assert report["main_runtime_processes"] == "one"
    assert report["sqlite"] == "ok"
    assert report["deployment_performed"] is False
    assert report["read_only"] is True
    assert "SYNTHETIC_PRIVATE" not in output
    assert str(tmp_path) not in output
    assert set(report) == {
        "report", "read_only", "service", "legacy_service",
        "main_runtime_processes", "code_commit", "tracked_worktree",
        "data_directory", "env_file", "settings_file", "sqlite",
        "disk_free", "deployment_performed"
    }


def test_unknown_primary_service_blocks_success(monkeypatch, capsys):
    monkeypatch.setattr(check, "inspect", lambda: {"service": "unknown", "read_only": True})
    assert check.main() == 2
    assert json.loads(capsys.readouterr().out)["service"] == "unknown"
