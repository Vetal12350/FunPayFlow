"""Offline: deploy agent with a fake Linux host (systemctl/venv/pip/systemd-run).

No subprocesses, network, SSH or real services. SQLite is a temporary file.
"""
from contextlib import closing
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tarfile

import pytest

if os.name == "nt":  # agent is Linux-only (fcntl, symlinks); logic is tested on Linux CI
    pytest.skip("Linux deploy agent", allow_module_level=True)

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "deploy_agent.py"
spec = importlib.util.spec_from_file_location("deploy_agent", SCRIPT)
agent_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent_mod)

OLD = "a" * 40
NEW = "b" * 40


class FakeHost(agent_mod.Host):
    def __init__(self, tmp):
        self.t = 1000.0
        self.tmp = tmp
        self.calls = []
        self.spawned = []
        self.legacy = "inactive"
        self.dropins = ""
        self.exec_path = None
        self.bad_releases = set()      # service crashes when current points here
        self.heartbeat = True
        self.pip_rc = 0
        self.restart_rc = 0
        self.enter = 1
        self.pid = 100
        self.restarts = 0
        self.active = "active"
        self.free = 10_000
        self.current_link = None
        self.data_dir = None

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s

    def free_mb(self, path):
        return self.free

    def spawn_worker(self, sha):
        self.spawned.append(sha)
        return True

    def _current(self):
        try:
            return Path(os.readlink(self.current_link)).name
        except OSError:
            return None

    def run(self, argv, timeout=300):
        self.calls.append(argv)
        if argv[1:3] == ["-I", "-m"] and argv[3] == "venv":
            Path(argv[4], "bin").mkdir(parents=True)
            return 0, ""
        if "pip" in argv:
            return self.pip_rc, ""
        if argv[:2] == ["systemctl", "is-active"]:
            return 3, self.legacy + "\n"
        if argv[:2] == ["systemctl", "restart"]:
            if self.restart_rc:
                return self.restart_rc, ""
            self.enter += 1
            self.pid += 1
            cur = self._current()
            if cur in self.bad_releases:
                self.active = "failed"
            else:
                self.active = "active"
                if self.heartbeat:
                    (self.data_dir / "health.json").write_text(json.dumps(
                        {"sha": cur, "runner": "ok", "time": self.t}))
            return 0, ""
        if argv[:2] == ["systemctl", "show"]:
            sub = "running" if self.active == "active" else "failed"
            return 0, "\n".join([
                "LoadState=loaded", f"ActiveState={self.active}", f"SubState={sub}",
                f"MainPID={self.pid if self.active == 'active' else 0}",
                f"NRestarts={self.restarts}", f"DropInPaths={self.dropins}",
                f"ExecStart={{ path={self.exec_path} ; argv[]={self.exec_path} -m funpayflow.main }}",
                f"ActiveEnterTimestampMonotonic={self.enter}"])
        raise AssertionError(argv)


def make_archive(sha, wheels=None, manifest_override=None, extra=None):
    wheels = wheels or {"wheels/funpayflow-1.0-py3-none-any.whl": b"PK-new-" + sha.encode()}
    manifest = {"sha": sha, "wheels": {n: hashlib.sha256(d).hexdigest()
                                       for n, d in wheels.items()}}
    if manifest_override:
        manifest.update(manifest_override)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:") as tar:
        def add(name, data):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        add("manifest.json", json.dumps(manifest).encode())
        for name, data in wheels.items():
            add(name, data)
        for name, data in (extra or {}).items():
            add(name, data)
    data = buf.getvalue()
    return data, hashlib.sha256(data).hexdigest()


