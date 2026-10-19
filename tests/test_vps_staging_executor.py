"""Offline tests for the permitted no-op fallback, never a real staging run."""

import builtins
import importlib.util
import inspect
import io
import os
from pathlib import Path
import socket
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "vps_staging_executor.py"
SPEC = importlib.util.spec_from_file_location("vps_staging_executor", SCRIPT)
executor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(executor)
SHA = "a" * 40
SENTINEL = "SYNTHETIC_PRIVATE_SENTINEL"


@pytest.fixture
def boundary(monkeypatch):
    """Forbid all executor IO; do not touch /root, even on Windows CI."""
    output = io.StringIO()
    errors = io.StringIO()
    forbidden = Mock(side_effect=AssertionError("Forbidden IO " + SENTINEL))

    def guarded(original):
        # pytest itself needs stat/open while reporting tests. Forbid IO only
        # while the executor is on the stack, including calls through helpers.
        def check(*args, **kwargs):
            frame = inspect.currentframe()
            try:
                while frame is not None:
                    if frame.f_globals is executor.__dict__:
                        return forbidden(*args, **kwargs)
                    frame = frame.f_back
            finally:
                del frame
            return original(*args, **kwargs)
        return check
    monkeypatch.setattr(executor, "sys", SimpleNamespace(
        platform="linux", argv=["executor"], stdout=output, stderr=errors))
    monkeypatch.setattr(executor, "os", SimpleNamespace(
        getuid=Mock(return_value=0), geteuid=Mock(return_value=0),
        environ={"SSH_ORIGINAL_COMMAND": "stage " + SHA}))
    for obj, names in (
        (subprocess, ("run", "Popen", "call", "check_call", "check_output")),
        (os, ("system", "popen", "open", "mkdir", "makedirs", "remove", "unlink",
              "rmdir", "rename", "replace", "stat", "lstat", "readlink", "scandir")),
        (Path, ("open", "mkdir", "unlink", "rmdir", "rename", "replace", "stat",
                "lstat", "readlink", "iterdir", "glob", "rglob")),
        (builtins, ("open",)),
        (io, ("open",)),
        (socket, ("socket", "create_connection", "getaddrinfo")),
    ):
        for name in names:
            monkeypatch.setattr(obj, name, guarded(getattr(obj, name)))
    yield SimpleNamespace(output=output, errors=errors, forbidden=forbidden)
    forbidden.assert_not_called()


def test_valid_request_is_explicitly_not_staged(boundary):
    assert executor.stage(SHA) == executor.DISABLED
    assert executor.main([]) == 1
    assert boundary.output.getvalue() == executor.DISABLED + "\n"
    assert boundary.errors.getvalue() == ""


@pytest.mark.parametrize("sha", [
    None, True, 1, [], {}, b"a" * 40, "", "a" * 39, "a" * 41, "A" * 40,
    "g" * 40, "main", SHA + "\n", SHA + " ", " " + SHA, "../" + SHA,
    "$(id)", "; reboot", "stage " + SHA, SENTINEL,
])
def test_invalid_sha_rejected_before_context(boundary, monkeypatch, sha):
    guard = Mock(side_effect=AssertionError("Context must not be queried"))
    monkeypatch.setattr(executor, "_root_linux", guard)
    assert executor.stage(sha) == executor.INVALID_SHA
    guard.assert_not_called()


def test_sha_subclass_cannot_supply_behavior(boundary):
    class Untrusted(str):
        def __str__(self):
            raise AssertionError(SENTINEL)

    assert executor.stage(Untrusted(SHA)) == executor.INVALID_SHA


@pytest.mark.parametrize("platform,uid,euid", [
    ("win32", 0, 0), ("darwin", 0, 0), ("linux", 1000, 1000),
    ("linux", 0, 1000), ("linux", 1000, 0),
])
def test_wrong_os_or_owner_fails_closed(boundary, platform, uid, euid):
    executor.sys.platform = platform
    executor.os.getuid.return_value = uid
    executor.os.geteuid.return_value = euid
    assert executor.stage(SHA) == executor.INVALID_CONTEXT
    assert executor.main([]) == 1
    assert boundary.output.getvalue() == executor.INVALID_CONTEXT + "\n"


