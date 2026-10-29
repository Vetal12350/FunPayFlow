"""Offline checks for the optional no-console UI launcher.

A real Windows 11/WSH startup smoke test remains necessary before rollout.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_silent_launcher_uses_wscript_and_pythonw_without_worker_spawn():
    code = (ROOT / "scripts/start_control_center_silent.vbs").read_text(encoding="utf-8")
    assert "pythonw.exe" in code
    assert "control_center_launcher.pyw" in code
    assert "WScript.Shell" in code
    assert "shell.Run cmd, 1, False" in code
    assert "codex_issue_worker.py" not in code
    assert "start_codex_worker.ps1" not in code


def test_error_entrypoint_is_only_a_gui_entrypoint():
    code = (ROOT / "scripts/control_center_launcher.pyw").read_text(encoding="utf-8")
    assert "import control_center" in code
    assert "control_center.main()" in code
    assert "MessageBoxW" in code
    assert "subprocess" not in code
    assert "codex_issue_worker.py" not in code


def test_diagnostic_launcher_is_preserved():
    text = (ROOT / "scripts/start_control_center.bat").read_text(encoding="utf-8")
    assert "control_center.py" in text
    assert "python.exe" in text
