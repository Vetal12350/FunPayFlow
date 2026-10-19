"""Unprivileged staging contract; read-only planning, execution NOT implemented.

No subprocess, filesystem or network IO. The upstream/build command arrays below
are proposals, never executed. A trusted sandbox bootstrap and authenticated
observation collector are missing; neither environment flags nor plan validation
can enable execution. See docs/STAGING_ISOLATION.md before extending this module.
"""
from __future__ import annotations

import os
import re
import sys
from typing import NamedTuple

ORIGIN = "https://github.com/Vetal12350/FunPayFlow.git"
BASE = "/var/lib/funpayflow-staging"
PYTHON = "/usr/bin/python3.13"
GIT = "/usr/bin/git"
UV = "/usr/bin/uv"
MIN_FREE_BYTES = 2 * 1024**3
MIN_FREE_INODES = 10000
INVALID_SHA = "REJECTED_SHA"
INVALID_IDENTITY = "REJECTED_IDENTITY"
ISOLATION_REQUIRED = "NOT_STAGED_ISOLATION_REQUIRED"
PLAN_REJECTED = "PLAN_REJECTED_NOT_STAGED"
PLAN_VALIDATED = "PLAN_VALIDATED_NOT_STAGED"
FAILED = "NOT_STAGED_INTERNAL_FAILURE"
INVALID_COMMAND = "REJECTED_COMMAND"
STATUSES = frozenset((INVALID_SHA, INVALID_IDENTITY, ISOLATION_REQUIRED,
                      PLAN_REJECTED, PLAN_VALIDATED, FAILED, INVALID_COMMAND))


def _valid_sha(value: object) -> bool:
    return type(value) is str and re.fullmatch(r"[0-9a-f]{40}", value) is not None


def _identity() -> tuple[int, ...]:
    # Real/effective/saved identities: reject retained root and set-id contexts.
    return (*os.getresuid(), *os.getresgid())


def _unprivileged(identity: object) -> bool:
    return (type(identity) is tuple and len(identity) == 6
            and all(type(x) is int and x > 0 for x in identity)
            and len(set(identity[:3])) == len(set(identity[3:])) == 1)


def stage(sha: str) -> str:
    """Refuse staging, even when the caller asserts it is inside a sandbox.

    Only SHA is accepted. No paths, evidence, command strings, flags or env-based
    activation. Identity checks are real; all other checks are only modeled by
    validate_plan. No local production or staging paths are inspected.
    """
    if not _valid_sha(sha):
        return INVALID_SHA
    try:
        if sys.platform != "linux" or not _unprivileged(_identity()):
            return INVALID_IDENTITY
        return ISOLATION_REQUIRED
    except (Exception, KeyboardInterrupt):
        return FAILED


class Command(NamedTuple):
    argv: tuple[str, ...]
    cwd: str
    timeout: int
    shell: bool = False
    expected_stdout: str | None = None


def proposed_commands(sha: str) -> tuple[Command, ...]:
    """Non-executable allowlist. A future runner needs interleaved gate checks.

    Stage/repo/home/tmp/cache/artifacts must first be created exclusively by the
    trusted bootstrap, never by running these arrays in an existing directory.
    stdout predicates are exact (including newlines), not substring matches.
    """
    if not _valid_sha(sha):
        raise ValueError(INVALID_SHA)
    stage_path = f"{BASE}/{sha}"
    repo = stage_path + "/repo"
    git = (GIT, "-c", "core.hooksPath=/dev/null", "-c", "credential.helper=",
           "-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
           "-c", "http.followRedirects=false")
    main = Command((*git, "ls-remote", "--exit-code", "--refs", ORIGIN,
                    "refs/heads/main"), repo, 60, False,
                   sha + "\trefs/heads/main\n")
    uv = ("--offline", "--no-config", "--no-python-downloads",
          "--no-managed-python", "--python", PYTHON)
    return (
        Command((*git, "init", "--template=", "."), repo, 30),
        main,
        Command((*git, "fetch", "--no-tags", "--no-recurse-submodules",
                 "--depth=1", ORIGIN,
                 "refs/heads/main:refs/remotes/origin/main"), repo, 120),
        Command((*git, "rev-parse", "--verify", "refs/remotes/origin/main^{commit}"),
                repo, 30, False, sha + "\n"),
        Command((*git, "-c", "advice.detachedHead=false", "checkout",
                 "--detach", sha, "--"), repo, 30),
        Command((*git, "rev-parse", "--verify", "HEAD^{commit}"),
                repo, 30, False, sha + "\n"),
        Command((*git, "status", "--porcelain=v1", "--untracked-files=all"),
                repo, 30, False, ""),
        Command((UV, "sync", "--locked", "--no-dev", "--no-default-groups",
                 "--no-editable", "--link-mode=copy", *uv), repo, 600),
        Command((PYTHON, "-I", "-m", "compileall", "-q", "src/funpayflow"),
                repo, 120),
        Command((UV, "build", "--wheel", "--no-sources", "--no-build-logs",
                 "--out-dir", stage_path + "/artifacts", *uv), repo, 600),
        main,
    )


