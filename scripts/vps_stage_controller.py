"""Phase 2 STAGING ONLY: owner-operated Windows plan preview; SSH is disabled."""
from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import re
import sys

# Load only the reviewed sibling, never a module selected from PATH or an issue.
_spec = importlib.util.spec_from_file_location(
    "_stage_status_validation", Path(__file__).resolve().with_name("vps_status_controller.py"))
_status = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_status)
Failure = _status.Failure
REPO = _status.REPO
ORIGIN = f"https://github.com/{REPO}.git"
MAIN = f"repos/{REPO}/git/ref/heads/main"
CI = f"repos/{REPO}/actions/workflows/ci.yml/runs"


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally echoes untrusted argument values.
        self.exit(2, "Invalid staging arguments. Use --help.\n")


def validate_sha(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise Failure("local")
    return value


def supported() -> bool:
    return os.name == "nt" and sys.version_info >= (3, 13)


def github(endpoint: str, projection: str):
    raw = _status.invoke(
        ["gh.exe", "api", "--hostname", "github.com", "--method", "GET",
         endpoint, "--jq", projection], category="github")
    return _status.decode_json(raw, 16384, "github")


def verify_main(sha: str) -> None:
    validate_sha(sha)
    value = github(MAIN, "{sha: .object.sha, type: .object.type}")
    if value != {"sha": sha, "type": "commit"}:
        raise Failure("github")


def verify_source(sha: str) -> None:
    """Read only GitHub metadata; no issue, checkout, script or remote execution."""
    verify_main(sha)
    value = github(
        CI + f"?branch=main&event=push&head_sha={sha}&per_page=1",
        "{runs: [.workflow_runs[] | {id, path, name, head_sha, head_branch, "
        "event, status, conclusion, repo: .repository.full_name, "
        "head_repo: .head_repository.full_name}]}")
    if not isinstance(value, dict) or set(value) != {"runs"}:
        raise Failure("github")
    runs = value["runs"]
    if not isinstance(runs, list) or len(runs) != 1 or not isinstance(runs[0], dict):
        raise Failure("github")
    run = runs[0]
    expected = {"path": ".github/workflows/ci.yml", "name": "CI", "head_sha": sha,
                "head_branch": "main", "event": "push", "status": "completed",
                "conclusion": "success", "repo": REPO, "head_repo": REPO}
    if (set(run) != set(expected) | {"id"} or type(run["id"]) is not int
            or not 1 <= run["id"] <= 2**63 - 1
            or any(run[key] != val for key, val in expected.items())):
        raise Failure("github")


def preview(sha: str, config: dict) -> bool:
    sha = validate_sha(sha)
    config = _status.validate_config(config)
    # This function is also safe when called independently of main().
    if not sys.stdin.isatty():
        print("Declined: interactive terminal required.")
        return False
    verify_source(sha)
    print("STAGING ONLY / PLAN ONLY. Remote execution is disabled.")
    print(f"Target SHA: {sha}\nPublic host: {config['host']}")
    print(f"Proposed NEW directory: /root/FunPayFlow-stage-{sha[:12]}")
    print(f"Proposed source: {ORIGIN} (detached exact SHA).")
    print("A future reviewed executor must refuse an existing stage, verify active service, "
          "unchanged PID/WorkingDirectory and stage HEAD, and confine all writes to stage.")
    print("No SSH, dependency install, bot launch, backup or service change will occur.")
    approval = f"PLAN {sha} {config['host']}"
    if input(f"Type exactly {approval} to acknowledge this plan: ") != approval:
        print("Declined.")
        return False
    # Recheck after the human pause. This acknowledgment cannot authorize execution.
    verify_source(sha)
    print("Plan acknowledged. Nothing staged; no SSH attempted. "
          "Source review and an isolated executor remain required.")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--plan", metavar="SHA", help="Preview only; never execute staging")
    args = parser.parse_args(argv)
    if args.plan is None:
        print("Disabled. Use --plan with an exact lowercase source SHA.")
        return 0
    try:
        sha = validate_sha(args.plan)
        if not supported():
            raise Failure("local")
        if not sys.stdin.isatty():
            print("Declined: interactive terminal required.")
            return 1
        root = _status.local_directory()
        config = _status.validate_config(_status.decode_json(
            _status.read_bounded(root / "config.json", 4096), 4096, "local"))
        return 0 if preview(sha, config) else 1
    except KeyboardInterrupt:
        print("Declined.")
        return 1
    except (Failure, OSError, EOFError) as error:
        category = error.category if isinstance(error, Failure) else "local"
        print(f"Staging plan unavailable ({category}). Nothing staged.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