@pytest.fixture
def env(tmp_path):
    base = tmp_path
    data = base / "data"
    data.mkdir()
    with closing(sqlite3.connect(data / "state.sqlite3")) as db:
        db.execute("create table orders(id text primary key)")
        db.execute("insert into orders values ('synthetic-1')")
        db.commit()
    (data / "bot_settings.json").write_text("{}")
    config = {
        "root": str(base / "agent"), "releases": str(base / "releases"),
        "current": str(base / "current"), "data_dir": str(data),
        "backups": str(base / "backups"), "service": "funpayflow.service",
        "forbidden_services": ["funpay-bot.service"], "python": "/usr/bin/python3",
        "keep_releases": 3, "health_timeout": 120, "health_stable": 20,
        "require_heartbeat": True, "min_free_mb": 500,
    }
    host = FakeHost(base)
    host.current_link = Path(config["current"])
    host.data_dir = data
    host.exec_path = f"{config['current']}/venv/bin/python"
    agent = agent_mod.Agent(agent_mod.validate_config(config), host)
    (agent.releases / OLD / "venv").mkdir(parents=True)
    agent.switch_to(OLD)
    return agent, host


def deploy(agent, sha=NEW, archive=None):
    data, digest = archive or make_archive(sha)
    assert agent.receive(sha, digest, io.BytesIO(data)) == "ACCEPTED"
    return agent.work(sha)


def test_successful_deploy_switches_and_backs_up(env):
    agent, host = env
    assert deploy(agent) == "DEPLOYED"
    assert agent.current_sha() == NEW
    assert host.spawned == [NEW]
    assert agent.status(NEW) == "DEPLOYED"
    job = agent.read_job(NEW)
    assert job["previous"] == OLD
    backup = agent.backups / job["backup"] / "state.sqlite3"
    with closing(sqlite3.connect(backup)) as db:
        assert db.execute("select id from orders").fetchall() == [("synthetic-1",)]
    pip = next(c for c in host.calls if "pip" in c and "install" in c)
    assert {"--no-index", "--no-deps", "--only-binary=:all:"} <= set(pip)
    assert not (agent.incoming / f"{NEW}.tar").exists()


def test_failed_health_rolls_back_automatically(env):
    agent, host = env
    host.bad_releases.add(NEW)
    assert deploy(agent) == "ROLLED_BACK"
    assert agent.current_sha() == OLD
    assert agent.status(NEW) == "ROLLED_BACK HEALTH_FAILED"


def test_missing_heartbeat_rolls_back(env):
    agent, host = env
    host.heartbeat = False
    assert deploy(agent) == "ROLLED_BACK"
    assert agent.current_sha() == OLD


def test_rollback_failure_needs_owner_and_blocks_new_jobs(env):
    agent, host = env
    host.bad_releases.update({NEW, OLD})
    assert deploy(agent) == "NEEDS_OWNER"
    assert agent.status(NEW) == "NEEDS_OWNER ROLLBACK_FAILED"
    calls = len(host.calls)
    assert agent.work(NEW) == "NEEDS_OWNER"       # never retried
    assert len(host.calls) == calls
    other = "c" * 40
    data, digest = make_archive(other)
    with pytest.raises(agent_mod.Rejected) as exc:
        agent.receive(other, digest, io.BytesIO(data))
    assert exc.value.code == "BUSY"
    with pytest.raises(agent_mod.Rejected):
        agent.manual_rollback(NEW)
    agent.job_path(NEW).unlink()                   # owner resolved it by hand
    host.bad_releases.clear()
    assert deploy(agent, other) == "DEPLOYED"


def test_automatic_restart_loop_counts_as_unhealthy(env):
    agent, host = env
    original = host.run

    def crashing(argv, timeout=300):
        rc, out = original(argv, timeout)
        if argv[:2] == ["systemctl", "restart"] and host._current() == NEW:
            host.restarts += 1
        return rc, out
    host.run = crashing
    assert deploy(agent) == "ROLLED_BACK"


