"""Deployment PLAN VALIDATION ONLY. No production executor is implemented.

The pure validator consumes synthetic/trusted observations, never discovers them.
The command entrypoint always refuses deployment, even for a valid command.
No application imports, subprocesses, filesystem access, network or mutation.
"""
from __future__ import annotations

import os
from pathlib import PurePosixPath
import re
import sys

ORIGIN = "https://github.com/Vetal12350/FunPayFlow.git"
INSTALL_PATH = "/usr/local/libexec/funpayflow-deploy.py"
DATA = "/root/FunPayFlow-data"
SHA_PATTERN = r"[0-9a-f]{40}"


class Rejected(ValueError):
    """Fixed, non-sensitive rejection; never attach input or an exception."""

    def __init__(self):
        super().__init__("PLAN_REJECTED")


def parse_command(command: object) -> str:
    if type(command) is not str or re.fullmatch(
            rf"deploy ({SHA_PATTERN})", command) is None:
        raise Rejected()
    return command[7:]


def validate_transport(argv: list[str], environ: dict[str, str],
                       tty: bool) -> str:
    # No CLI deploy flags, environment assignments, PTY, forwarded agent or
    # locale/Python/Git settings. sshd must be configured to send no AcceptEnv.
    allowed = {"SSH_ORIGINAL_COMMAND", "SSH_CONNECTION", "SSH_CLIENT",
               "USER", "LOGNAME", "HOME", "SHELL", "PATH"}
    if argv or tty or not set(environ) <= allowed:
        raise Rejected()
    fixed = {"USER": "root", "LOGNAME": "root", "HOME": "/root",
             "SHELL": "/bin/bash", "PATH": "/usr/bin:/bin"}
    if any(environ.get(key) != value for key, value in fixed.items()):
        raise Rejected()
    if not environ.get("SSH_CONNECTION") or not environ.get("SSH_CLIENT"):
        raise Rejected()
    return parse_command(environ.get("SSH_ORIGINAL_COMMAND"))


def expected_unit(stage: str) -> str:
    """One deliberately narrow template; any deployment-specific change rejects.

    This is a proposed reviewed template, not a claim about the installed unit.
    It contains the stage exactly twice. Never print caller-supplied unit text.
    """
    return (
        "[Unit]\nDescription=FunPayFlow\nAfter=network-online.target\n"
        "Wants=network-online.target\n\n[Service]\nType=simple\nUser=root\n"
        f"WorkingDirectory={stage}\nEnvironmentFile={DATA}/.env\n"
        f'Environment="FUNPAY_BOT_DATA_DIR={DATA}"\n'
        f"ExecStart={stage}/.venv/bin/python -m funpayflow.main\n"
        "KillSignal=SIGTERM\nTimeoutStopSec=40\nRestart=on-failure\n"
        "RestartSec=10\n\n[Install]\nWantedBy=multi-user.target\n"
    )


# These are required observations for a future trusted VPS collector. Booleans
# here are assertions by the caller, NOT evidence gathered or authenticated by us.
REQUIRED_TRUE = frozenset({
    "root_owned_paths", "no_symlink_components", "clean_previous_stage",
    "previous_stage_verified", "unit_root_owned_0644", "no_unit_overrides",
    "disk_sufficient", "prerequisites_verified", "previous_backup_verified",
    "rollback_schema_compatible", "dependencies_locked", "compile_verified",
    "deployment_lock_held",
})
REQUIRED_FALSE = frozenset({"stage_exists", "legacy_active"})
FACT_KEYS = frozenset({
    "origin", "upstream_main", "previous_sha", "previous_stage", "new_stage",
    "unit", "active", "main_runner_count", "main_pid", "pid_cwd",
    "working_directory", "restart_count_before", "restart_count_after",
    "health_seconds",
}) | REQUIRED_TRUE | REQUIRED_FALSE


def validate_plan(command: object, facts: dict[str, object]) -> str:
    """Validate plan invariants only; result never authorizes execution.

    No JSON/file/issue-body input interface is provided. A future collector must
    establish each observation independently under the deployment lock, including
    a fresh main lookup; caller-supplied booleans cannot establish trust.
    """
    sha = parse_command(command)
    if type(facts) is not dict or facts.keys() != FACT_KEYS:
        raise Rejected()
    if (any(facts[key] is not True for key in REQUIRED_TRUE)
            or any(facts[key] is not False for key in REQUIRED_FALSE)):
        raise Rejected()
    if facts["origin"] != ORIGIN or facts["upstream_main"] != sha:
        raise Rejected()
    previous = facts["previous_sha"]
    stage = facts["previous_stage"]
    if (type(previous) is not str or re.fullmatch(SHA_PATTERN, previous) is None
            or type(stage) is not str):
        raise Rejected()
    match = re.fullmatch(r"/root/FunPayFlow-stage-([0-9a-f]{8,40})", stage)
    if match is None or not previous.startswith(match[1]):
        raise Rejected()
    new_stage = f"/root/FunPayFlow-stage-{sha}"
    if (facts["new_stage"] != new_stage or stage == new_stage
            or PurePosixPath(stage).parent != PurePosixPath("/root")
            or facts["working_directory"] != stage or facts["pid_cwd"] != stage):
        raise Rejected()
    unit = facts["unit"]
    if (type(unit) is not str or unit.count(stage) != 2
            or unit != expected_unit(stage)):
        raise Rejected()
    if facts["active"] is not True:
        raise Rejected()
    integers = ("main_runner_count", "main_pid", "restart_count_before",
                "restart_count_after", "health_seconds")
    if any(type(facts[key]) is not int for key in integers):
        raise Rejected()
    if (facts["main_runner_count"] != 1 or facts["main_pid"] <= 1
            or facts["health_seconds"] < 20
            or facts["restart_count_before"] < 0
            or facts["restart_count_after"] != facts["restart_count_before"]):
        raise Rejected()
    return "PLAN_VALIDATED_DEPLOY_DISABLED"


def main(argv: list[str] | None = None) -> int:
    status = "REQUEST_REJECTED"
    try:
        # In a future install, invoke with an isolated trusted Python (-I).
        # No --selfcheck: even valid requests do not access the local machine.
        validate_transport(list(sys.argv[1:] if argv is None else argv),
                           dict(os.environ),
                           any(stream.isatty() for stream in
                               (sys.stdin, sys.stdout, sys.stderr)))
        status = "DEPLOY_NOT_IMPLEMENTED"
    except (Exception, KeyboardInterrupt):
        # No exception text, original command, paths or environment in output.
        pass
    print(status)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
