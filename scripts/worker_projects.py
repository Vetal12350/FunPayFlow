"""Trusted, immutable project policies for a future single-process Codex worker.

This module intentionally does NOT change the running worker, read credentials,
query GitHub, spawn Codex, or grant authority from issue descriptions. Integration
requires a separate reviewed change before the existing worker is restarted.
"""
from __future__ import annotations

from types import MappingProxyType
from typing import Mapping, NamedTuple
import re


class ProjectProfile(NamedTuple):
    """Reviewed settings, never populated from an Issue body or network response."""

    repo: str
    owner: str
    remote: str
    branch: str
    toolchain: str
    allowed_prefixes: tuple[str, ...]
    allowed_files: frozenset[str]
    github_ci: tuple[str, ...]


_FUNPAY = ProjectProfile(
    repo="Vetal12350/FunPayFlow",
    owner="Vetal12350",
    remote="https://github.com/Vetal12350/FunPayFlow.git",
    branch="main",
    toolchain="python-uv",
    # Preserve the existing worker path scope exactly until explicitly reviewed.
    allowed_prefixes=("src/funpayflow/", "tests/", "docs/", "scripts/"),
    allowed_files=frozenset({"README.md"}),
    github_ci=(".github/workflows/ci.yml", ".github/workflows/product-qa.yml"),
)

_SENREADER = ProjectProfile(
    repo="Vetal12350/SenReader",
    owner="Vetal12350",
    remote="https://github.com/Vetal12350/SenReader.git",
    branch="main",
    toolchain="flutter-github-ci",
    allowed_prefixes=("lib/", "test/", "docs/", "scripts/"),
    allowed_files=frozenset({"README.md", "pubspec.yaml", "analysis_options.yaml"}),
    github_ci=(".github/workflows/android-apk.yml",),
)

# This registry is intentionally source-controlled, fixed and read-only. There
# is no automatic discovery of new repositories from GitHub or Issue metadata.
PROFILES: Mapping[str, ProjectProfile] = MappingProxyType({
    _FUNPAY.repo: _FUNPAY,
    _SENREADER.repo: _SENREADER,
})
_BLOCKED_SUBSTRINGS = (".env", "secret", "credential", "private", "token", "keyfile")
_ISSUE_NUMBER = re.compile(r"[1-9][0-9]{0,14}\Z", flags=re.ASCII)
_REPO_KEY = re.compile(r"Vetal12350/(FunPayFlow|SenReader)\Z")


def profile_for(repo: str) -> ProjectProfile:
    """Fail closed if repo is not explicitly trusted in this code version."""
    if type(repo) is not str or repo not in PROFILES:
        raise ValueError("Unknown or unauthorized repository")
    return PROFILES[repo]


def allowed_path(profile: ProjectProfile, path: str) -> bool:
    """Check a Git status POSIX path against one project's reviewed write scope.

    A caller MUST additionally reject symlinks, submodules, renamed entries,
    non-text/binary outputs and suspicious index modes before committing.
    This function alone is not an OS sandbox or full filesystem security check.
    """
    if profile not in PROFILES.values() or type(path) is not str:
        return False
    if (not path or len(path) > 512 or path.startswith("/") or "\\" in path
            or any(ord(c) < 32 or ord(c) == 127 for c in path)):
        return False
    components = path.split("/")
    if any(not part or part in {".", ".."} or part.startswith(".")
           for part in components):
        return False
    if ":" in path:
        return False
    if any(any(word in component.lower() for word in _BLOCKED_SUBSTRINGS)
           for component in components):
        return False
    return path in profile.allowed_files or path.startswith(profile.allowed_prefixes)


def task_key(repo: str, issue_number: int) -> str:
    """Collision-free identity across GitHub repos for one worker ledger."""
    profile_for(repo)
    if type(issue_number) is not int or not 0 < issue_number < 10**15:
        raise ValueError("Invalid issue number")
    return f"{repo}#{issue_number}"


def migrate_funpay_ledger(old_state: dict) -> dict[str, dict]:
    """Return a *copy* of legacy state, preserving every claimed/blocked entry.

    This pure function never touches state.json. The eventual migration should
    make a verified atomic backup, use one global instance lock and fail closed
    if either source or destination state is malformed.
    """
    if type(old_state) is not dict:
        raise ValueError("Legacy ledger must be a mapping")
    migrated: dict[str, dict] = {}
    for key, value in old_state.items():
        if type(key) is not str or not _ISSUE_NUMBER.fullmatch(key):
            raise ValueError("Invalid legacy issue key")
        if type(value) is not dict:
            raise ValueError("Invalid legacy task state")
        migrated[task_key(_FUNPAY.repo, int(key))] = value.copy()
    return migrated


def choose_next_repo(pending: Mapping[str, bool], previous: str | None) -> str | None:
    """Fair, deterministic round-robin across the **fixed** project list.

    The scheduler is single-task-at-a-time; this helper does not spawn jobs.
    """
    if not isinstance(pending, Mapping):
        raise ValueError("Invalid queue mapping")
    for repo in pending:
        profile_for(repo)
        if type(pending[repo]) is not bool:
            raise ValueError("Pending states must be boolean")
    if previous is not None:
        profile_for(previous)
    ordered = list(PROFILES)
    first = (ordered.index(previous) + 1) % len(ordered) if previous else 0
    for offset in range(len(ordered)):
        repo = ordered[(first + offset) % len(ordered)]
        if pending.get(repo, False):
            return repo
    return None
