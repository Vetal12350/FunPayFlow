"""Offline: deployment heartbeat file used by scripts/deploy_agent.py."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from funpayflow import heartbeat

SHA = "c" * 40


def client(state="healthy", boom=False):
    def health():
        if boom:
            raise RuntimeError("private detail")
        return {"state": state, "incident_id": "secret-ish"}
    return SimpleNamespace(get_runner_health=health)


@pytest.mark.parametrize("prefix,expected", [
    (f"/opt/funpayflow/releases/{SHA}/venv", SHA),
    (f"/opt/x/{SHA}/venv", SHA),
    ("/home/u/FunPayFlow/.venv", None),
    (f"/opt/funpayflow/releases/{SHA}", None),
    (f"/opt/releases/{SHA.upper()}/venv", None),
    ("/usr", None),
])
def test_release_sha_only_for_agent_releases(prefix, expected):
    assert heartbeat.release_sha(prefix) == expected


@pytest.mark.parametrize("c,expected", [
    (client("healthy"), "ok"), (client("backoff"), "backoff"),
    (client("failed"), "failed"), (client(boom=True), "unknown"),
    (client(None), "unknown"),
])
def test_runner_status(c, expected):
    assert heartbeat.runner_status(c) == expected


def test_file_contains_only_sha_state_and_time(tmp_path):
    path = tmp_path / "health.json"
    heartbeat.write_heartbeat(path, SHA, heartbeat.runner_status(client()), 123.5)
    assert json.loads(path.read_text()) == {"sha": SHA, "runner": "ok", "time": 123.5}
    assert not list(tmp_path.glob("*.tmp"))


def test_loop_writes_periodically_and_survives_io_errors(tmp_path):
    async def scenario():
        times = iter([10.0, 20.0, 30.0])
        path = tmp_path / "health.json"
        task = asyncio.create_task(heartbeat.heartbeat_loop(
            client(), SHA, path, interval=0.01, clock=lambda: next(times, 40.0)))
        await asyncio.sleep(0.05)
        assert json.loads(path.read_text())["time"] >= 20.0
        await heartbeat.stop_heartbeat(task)
        assert task.cancelled()
        broken = asyncio.create_task(heartbeat.heartbeat_loop(
            client(), SHA, tmp_path / "missing" / "health.json", interval=0.01))
        await asyncio.sleep(0.03)
        assert not broken.done()
        await heartbeat.stop_heartbeat(broken)
    asyncio.run(scenario())


def test_start_is_noop_outside_release(monkeypatch):
    monkeypatch.setattr(heartbeat.sys, "prefix", "/home/dev/.venv")
    assert heartbeat.start_heartbeat(client()) is None
    asyncio.run(heartbeat.stop_heartbeat(None))


def test_start_writes_into_data_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(heartbeat.sys, "prefix", f"/opt/releases/{SHA}/venv")
    monkeypatch.setenv("FUNPAY_BOT_DATA_DIR", str(tmp_path))

    async def scenario():
        task = heartbeat.start_heartbeat(client())
        await asyncio.sleep(0.01)
        await heartbeat.stop_heartbeat(task)
    asyncio.run(scenario())
    assert json.loads((tmp_path / "health.json").read_text())["sha"] == SHA


@pytest.mark.skipif(os.name == "nt", reason="deploy agent is Linux-only")
def test_deploy_agent_accepts_application_heartbeat(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts" / "deploy_agent.py"
    spec = importlib.util.spec_from_file_location("deploy_agent_hb", script)
    agent_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent_mod)
    agent = SimpleNamespace(data=tmp_path)
    heartbeat.write_heartbeat(tmp_path / "health.json", SHA, "ok", 200.0)
    assert agent_mod.Agent.heartbeat_ok(agent, SHA, 100.0)
    assert not agent_mod.Agent.heartbeat_ok(agent, SHA, 300.0)
    assert not agent_mod.Agent.heartbeat_ok(agent, "d" * 40, 100.0)
    heartbeat.write_heartbeat(tmp_path / "health.json", SHA, "backoff", 200.0)
    assert not agent_mod.Agent.heartbeat_ok(agent, SHA, 100.0)


@pytest.mark.skipif(os.name == "nt", reason="symlink layout of the Linux deploy agent")
def test_release_sha_follows_the_current_symlink(tmp_path):
    release = tmp_path / "releases" / SHA / "venv"
    release.mkdir(parents=True)
    (tmp_path / "current").symlink_to(tmp_path / "releases" / SHA)
    assert heartbeat.release_sha(str(tmp_path / "current" / "venv")) == SHA
    old = tmp_path / "stage" / ".venv"
    old.mkdir(parents=True)
    (tmp_path / "releases" / ("0" * 40)).mkdir()
    (tmp_path / "releases" / ("0" * 40) / "venv").symlink_to(old)
    assert heartbeat.release_sha(str(tmp_path / "releases" / ("0" * 40) / "venv")) is None