@pytest.mark.parametrize("command", [
    None, "", SHA, "stage", "stage main", "stage " + "A" * 40,
    "stage " + SHA + "\n", "stage " + SHA + " ", " stage " + SHA,
    "stage  " + SHA, "stage\t" + SHA, "stage " + SHA + ";id",
    "stage " + SHA + " && id", "stage " + SHA + " --plan", "deploy " + SHA,
    "env SSH_ORIGINAL_COMMAND='stage " + SHA + "'", SENTINEL,
])
def test_ssh_command_is_exact_and_never_echoed(boundary, command):
    executor.os.environ["SSH_ORIGINAL_COMMAND"] = command
    assert executor.main([]) == 1
    assert boundary.output.getvalue() == executor.INVALID_COMMAND + "\n"


@pytest.mark.parametrize("args", [
    [SHA], ["stage", SHA], ["--plan"], ["--help"], ["--sha", SHA],
    ["--command", SENTINEL], ["--path", "/root/FunPayFlow-data"],
])
def test_cli_cannot_select_commands_paths_or_activation(boundary, args):
    assert executor.main(args) == 1
    assert boundary.output.getvalue() == executor.INVALID_COMMAND + "\n"


def test_actual_argv_and_missing_ssh_command_rejected(boundary):
    executor.sys.argv = ["executor", SENTINEL]
    assert executor.main() == 1
    executor.sys.argv = ["executor"]
    executor.os.environ.clear()
    assert executor.main() == 1
    assert boundary.output.getvalue() == (executor.INVALID_COMMAND + "\n") * 2


@pytest.mark.parametrize("name", [
    "GIT_CONFIG_COUNT", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_DIR",
    "GIT_WORK_TREE", "GIT_SSH_COMMAND", "LD_PRELOAD", "PYTHONPATH", "PYTHONHOME",
    "UV_PROJECT_ENVIRONMENT", "UV_CACHE_DIR", "UV_INDEX_URL", "BASH_ENV", "PATH",
    "HOME", "FUNPAY_BOT_DATA_DIR", "SSH_AUTH_SOCK",
])
def test_environment_injection_cannot_enable_any_execution(boundary, name):
    executor.os.environ[name] = SENTINEL
    assert executor.main([]) == 1
    assert boundary.output.getvalue() == executor.DISABLED + "\n"
    assert executor.os.environ[name] == SENTINEL


@pytest.mark.parametrize("error", [
    OSError(SENTINEL), RuntimeError(SENTINEL), PermissionError(SENTINEL),
    subprocess.TimeoutExpired(SENTINEL, 1, output=SENTINEL, stderr=SENTINEL),
    subprocess.CalledProcessError(1, SENTINEL, output=SENTINEL, stderr=SENTINEL),
])
def test_context_errors_are_redacted_without_retry(boundary, error):
    executor.os.getuid.side_effect = error
    assert executor.stage(SHA) == executor.FAILED
    executor.os.getuid.reset_mock()
    assert executor.main([]) == 1
    executor.os.getuid.assert_called_once_with()
    assert boundary.output.getvalue() == executor.FAILED + "\n"
    assert boundary.errors.getvalue() == ""


@pytest.mark.parametrize("error", [RuntimeError(SENTINEL), KeyboardInterrupt(SENTINEL)])
def test_cli_redacts_unexpected_exceptions(boundary, monkeypatch, error):
    monkeypatch.setattr(executor, "stage", Mock(side_effect=error))
    assert executor.main([]) == 1
    assert boundary.output.getvalue() == executor.FAILED + "\n"


@pytest.mark.parametrize("value", [SENTINEL, None, [], {"status": SENTINEL}])
def test_reports_cannot_escape_status_allowlist(boundary, monkeypatch, value):
    monkeypatch.setattr(executor, "stage", Mock(return_value=value))
    assert executor.main([]) == 1
    assert boundary.output.getvalue() == executor.FAILED + "\n"


def test_no_claim_of_runtime_or_stage_checks(boundary):
    # Even a root-shaped request is refused before inspecting any real service,
    # PID/cwd, old/partial/symlink stage, disk space, uv, or upstream main.
    # There is no fabricated happy staging path, subprocess timeout or retry.
    assert executor.stage(SHA) == "NOT_STAGED_ISOLATION_REQUIRED"
    assert not hasattr(executor, "deploy")
    assert not hasattr(executor, "subprocess")
    assert not hasattr(executor, "Path")
    assert all("SUCCESS" not in status for status in executor.STATUSES)
