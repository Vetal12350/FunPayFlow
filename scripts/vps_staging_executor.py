"""STAGING ONLY / DISABLED: fail-closed boundary for a future forced command.

This is the explicitly permitted no-op fallback, NOT a staging implementation or
deployment. SHA validation does not verify upstream main, service identity, PID,
cwd, runtime count, disk space, ownership, or an existing stage. None of those
checks is claimed to have passed. No network, subprocess, or filesystem IO occurs.

Why execution remains disabled: locked uv sync can execute Python build backends
(including the project's backend), and root build code can write outside a stage
or read runtime credentials. Sanitizing Git/environment and redirecting uv caches
does not confine that code. A separately audited isolation boundary must enforce
stage-only writes and deny access to runtime data before this can be enabled.
An SSH_ORIGINAL_COMMAND string cannot prove sshd's forced-command restrictions.
Installation of that root-owned endpoint and its policy is also outside this file.

Future execution needs fixed public upstream/main verification, exclusive stage
creation, safe Git config/hooks, bounded commands, and live service/PID/cwd and
runtime-count checks before and after staging, including failures. It must never
stop/start a service, activate code, remove partial stages, or run the application.
Final deployment and rollback require a separate independent audit.

Invocation contract (still disabled): trusted Python caller stage(exact_sha), or
no CLI arguments and SSH_ORIGINAL_COMMAND exactly 'stage <exact_sha>'. Invoke a
future installed endpoint using isolated Python (-I) and a trusted root-owned
interpreter/script; this file does not configure SSH or establish that trust.
"""

from __future__ import annotations

import os
import re
import sys


INVALID_SHA = "REJECTED_SHA"
INVALID_CONTEXT = "REJECTED_CONTEXT"
INVALID_COMMAND = "REJECTED_COMMAND"
DISABLED = "NOT_STAGED_ISOLATION_REQUIRED"
FAILED = "NOT_STAGED_INTERNAL_FAILURE"
STATUSES = frozenset((INVALID_SHA, INVALID_CONTEXT, INVALID_COMMAND, DISABLED, FAILED))


def _valid_sha(value: object) -> bool:
    # Reject subclasses too: no caller-supplied conversion or formatting methods.
    return type(value) is str and re.fullmatch(r"[0-9a-f]{40}", value) is not None


def _root_linux() -> bool:
    return sys.platform == "linux" and os.getuid() == 0 and os.geteuid() == 0


def stage(sha: str) -> str:
    """Validate the request shape, then refuse execution; never report success.

    This accepts only the SHA, never a command, issue, path, or environment config.
    Valid input still returns NOT_STAGED_ISOLATION_REQUIRED. No plan is executed.
    """
    if not _valid_sha(sha):
        return INVALID_SHA
    try:
        if not _root_linux():
            return INVALID_CONTEXT
    except Exception:
        return FAILED
    return DISABLED


def main(argv: list[str] | None = None) -> int:
    """Emit exactly one allowlisted status; every request exits unsuccessfully."""
    status = FAILED
    try:
        args = sys.argv[1:] if argv is None else argv
        command = os.environ.get("SSH_ORIGINAL_COMMAND", "")
        if args or type(command) is not str or re.fullmatch(
            r"stage [0-9a-f]{40}", command
        ) is None:
            status = INVALID_COMMAND
        else:
            status = stage(command[6:])
    except (Exception, KeyboardInterrupt):
        # Never interpolate an exception, command, environment value or raw log.
        status = FAILED
    if type(status) is not str or status not in STATUSES:
        status = FAILED
    print(status, file=sys.stdout)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
