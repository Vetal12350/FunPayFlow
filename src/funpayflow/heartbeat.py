"""Deployment heartbeat: <data dir>/health.json for the VPS deploy agent.

Active only when the code runs from a deploy-agent release, i.e. the interpreter
prefix is ``.../releases/<40-hex sha>/venv``. Development, Windows and installer
setups are unaffected. The file holds only the release SHA, a coarse runner
state and a wall-clock timestamp: no account, token, order or exception data.
A heartbeat failure never stops the bot.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import sys
import time

from .runtime_paths import data_dir

HEALTH_FILE = "health.json"
INTERVAL = 15.0
_SHA = re.compile(r"[0-9a-f]{40}")


def release_sha(prefix: str | None = None) -> str | None:
    venv = Path(prefix if prefix is not None else sys.prefix)
    if venv.name != "venv":
        return None
    name = venv.parent.name
    return name if _SHA.fullmatch(name) else None


def runner_status(client) -> str:
    try:
        state = client.get_runner_health().get("state")
    except Exception:
        return "unknown"
    return "ok" if state == "healthy" else (state if isinstance(state, str) else "unknown")


def write_heartbeat(path: Path, sha: str, runner: str, now: float) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"sha": sha, "runner": runner, "time": now}),
                   encoding="utf-8")
    os.replace(tmp, path)


async def heartbeat_loop(client, sha: str, path: Path, *, interval: float = INTERVAL,
                         clock=time.time) -> None:
    while True:
        try:
            write_heartbeat(path, sha, runner_status(client), clock())
        except OSError:
            pass
        await asyncio.sleep(interval)


def start_heartbeat(client) -> asyncio.Task | None:
    sha = release_sha()
    if sha is None:
        return None
    return asyncio.create_task(heartbeat_loop(client, sha, data_dir() / HEALTH_FILE),
                               name="deploy_heartbeat")


async def stop_heartbeat(task: asyncio.Task | None) -> None:
    if task is None:
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
