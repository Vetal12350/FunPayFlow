"""Read-only scoped project telemetry for the Universal Codex Worker.

No subprocess, authentication, GUI, or worker start. A corrupt/stale status
cannot grant permissions or be interpreted as running; this module is only UI
presentation. The original Control Center FunPay monitor remains compatible.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import time
from typing import Any

import importlib.util

_path = Path(__file__).with_name("worker_projects.py")
_spec = importlib.util.spec_from_file_location("project_status_policies", _path)
_policies = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_policies)
PROFILES = _policies.PROFILES

PHASES = frozenset({
    "waiting", "claimed", "clone", "dependencies", "codex_running",
    "branch_pr", "pr_created", "blocked", "unavailable", "stopped",
})
JOB_PATTERN = re.compile(r"(Vetal12350/(?:FunPayFlow|SenReader))#([1-9][0-9]{0,14})\Z")
ID_PATTERN = re.compile(r"[1-9][0-9]*\Z")
ACTIVE_PHASES = frozenset({
    "claimed", "clone", "dependencies", "codex_running", "branch_pr",
})


def safe_project_link(repo: str, link: object) -> bool:
    if repo not in PROFILES or not isinstance(link, str):
        return False
    prefix = f"https://github.com/{repo}/"
    if not link.startswith(prefix):
        return False
    rest = link[len(prefix):]
    if "/" not in rest:
        return False
    kind, _, number = rest.partition("/")
    return kind in {"issues", "pull"} and ID_PATTERN.fullmatch(number) is not None


def read_project_status(root: Path) -> dict[str, Any]:
    """Sanitize only known fields in bounded worker telemetry.

    A missing, invalid or future heartbeat means status UNKNOWN even if some
    historical rows are present. Never expose arbitrary strings from the file.
    """
    empty: dict[str, Any] = {
        "live": False, "phase": "unknown", "active_repo": None, "jobs": {},
        "heartbeat_seconds": None,
    }
    try:
        with (root / "live_status.json").open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            return empty
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("version") != 1:
            return empty
        phase = data.get("phase")
        heartbeat = data.get("heartbeat")
        if phase not in PHASES or type(heartbeat) not in {int, float}:
            return empty
        now = time.time()
        if not (0 <= now - heartbeat <= 30):
            live = False
            age = None
        else:
            live = phase not in {"stopped", "unavailable"}
            age = int(now - heartbeat)
        current = data.get("current_repo")
        if current not in PROFILES:
            current = None
        jobs: dict[str, dict[int, dict[str, str]]] = {repo: {} for repo in PROFILES}
        entries = data.get("project_jobs", {})
        if not isinstance(entries, dict) or len(entries) > 100:
            return empty
        for key, value in entries.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                continue
            match = JOB_PATTERN.fullmatch(key)
            if not match:
                continue
            repo, number = match.group(1), int(match.group(2))
            if repo not in PROFILES or value.get("phase") not in PHASES:
                continue
            item = {"phase": value["phase"]}
            url = value.get("pr_url")
            if safe_project_link(repo, url) and "/pull/" in url:
                item["pr_url"] = url
            jobs[repo][number] = item
        return {"live": live, "phase": phase, "active_repo": current,
                "jobs": jobs, "heartbeat_seconds": age}
    except (OSError, ValueError, TypeError, AttributeError):
        return empty


def project_rows(snapshot: dict, repo: str) -> dict[int, dict]:
    """Convert scoped worker records into minimal view-model rows."""
    if repo not in PROFILES:
        raise ValueError("Unknown project")
    jobs = snapshot.get("jobs", {}).get(repo, {})
    result = {}
    for number, job in jobs.items():
        row = {"issue": number, "phase": job["phase"],
               "link": f"https://github.com/{repo}/issues/{number}"}
        if "pr_url" in job:
            row["pr_url"] = job["pr_url"]
            row["link"] = job["pr_url"]
        result[number] = row
    return result
