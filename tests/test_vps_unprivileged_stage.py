"""Offline contract tests: no sandbox, Git, uv, services or production IO."""
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


SPEC = importlib.util.spec_from_file_location(
    "vps_unprivileged_stage",
    Path(__file__).resolve().parents[1] / "scripts/vps_unprivileged_stage.py")
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)
REAL_IDENTITY = worker._identity
SHA = "a" * 40
PRIVATE = "SYNTHETIC_SECRET_DO_NOT_PRINT"


@pytest.fixture(autouse=True)
def boundary(monkeypatch):
    """Fail even on attempted reads; never inspect a real /root or /var tree."""
    forbidden = Mock(side_effect=AssertionError(PRIVATE))

    def guarded(original):
        def check(*args, **kwargs):
            frame = inspect.currentframe()
            try:
                while frame is not None:
                    if frame.f_globals is worker.__dict__:
                        return forbidden(*args, **kwargs)
                    frame = frame.f_back
            finally:
                del frame
            return original(*args, **kwargs)
        return check

    for obj, names in (
        (builtins, ("open",)), (io, ("open",)),
        (os, ("open", "stat", "lstat", "readlink", "scandir", "mkdir",
              "makedirs", "remove", "unlink", "rename", "replace", "rmdir",
              "system", "popen")),
        (Path, ("open", "stat", "lstat", "iterdir", "mkdir", "unlink",
                "rename", "replace", "rmdir")),
        (subprocess, ("run", "Popen", "call", "check_call", "check_output")),
        (socket, ("socket", "create_connection", "getaddrinfo")),
    ):
        for name in names:
            monkeypatch.setattr(obj, name, guarded(getattr(obj, name)))
    monkeypatch.setattr(worker, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(worker, "_identity", Mock(return_value=(1234,) * 6))
    yield forbidden
    forbidden.assert_not_called()


@pytest.fixture
def mock_evidence():
    """Explicitly synthetic real-isolation observations; NOT a sandbox token.

    This successful scenario validates only the contract, never actual staging.
    There is intentionally no way to supply these facts to stage().
    """
    facts = {key: True for key in worker.ISOLATION_FACTS}
    facts.update(
        identity_before=(1234,) * 6, identity_after=(1234,) * 6,
        ancestor_metadata=tuple((p, 0, 0o755, "directory") for p in
                                ("/", "/var", "/var/lib", worker.BASE)),
        stage_exists=False, stage_uid=1234, stage_mode=0o700,
        free_bytes=worker.MIN_FREE_BYTES, free_inodes=worker.MIN_FREE_INODES,
        main_before=SHA, main_after=SHA, fetched_sha=SHA, checkout_sha=SHA,
        detached=True, clean=True,
        runtime_before=(2500, 4000, 5000, 1, 0),
        runtime_after=(2500, 4000, 5000, 1, 0),
        command_results=("OK",) * len(worker.proposed_commands(SHA)),
    )
    return facts


def test_success_is_only_offline_plan_validation(mock_evidence):
    assert worker.validate_plan(SHA, mock_evidence) == "PLAN_VALIDATED_NOT_STAGED"
    assert worker.stage(SHA) == "NOT_STAGED_ISOLATION_REQUIRED"
    assert not hasattr(worker, "subprocess")
    assert not hasattr(worker, "deploy")


@pytest.mark.parametrize("value", [None, {}, True, "ISOLATED", {"sandbox": True}])
def test_missing_or_unauthenticated_confinement(value):
    assert worker.validate_plan(SHA, value) == worker.PLAN_REJECTED
    assert worker.stage(SHA) == worker.ISOLATION_REQUIRED


@pytest.mark.parametrize("key", sorted(worker.ISOLATION_FACTS))
@pytest.mark.parametrize("value", [None, False, 1, "yes"])
def test_each_isolation_gate_is_mandatory(mock_evidence, key, value):
    mock_evidence[key] = value
    assert worker.validate_plan(SHA, mock_evidence) == worker.PLAN_REJECTED


@pytest.mark.parametrize("sha", [None, True, 42, [], {}, b"a" * 40, "", "main",
    "a" * 39, "a" * 41, "A" * 40, "g" * 40, SHA + "\n", SHA + " ",
    " " + SHA, "../" + SHA, "--help", "$(id)", SHA + ";id"])
def test_bad_sha_before_identity(sha):
    assert worker.stage(sha) == worker.INVALID_SHA
    worker._identity.assert_not_called()
    assert worker.validate_plan(sha, {}) == worker.INVALID_SHA
    for proposal in (worker.proposed_commands, worker.proposed_environment):
        with pytest.raises(ValueError, match="^REJECTED_SHA$"):
            proposal(sha)


def test_sha_subclass_rejected():
    class Untrusted(str):
        def __str__(self):
            raise AssertionError(PRIVATE)
    assert worker.stage(Untrusted(SHA)) == worker.INVALID_SHA


@pytest.mark.parametrize("identity", [(0,) * 6, (0, 1234, 1234, 1234, 1234, 1234),
    (1234, 1234, 0, 1234, 1234, 1234), (1234, 1234, 1234, 0, 0, 0),
    (1234, 1235, 1234, 1234, 1234, 1234), (True,) * 6, (), None])
def test_root_saved_root_or_setid_rejected(identity, mock_evidence):
    worker._identity.return_value = identity
    assert worker.stage(SHA) == worker.INVALID_IDENTITY
    mock_evidence["identity_before"] = mock_evidence["identity_after"] = identity
    assert worker.validate_plan(SHA, mock_evidence) == worker.PLAN_REJECTED


@pytest.mark.parametrize("platform", ["win32", "darwin", "unknown"])
def test_non_linux_execution_refused(platform):
    worker.sys.platform = platform
    assert worker.stage(SHA) == worker.INVALID_IDENTITY
    worker._identity.assert_not_called()


def test_identity_collector_checks_saved_ids_and_groups(monkeypatch):
    fake_os = SimpleNamespace(getresuid=Mock(return_value=(1234, 1234, 0)),
                              getresgid=Mock(return_value=(2345, 2345, 0)))
    monkeypatch.setattr(worker, "os", fake_os)
    assert REAL_IDENTITY() == (1234, 1234, 0, 2345, 2345, 0)
    fake_os.getresuid.assert_called_once_with()
    fake_os.getresgid.assert_called_once_with()


@pytest.mark.parametrize("key,value", [
    ("stage_exists", True), ("stage_exists", None), ("stage_uid", 0),
    ("stage_mode", 0o777), ("no_symlink_components", False),
    ("ancestor_metadata", (("/var", 1234, 0o777, "symlink"),)),
    ("free_bytes", worker.MIN_FREE_BYTES - 1), ("free_bytes", True),
    ("free_inodes", 0), ("main_before", "b" * 40),
    ("main_after", "b" * 40), ("fetched_sha", SHA + "\n"),
    ("checkout_sha", "b" * 40), ("detached", False), ("clean", None),
    ("identity_after", (4321,) * 6),
    ("runtime_after", (2501, 4000, 5000, 1, 0)),
    ("runtime_after", (2500, 4001, 5000, 1, 0)),
    ("runtime_after", (2500, 4000, 5001, 1, 0)),
    ("runtime_before", (2500, 4000, 5000, 2, 0)),
    ("runtime_after", (2500, 4000, 5000, 1, 1)),
    ("runtime_after", (2500, 4000, 5000, True, False)),
    ("ancestor_metadata", tuple((p, False, 0o755, "directory") for p in
                               ("/", "/var", "/var/lib", worker.BASE))),
    ("command_results", ()),
])
def test_unsafe_or_ambiguous_observations(mock_evidence, key, value):
    mock_evidence[key] = value
    assert worker.validate_plan(SHA, mock_evidence) == worker.PLAN_REJECTED


@pytest.mark.parametrize("result", ["TIMEOUT", "PERMISSION_DENIED", "UNKNOWN",
                                     "FAILED", PRIVATE, None, True])
def test_every_command_must_have_unambiguous_result(mock_evidence, result):
    for index in range(len(worker.proposed_commands(SHA))):
        results = list(mock_evidence["command_results"])
        results[index] = result
        mock_evidence["command_results"] = tuple(results)
        assert worker.validate_plan(SHA, mock_evidence) == worker.PLAN_REJECTED
        mock_evidence["command_results"] = ("OK",) * len(results)


@pytest.mark.parametrize("error", [PermissionError(PRIVATE), OSError(PRIVATE),
    subprocess.TimeoutExpired(PRIVATE, 1, output=PRIVATE, stderr=PRIVATE),
    subprocess.CalledProcessError(1, PRIVATE, output=PRIVATE, stderr=PRIVATE),
    KeyboardInterrupt(PRIVATE)])
def test_error_redaction_and_no_retry(error, capsys):
    worker._identity.side_effect = error
    assert worker.stage(SHA) == worker.FAILED
    worker._identity.assert_called_once_with()
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("key", ["GIT_CONFIG_COUNT", "GIT_CONFIG_GLOBAL",
    "GIT_SSH_COMMAND", "GIT_DIR", "GIT_WORK_TREE", "PYTHONPATH", "PYTHONHOME",
    "UV_INDEX_URL", "UV_PROJECT_ENVIRONMENT", "UV_CACHE_DIR", "LD_PRELOAD",
    "LD_LIBRARY_PATH", "SSH_AUTH_SOCK", "SSH_ORIGINAL_COMMAND", "HTTPS_PROXY",
    "http_proxy", "ALL_PROXY", "HOME", "PATH", "BASH_ENV", "ISOLATED",
    "FUNPAY_BOT_DATA_DIR"])
def test_malicious_env_cannot_configure_or_enable_worker(monkeypatch, key):
    expected = worker.proposed_environment(SHA)
    commands = worker.proposed_commands(SHA)
    monkeypatch.setenv(key, PRIVATE)
    assert worker.stage(SHA) == worker.ISOLATION_REQUIRED
    assert worker.proposed_environment(SHA) == expected
    assert worker.proposed_commands(SHA) == commands
    assert PRIVATE not in repr(expected) + repr(commands)
    assert os.environ[key] == PRIVATE


def test_command_array_contract():
    commands = worker.proposed_commands(SHA)
    repo = f"{worker.BASE}/{SHA}/repo"
    for command in commands:
        assert type(command.argv) is tuple
        assert all(type(x) is str for x in command.argv)
        assert command.argv[0] in {"/usr/bin/git", "/usr/bin/uv", "/usr/bin/python3.13"}
        assert command.shell is False
        assert 0 < command.timeout <= 600
        assert command.cwd == repo
        assert not any(x in repr(command) for x in ("/root", "systemctl", "sudo",
                                                   "funpayflow.main", "POST"))
    assert commands[1] == commands[-1]
    assert commands[1].expected_stdout == SHA + "\trefs/heads/main\n"
    assert commands[3].expected_stdout == commands[5].expected_stdout == SHA + "\n"
    assert "--detach" in commands[4].argv and SHA in commands[4].argv
    assert commands[6].expected_stdout == ""
    for command in (commands[7], commands[9]):
        assert "--offline" in command.argv
        assert "--no-config" in command.argv
        assert "--no-python-downloads" in command.argv
    assert {"--locked", "--no-dev", "--no-default-groups"} <= set(commands[7].argv)
    assert commands[8].argv[1:4] == ("-I", "-m", "compileall")
    assert "--wheel" in commands[9].argv
    assert "--clear" not in commands[9].argv


def test_environment_is_private_and_returned_fresh():
    env = worker.proposed_environment(SHA)
    for key in ("HOME", "TMPDIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME",
                "UV_CACHE_DIR", "UV_PROJECT_ENVIRONMENT"):
        assert env[key].startswith(f"{worker.BASE}/{SHA}/")
    env["LD_PRELOAD"] = PRIVATE
    assert "LD_PRELOAD" not in worker.proposed_environment(SHA)


@pytest.mark.parametrize("args", [[], [SHA], ["stage", SHA], ["--help"],
                                   ["--path", PRIVATE], ["--command", PRIVATE]])
def test_cli_has_no_execution_transport(args, capsys):
    assert worker.main(args) == 1
    assert capsys.readouterr() == ("REJECTED_COMMAND\n", "")


def test_mock_plan_cannot_be_used_as_execution_permission(mock_evidence):
    assert worker.validate_plan(SHA, mock_evidence) == worker.PLAN_VALIDATED
    with pytest.raises(TypeError):
        worker.stage(SHA, mock_evidence)
    assert worker.stage(SHA) == worker.ISOLATION_REQUIRED
    del mock_evidence["trusted_tools"]
    assert worker.validate_plan(SHA, mock_evidence) == worker.PLAN_REJECTED