def proposed_environment(sha: str) -> dict[str, str]:
    """Fresh allowlist, never merge os.environ; values are not sandbox proof."""
    if not _valid_sha(sha):
        raise ValueError(INVALID_SHA)
    stage_path = f"{BASE}/{sha}"
    return {
        "PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C",
        "HOME": stage_path + "/home", "TMPDIR": stage_path + "/tmp",
        "XDG_CACHE_HOME": stage_path + "/cache",
        "XDG_CONFIG_HOME": stage_path + "/home/config",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0",
        "GIT_ALLOW_PROTOCOL": "https", "GIT_ATTR_NOSYSTEM": "1",
        "UV_CACHE_DIR": stage_path + "/cache/uv",
        "UV_PROJECT_ENVIRONMENT": stage_path + "/repo/.venv",
        "UV_PYTHON_DOWNLOADS": "never", "UV_OFFLINE": "1",
    }


# Synthetic observations for offline contract testing ONLY. These booleans are
# NOT authenticated evidence. Deliberately no collector or execution path exists.
ISOLATION_FACTS = frozenset((
    "readonly_namespace", "only_stage_writable", "private_tmp_cache",
    "private_data_inaccessible", "no_privilege_gain", "no_inherited_handles",
    "no_host_sockets", "network_policy", "dedicated_account", "exclusive_stage",
    "no_symlink_components", "trusted_tools", "process_tree_limits",
))
FACT_KEYS = ISOLATION_FACTS | frozenset((
    "identity_before", "identity_after", "ancestor_metadata", "stage_exists",
    "stage_uid", "stage_mode", "free_bytes", "free_inodes", "main_before",
    "main_after", "fetched_sha", "checkout_sha", "detached", "clean",
    "runtime_before", "runtime_after", "command_results",
))


def validate_plan(sha: str, facts: object) -> str:
    """Pure validation of mock/trusted observations; NEVER staging success.

    No JSON/CLI/environment adapter. In particular stage() never calls this to
    authorize execution. Missing/unknown/ambiguous observations fail closed.
    """
    if not _valid_sha(sha):
        return INVALID_SHA
    try:
        if type(facts) is not dict or facts.keys() != FACT_KEYS:
            return PLAN_REJECTED
        if any(facts[key] is not True for key in ISOLATION_FACTS):
            return PLAN_REJECTED
        identity = facts["identity_before"]
        if (not _unprivileged(identity)
                or not _unprivileged(facts["identity_after"])
                or facts["identity_after"] != identity):
            return PLAN_REJECTED
        # Proposed root-owned non-writable ancestors, then stage owned by worker.
        ancestors = tuple((p, 0, 0o755, "directory")
                          for p in ("/", "/var", "/var/lib", BASE))
        metadata = facts["ancestor_metadata"]
        if (type(metadata) is not tuple or metadata != ancestors
                or any(type(row) is not tuple or len(row) != 4
                       or tuple(type(x) for x in row) != (str, int, int, str)
                       for row in metadata)):
            return PLAN_REJECTED
        if (facts["stage_exists"] is not False
                or type(facts["stage_uid"]) is not int
                or facts["stage_uid"] != identity[0]
                or type(facts["stage_mode"]) is not int
                or facts["stage_mode"] != 0o700):
            return PLAN_REJECTED
        for key, minimum in (("free_bytes", MIN_FREE_BYTES),
                             ("free_inodes", MIN_FREE_INODES)):
            if type(facts[key]) is not int or facts[key] < minimum:
                return PLAN_REJECTED
        if any(type(facts[k]) is not str or facts[k] != sha
               for k in ("main_before", "main_after", "fetched_sha", "checkout_sha")):
            return PLAN_REJECTED
        if facts["detached"] is not True or facts["clean"] is not True:
            return PLAN_REJECTED
        # Gate's opaque runtime identity: PID, start ticks, code inode, runner
        # count, restart count. Worker must never inspect the production process.
        runtime = facts["runtime_before"]
        if (type(runtime) is not tuple or len(runtime) != 5
                or any(type(x) is not int for x in runtime)
                or min(runtime[:3]) <= 1 or runtime[3] != 1 or runtime[4] < 0
                or type(facts["runtime_after"]) is not tuple
                or any(type(x) is not int for x in facts["runtime_after"])
                or facts["runtime_after"] != runtime):
            return PLAN_REJECTED
        results = facts["command_results"]
        if (type(results) is not tuple or len(results) != len(proposed_commands(sha))
                or any(type(x) is not str or x != "OK" for x in results)):
            return PLAN_REJECTED
        return PLAN_VALIDATED
    except (Exception, KeyboardInterrupt):
        return PLAN_REJECTED


def main(argv: list[str] | None = None) -> int:
    """No CLI transport is installed. Emit a constant status; always fail."""
    print(INVALID_COMMAND)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