def test_heartbeat_from_other_release_is_not_health(env):
    agent, host = env
    original = host.run

    def stale(argv, timeout=300):
        rc, out = original(argv, timeout)
        if argv[:2] == ["systemctl", "restart"]:
            (host.data_dir / "health.json").write_text(json.dumps(
                {"sha": OLD, "runner": "ok", "time": host.t}))
        return rc, out
    host.run = stale
    assert deploy(agent) == "ROLLED_BACK"


def test_restart_command_failure_rolls_back(env):
    agent, host = env
    host.restart_rc = 1
    # restart of the rollback also fails -> owner must intervene
    assert deploy(agent) == "NEEDS_OWNER"


def test_digest_mismatch_rejected_before_any_change(env):
    agent, host = env
    data, _ = make_archive(NEW)
    with pytest.raises(agent_mod.Rejected) as exc:
        agent.receive(NEW, "0" * 64, io.BytesIO(data))
    assert exc.value.code == "DIGEST_MISMATCH"
    assert host.spawned == [] and agent.current_sha() == OLD
    assert agent.read_job(NEW) is None


@pytest.mark.parametrize("archive_kwargs,code", [
    ({"extra": {"../evil": b"x"}}, "ARCHIVE_INVALID"),
    ({"extra": {"wheels/run.sh": b"x"}}, "ARCHIVE_INVALID"),
    ({"manifest_override": {"sha": "c" * 40}}, "MANIFEST_SHA"),
    ({"manifest_override": {"wheels": {"wheels/x.whl": "0" * 64}}}, "MANIFEST_MISMATCH"),
])
def test_bad_archives_fail_without_touching_production(env, archive_kwargs, code):
    agent, host = env
    assert deploy(agent, archive=make_archive(NEW, **archive_kwargs)) == "FAILED"
    assert agent.status(NEW) == f"FAILED {code}"
    assert agent.current_sha() == OLD
    assert not (agent.releases / NEW).exists()
    assert not any(c[:2] == ["systemctl", "restart"] for c in host.calls)


def test_tampered_wheel_digest_fails(env):
    agent, _ = env
    wheels = {"wheels/funpayflow-1.0-py3-none-any.whl": b"good"}
    data, digest = make_archive(NEW, wheels)
    data = data.replace(b"good", b"evil")
    digest = hashlib.sha256(data).hexdigest()
    assert agent.receive(NEW, digest, io.BytesIO(data)) == "ACCEPTED"
    assert agent.work(NEW) == "FAILED"
    assert agent.status(NEW) == "FAILED WHEEL_DIGEST"


@pytest.mark.parametrize("setup,code", [
    (lambda h: setattr(h, "legacy", "active"), "LEGACY_ACTIVE"),
    (lambda h: setattr(h, "dropins", "/etc/systemd/system/x.d/o.conf"), "UNIT_DROPIN"),
    (lambda h: setattr(h, "exec_path", "/root/other/python"), "UNIT_NOT_ON_CURRENT"),
    (lambda h: setattr(h, "pip_rc", 1), "INSTALL_FAILED"),
    (lambda h: setattr(h, "free", 10), "LOW_DISK"),
])
def test_preflight_failures_keep_old_release(env, setup, code):
    agent, host = env
    setup(host)
    assert deploy(agent) == "FAILED"
    assert agent.status(NEW) == f"FAILED {code}"
    assert agent.current_sha() == OLD
    assert not any(c[:2] == ["systemctl", "restart"] for c in host.calls)


def test_corrupt_database_blocks_switch(env):
    agent, host = env
    (agent.data / "state.sqlite3").write_bytes(b"not a database" * 100)
    assert deploy(agent) == "FAILED"
    assert agent.current_sha() == OLD


def test_concurrent_and_duplicate_requests_rejected(env):
    agent, host = env
    data, digest = make_archive(NEW)
    assert agent.receive(NEW, digest, io.BytesIO(data)) == "ACCEPTED"
    other = "c" * 40
    d2, g2 = make_archive(other)
    with pytest.raises(agent_mod.Rejected) as exc:
        agent.receive(other, g2, io.BytesIO(d2))
    assert exc.value.code == "BUSY"
    with pytest.raises(agent_mod.Rejected) as exc:
        agent.receive(NEW, digest, io.BytesIO(data))
    assert exc.value.code == "BUSY"
    assert agent.work(NEW) == "DEPLOYED"
    assert agent.receive(NEW, digest, io.BytesIO(data)) == "ALREADY_DEPLOYED"


def test_lock_held_rejects(env):
    agent, _ = env
    fd = agent.lock()
    try:
        with pytest.raises(agent_mod.Rejected) as exc:
            agent.status(NEW) and agent.lock()
        assert exc.value.code == "BUSY"
    finally:
        os.close(fd)


@pytest.mark.parametrize("phase,result", [
    ("PREPARING", "FAILED"), ("BACKED_UP", "FAILED"),
    ("SWITCHED", "NEEDS_OWNER"), ("HEALTH_CHECK", "NEEDS_OWNER"),
])
def test_interrupted_job_is_never_resumed(env, phase, result):
    agent, host = env
    agent.write_job({"sha": NEW, "digest": "0" * 64, "phase": phase, "previous": OLD})
    assert agent.work(NEW) == result
    assert agent.status(NEW) == f"{result} INTERRUPTED"
    assert host.calls == []


def test_redeploy_after_rollback_cleans_stale_release(env):
    agent, host = env
    host.bad_releases.add(NEW)
    assert deploy(agent) == "ROLLED_BACK"
    host.bad_releases.clear()
    assert deploy(agent) == "DEPLOYED"


def test_manual_rollback(env):
    agent, host = env
    assert deploy(agent) == "DEPLOYED"
    with pytest.raises(agent_mod.Rejected):
        agent.manual_rollback(OLD)
    assert agent.manual_rollback(NEW) == "ROLLED_BACK"
    assert agent.current_sha() == OLD


def test_prune_keeps_current_and_previous(env):
    agent, _ = env
    shas = [c * 40 for c in "cdef"]
    for sha in shas:
        assert deploy(agent, sha) == "DEPLOYED"
    kept = sorted(p.name for p in agent.releases.iterdir())
    assert len(kept) == 3 and shas[-1] in kept and shas[-2] in kept


@pytest.mark.parametrize("command", [
    None, "", "deploy " + NEW, "deploy " + NEW + " " + "0" * 64 + " ",
    "status " + NEW.upper(), "status " + NEW + "; rm -rf /", "shell", "rollback",
    "deploy " + NEW[:-1] + " " + "0" * 64,
])
def test_request_grammar_rejects_everything_else(command):
    with pytest.raises(agent_mod.Rejected):
        agent_mod.parse_request(command)


def test_entrypoint_output_is_fixed_tokens(env):
    agent, _ = env
    out = io.StringIO()
    rc = agent_mod.main([], {"SSH_ORIGINAL_COMMAND": "status " + NEW}, io.BytesIO(),
                        out, agent_factory=lambda: agent)
    assert rc == 0 and out.getvalue() == "UNKNOWN_JOB\n"
    out = io.StringIO()
    rc = agent_mod.main(["--evil"], {}, io.BytesIO(), out, agent_factory=lambda: agent)
    assert rc == 2 and out.getvalue() == "REJECTED BAD_REQUEST\n"
    out = io.StringIO()
    rc = agent_mod.main([], {"SSH_ORIGINAL_COMMAND": "cat /etc/shadow"}, io.BytesIO(),
                        out, agent_factory=lambda: agent)
    assert rc == 2 and out.getvalue() == "REJECTED BAD_REQUEST\n"


def test_config_validation():
    with pytest.raises(agent_mod.Rejected):
        agent_mod.validate_config({"root": "relative"})
    with pytest.raises(agent_mod.Rejected):
        agent_mod.load_config(Path("/nonexistent/funpayflow/config.json"))
