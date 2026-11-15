#!/usr/bin/python3 -I
"""FunPayFlow VPS deploy agent: PREPARE -> BACKUP -> SWITCH -> HEALTH -> ROLLBACK.

Installed once by the owner as a root-owned file (see docs/VPS_AUTO_UPDATE.md)
and reached only through an SSH forced command of the dedicated deploy user:

    authorized_keys: restrict,command="sudo -n /usr/local/sbin/funpayflow-deploy-agent" ssh-ed25519 ...

Requests (SSH_ORIGINAL_COMMAND), nothing else is accepted:

    deploy <sha40> <sha256>   release tar on stdin; verified, then a detached
                              worker (systemd-run) performs the deployment
    status <sha40>            read-only; prints the persisted phase
    rollback <sha40>          <sha40> must be the current release; switches back
                              to the recorded previous release

Safety properties:
* standard library only; never imports application code or reads .env;
* wheels-only install (--no-index --no-deps --only-binary); nothing is built here;
* one operation at a time (flock + persisted job phase); a job is never resumed
  or retried blindly: an interrupted job after the switch becomes NEEDS_OWNER;
* SQLite backup through the backup API plus integrity_check before the switch;
* atomic symlink switch; automatic rollback when health fails;
* output is a fixed token vocabulary, no paths, logs, exceptions or input echo.
"""
from __future__ import annotations

from contextlib import closing
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time

CONFIG_PATH = Path("/etc/funpayflow-deploy/config.json")
SHA_RE = re.compile(r"[0-9a-f]{40}")
DIGEST_RE = re.compile(r"[0-9a-f]{64}")
WHEEL_RE = re.compile(r"wheels/[A-Za-z0-9_.+-]{1,200}\.whl")
MAX_ARCHIVE = 200 * 1024 * 1024
MAX_MEMBERS = 400
CHUNK = 1024 * 1024

TERMINAL = {"DEPLOYED", "ROLLED_BACK", "FAILED", "NEEDS_OWNER", "ALREADY_DEPLOYED"}
# NEEDS_OWNER is final for its job and blocks every new operation until the owner
# inspects the server and removes that job record manually.
SETTLED = TERMINAL - {"NEEDS_OWNER"}
# Phases after which production may have been touched.
TOUCHED = {"SWITCHED", "HEALTH_CHECK", "ROLLING_BACK"}

CONFIG_KEYS = {
    "root": str,            # agent state: jobs/, incoming/, lock
    "releases": str,        # releases/<sha>/{manifest.json,wheels/,venv/}
    "current": str,         # symlink -> releases/<sha>, referenced by the unit
    "data_dir": str,        # FUNPAY_BOT_DATA_DIR of the running service
    "backups": str,
    "service": str,         # e.g. funpayflow.service
    "forbidden_services": list,  # must be inactive, e.g. ["funpay-bot.service"]
    "python": str,          # absolute interpreter used to create release venvs
    "keep_releases": int,
    "health_timeout": int,  # seconds
    "health_stable": int,   # seconds the service must stay up without restarts
    "require_heartbeat": bool,
    "min_free_mb": int,
}


class Rejected(Exception):
    """Fixed, non-sensitive code only; never carries input or paths."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def require(condition: bool, code: str) -> None:
    if not condition:
        raise Rejected(code)


# --------------------------------------------------------------------------- host
class Host:
    """Side effects in one place so tests can substitute a fake Linux host."""

    ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}

    def run(self, argv: list[str], timeout: int = 300) -> tuple[int, str]:
        try:
            done = subprocess.run(argv, capture_output=True, text=True, env=self.ENV,
                                  timeout=timeout, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            return 127, ""
        return done.returncode, done.stdout

    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def free_mb(self, path: Path) -> int:
        return shutil.disk_usage(path).free // (1024 * 1024)

    def spawn_worker(self, sha: str) -> bool:
        rc, _ = self.run(["systemd-run", "--quiet", "--collect",
                          f"--unit=funpayflow-deploy-{sha[:12]}",
                          sys.executable, "-I", str(Path(__file__).resolve()),
                          "--worker", sha], timeout=60)
        return rc == 0


# ------------------------------------------------------------------------- config
def load_config(path: Path = CONFIG_PATH) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise Rejected("CONFIG_INVALID") from None
    return validate_config(raw)


def validate_config(raw: object) -> dict:
    require(isinstance(raw, dict) and set(raw) == set(CONFIG_KEYS), "CONFIG_INVALID")
    for key, kind in CONFIG_KEYS.items():
        value = raw[key]
        require(type(value) is kind, "CONFIG_INVALID")
        if kind is str and key not in ("service",):
            require(value.startswith("/") and ".." not in value.split("/"),
                    "CONFIG_INVALID")
    for name in [raw["service"], *raw["forbidden_services"]]:
        require(isinstance(name, str)
                and re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,60}\.service", name) is not None,
                "CONFIG_INVALID")
    require(raw["keep_releases"] >= 2 and raw["health_timeout"] > raw["health_stable"] >= 10,
            "CONFIG_INVALID")
    return dict(raw)


# -------------------------------------------------------------------------- state
class Agent:
    def __init__(self, config: dict, host: Host | None = None):
        self.c = config
        self.host = host or Host()
        self.root = Path(config["root"])
        self.jobs = self.root / "jobs"
        self.incoming = self.root / "incoming"
        self.releases = Path(config["releases"])
        self.current = Path(config["current"])
        self.data = Path(config["data_dir"])
        self.backups = Path(config["backups"])
        for d in (self.root, self.jobs, self.incoming, self.releases, self.backups):
            d.mkdir(mode=0o700, parents=True, exist_ok=True)

    # job records ---------------------------------------------------------------
    def job_path(self, sha: str) -> Path:
        return self.jobs / f"{sha}.json"

    def read_job(self, sha: str) -> dict | None:
        try:
            return json.loads(self.job_path(sha).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return {"sha": sha, "phase": "NEEDS_OWNER", "code": "STATE_CORRUPT"}

    def write_job(self, job: dict) -> None:
        job["updated"] = int(self.host.now())
        tmp = self.job_path(job["sha"]).with_suffix(".tmp")
        tmp.write_text(json.dumps(job, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.job_path(job["sha"]))

    def phase(self, job: dict, phase: str, code: str = "") -> None:
        job["phase"] = phase
        if code:
            job["code"] = code
        self.write_job(job)

    def active_jobs(self) -> list[dict]:
        out = []
        for path in self.jobs.glob("*.json"):
            job = self.read_job(path.stem)
            if job and job.get("phase") not in SETTLED:
                out.append(job)
        return out

    def lock(self):
        fd = os.open(self.root / "lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise Rejected("BUSY") from None
        return fd

    # releases -------------------------------------------------------------------
    def current_sha(self) -> str | None:
        try:
            target = os.readlink(self.current)
        except OSError:
            return None
        name = Path(target).name
        if SHA_RE.fullmatch(name) and Path(target).parent == self.releases:
            return name
        return None

    def switch_to(self, sha: str) -> None:
        tmp = self.current.with_name(self.current.name + ".switch")
        if tmp.is_symlink() or tmp.exists():
            tmp.unlink()
        os.symlink(self.releases / sha, tmp)
        os.replace(tmp, self.current)

    # ------------------------------------------------------------- deploy: receive
    def receive(self, sha: str, digest: str, stream) -> str:
        """Store and verify the uploaded release, then hand off to the worker."""
        fd = self.lock()
        try:
            require(not self.active_jobs(), "BUSY")
            if self.current_sha() == sha:
                return "ALREADY_DEPLOYED"
            previous = self.read_job(sha)
            require(previous is None or previous.get("phase") in ("FAILED", "ROLLED_BACK"),
                    "JOB_EXISTS")
            stale = self.releases / sha
            if stale.exists() or stale.is_symlink():
                # Leftover of an earlier FAILED/ROLLED_BACK attempt, never current.
                require(previous is not None and not stale.is_symlink(), "RELEASE_EXISTS")
                shutil.rmtree(stale)
            archive = self.incoming / f"{sha}.tar"
            got = self._store(stream, archive)
            if got != digest:
                archive.unlink(missing_ok=True)
                raise Rejected("DIGEST_MISMATCH")
            self.write_job({"sha": sha, "digest": digest, "phase": "RECEIVED",
                            "previous": self.current_sha()})
        finally:
            os.close(fd)
        if not self.host.spawn_worker(sha):
            job = self.read_job(sha)
            self.phase(job, "FAILED", "WORKER_NOT_STARTED")
            raise Rejected("WORKER_NOT_STARTED")
        return "ACCEPTED"

    def _store(self, stream, archive: Path) -> str:
        h = hashlib.sha256()
        size = 0
        with open(archive, "wb") as out:
            while chunk := stream.read(CHUNK):
                size += len(chunk)
                if size > MAX_ARCHIVE:
                    out.close()
                    archive.unlink(missing_ok=True)
                    raise Rejected("ARCHIVE_TOO_LARGE")
                h.update(chunk)
                out.write(chunk)
        require(size > 0, "ARCHIVE_EMPTY")
        return h.hexdigest()

    # --------------------------------------------------------------- deploy: worker
    def work(self, sha: str) -> str:
        fd = self.lock()
        try:
            job = self.read_job(sha)
            require(job is not None, "NO_JOB")
            if job["phase"] != "RECEIVED":
                # Never resume a half-done job: production state is not known.
                if job["phase"] in TERMINAL:
                    return job["phase"]
                self.phase(job, "NEEDS_OWNER" if job["phase"] in TOUCHED else "FAILED",
                           "INTERRUPTED")
                return job["phase"]
            try:
                self._prepare(job)
                self._preflight(job)
                self._backup(job)
            except Rejected as exc:
                shutil.rmtree(self.releases / sha, ignore_errors=True)
                self.phase(job, "FAILED", exc.code)
                return "FAILED"
            except Exception:
                shutil.rmtree(self.releases / sha, ignore_errors=True)
                self.phase(job, "FAILED", "PREPARE_ERROR")
                return "FAILED"
            try:
                return self._cutover(job)
            except Exception:
                job = self.read_job(sha) or job
                touched = job.get("phase") in TOUCHED
                self.phase(job, "NEEDS_OWNER" if touched else "FAILED", "CUTOVER_ERROR")
                return job["phase"]
        finally:
            (self.incoming / f"{sha}.tar").unlink(missing_ok=True)
            os.close(fd)

    def _prepare(self, job: dict) -> None:
        sha = job["sha"]
        self.phase(job, "PREPARING")
        require(self.host.free_mb(self.releases) >= self.c["min_free_mb"], "LOW_DISK")
        release = self.releases / sha
        require(not release.exists(), "RELEASE_EXISTS")
        archive = self.incoming / f"{sha}.tar"
        with open(archive, "rb") as f:
            require(hashlib.file_digest(f, "sha256").hexdigest() == job["digest"],
                    "DIGEST_MISMATCH")
        release.mkdir(mode=0o755)
        manifest = extract_release(archive, release)
        require(manifest["sha"] == sha, "MANIFEST_SHA")
        wheels = sorted(str(release / name) for name in manifest["wheels"])
        venv = release / "venv"
        rc, _ = self.host.run([self.c["python"], "-I", "-m", "venv", str(venv)])
        require(rc == 0, "VENV_FAILED")
        py = str(venv / "bin" / "python")
        rc, _ = self.host.run([py, "-I", "-m", "pip", "install", "--quiet",
                               "--no-index", "--no-deps", "--only-binary=:all:",
                               "--disable-pip-version-check", *wheels], timeout=900)
        require(rc == 0, "INSTALL_FAILED")
        rc, _ = self.host.run([py, "-I", "-m", "pip", "check",
                               "--disable-pip-version-check"])
        require(rc == 0, "DEPENDENCIES_BROKEN")
        self.phase(job, "PREPARED")

    def _preflight(self, job: dict) -> None:
        for name in self.c["forbidden_services"]:
            rc, out = self.host.run(["systemctl", "is-active", name], timeout=30)
            require(out.strip() in ("inactive", "failed", "unknown", ""), "LEGACY_ACTIVE")
        state = self._service_state()
        require(state["LoadState"] == "loaded", "SERVICE_MISSING")
        require(state["DropInPaths"] == "", "UNIT_DROPIN")
        require(str(self.current) in state["ExecStart"], "UNIT_NOT_ON_CURRENT")
        require(self.current_sha() == job["previous"], "CURRENT_CHANGED")

    def _service_state(self) -> dict:
        props = ["LoadState", "ActiveState", "SubState", "MainPID", "NRestarts",
                 "DropInPaths", "ExecStart", "ActiveEnterTimestampMonotonic"]
        rc, out = self.host.run(["systemctl", "show", self.c["service"],
                                 "--property=" + ",".join(props)], timeout=30)
        require(rc == 0, "SYSTEMCTL_FAILED")
        state = dict.fromkeys(props, "")
        for line in out.splitlines():
            key, sep, value = line.partition("=")
            if sep and key in state:
                state[key] = value.strip()
        return state

    def _backup(self, job: dict) -> None:
        source = self.data / "state.sqlite3"
        require(source.is_file() and not source.is_symlink(), "DB_MISSING")
        target_dir = self.backups / f"{int(self.host.now())}-{job['sha'][:12]}"
        target_dir.mkdir(mode=0o700)
        target = target_dir / "state.sqlite3"
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True,
                                     timeout=30)) as src, \
                closing(sqlite3.connect(target)) as dst:
            src.backup(dst)
        with closing(sqlite3.connect(target.as_uri() + "?mode=ro", uri=True)) as db:
            ok = db.execute("PRAGMA integrity_check").fetchone()
        require(ok == ("ok",), "BACKUP_CORRUPT")
        settings = self.data / "bot_settings.json"
        if settings.is_file() and not settings.is_symlink():
            shutil.copy2(settings, target_dir / "bot_settings.json")
        os.chmod(target, 0o600)
        job["backup"] = target_dir.name
        self.phase(job, "BACKED_UP")

    def _cutover(self, job: dict) -> str:
        sha, previous = job["sha"], job["previous"]
        before = self._service_state()
        self.phase(job, "SWITCHED")
        try:
            self.switch_to(sha)
            self.restart()
            self.phase(job, "HEALTH_CHECK")
            healthy = self.healthy(sha, before, self.c["require_heartbeat"])
        except Exception:
            healthy = False
        if healthy:
            self.phase(job, "DEPLOYED")
            self.prune(keep={sha, previous})
            return "DEPLOYED"
        job["code"] = "HEALTH_FAILED"
        return self._rollback(job, previous)

    def _rollback(self, job: dict, previous: str | None) -> str:
        self.phase(job, "ROLLING_BACK")
        if previous is None or not (self.releases / previous).is_dir():
            self.phase(job, "NEEDS_OWNER", "NO_PREVIOUS_RELEASE")
            return "NEEDS_OWNER"
        try:
            before = self._service_state()
            self.switch_to(previous)
            self.restart()
            ok = self.healthy(previous, before, heartbeat=False)
        except Exception:
            ok = False
        if ok:
            self.phase(job, "ROLLED_BACK")
            return "ROLLED_BACK"
        self.phase(job, "NEEDS_OWNER", "ROLLBACK_FAILED")
        return "NEEDS_OWNER"

    def restart(self) -> None:
        rc, _ = self.host.run(["systemctl", "restart", self.c["service"]], timeout=120)
        require(rc == 0, "RESTART_FAILED")

    def healthy(self, sha: str, before: dict, heartbeat: bool) -> bool:
        started = self.host.now()
        deadline = started + self.c["health_timeout"]
        restarts = before.get("NRestarts") or "0"
        stable_since = None
        pid = None
        while self.host.now() < deadline:
            s = self._service_state()
            up = s["ActiveState"] == "active" and s["SubState"] == "running" \
                and s["MainPID"] not in ("", "0") and s["NRestarts"] == restarts \
                and s["ActiveEnterTimestampMonotonic"] != before.get(
                    "ActiveEnterTimestampMonotonic")
            if s["ActiveState"] == "failed" or s["NRestarts"] != restarts:
                return False
            if up and s["MainPID"] == pid:
                if self.host.now() - stable_since >= self.c["health_stable"] and (
                        not heartbeat or self.heartbeat_ok(sha, started)):
                    return True
            elif up:
                pid, stable_since = s["MainPID"], self.host.now()
            else:
                pid = stable_since = None
            self.host.sleep(2)
        return False

    def heartbeat_ok(self, sha: str, since: float) -> bool:
        try:
            beat = json.loads((self.data / "health.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return (isinstance(beat, dict) and beat.get("sha") == sha
                and beat.get("runner") == "ok"
                and isinstance(beat.get("time"), (int, float)) and beat["time"] >= since)

    def prune(self, keep: set) -> None:
        dirs = sorted((p for p in self.releases.iterdir()
                       if p.is_dir() and not p.is_symlink() and SHA_RE.fullmatch(p.name)),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        for old in dirs[self.c["keep_releases"]:]:
            if old.name not in keep:
                shutil.rmtree(old, ignore_errors=True)

    # -------------------------------------------------------------- status/rollback
    def status(self, sha: str) -> str:
        job = self.read_job(sha)
        if job is None:
            return "ALREADY_DEPLOYED" if self.current_sha() == sha else "UNKNOWN_JOB"
        code = job.get("code", "")
        return job["phase"] + (f" {code}" if code else "")

    def manual_rollback(self, sha: str) -> str:
        fd = self.lock()
        try:
            require(not self.active_jobs(), "BUSY")
            require(self.current_sha() == sha, "NOT_CURRENT")
            job = self.read_job(sha)
            require(job is not None and job.get("phase") == "DEPLOYED", "NO_JOB")
            return self._rollback(job, job.get("previous"))
        finally:
            os.close(fd)


# ------------------------------------------------------------------------ archive
def extract_release(archive: Path, release: Path) -> dict:
    """Extract only manifest.json and hash-verified wheels/*.whl regular files."""
    with tarfile.open(archive, "r:") as tar:
        members = tar.getmembers()
        require(0 < len(members) <= MAX_MEMBERS, "ARCHIVE_INVALID")
        by_name = {}
        for m in members:
            if m.isdir() and m.name in ("wheels", "wheels/"):
                continue
            require(m.isfile() and m.name not in by_name
                    and (m.name == "manifest.json" or WHEEL_RE.fullmatch(m.name)),
                    "ARCHIVE_INVALID")
            by_name[m.name] = m
        require("manifest.json" in by_name, "ARCHIVE_INVALID")
        raw = tar.extractfile(by_name["manifest.json"]).read(1024 * 1024)
        try:
            manifest = json.loads(raw)
        except ValueError:
            raise Rejected("MANIFEST_INVALID") from None
        require(isinstance(manifest, dict) and SHA_RE.fullmatch(str(manifest.get("sha")))
                and isinstance(manifest.get("wheels"), dict) and manifest["wheels"],
                "MANIFEST_INVALID")
        wheel_members = {n for n in by_name if n != "manifest.json"}
        require(set(manifest["wheels"]) == wheel_members, "MANIFEST_MISMATCH")
        (release / "wheels").mkdir(mode=0o755)
        for name, expected in manifest["wheels"].items():
            require(isinstance(expected, str) and DIGEST_RE.fullmatch(expected),
                    "MANIFEST_INVALID")
            data = tar.extractfile(by_name[name]).read()
            require(hashlib.sha256(data).hexdigest() == expected, "WHEEL_DIGEST")
            (release / name).write_bytes(data)
        (release / "manifest.json").write_bytes(raw)
    return manifest


# --------------------------------------------------------------------------- entry
def parse_request(command: object) -> tuple[str, str, str | None]:
    require(isinstance(command, str), "BAD_REQUEST")
    m = re.fullmatch(r"(deploy) ([0-9a-f]{40}) ([0-9a-f]{64})|(status|rollback) ([0-9a-f]{40})",
                     command)
    require(m is not None, "BAD_REQUEST")
    if m.group(1):
        return "deploy", m.group(2), m.group(3)
    return m.group(4), m.group(5), None


def main(argv: list[str], environ: dict, stdin, stdout,
         agent_factory=None) -> int:
    try:
        agent = (agent_factory or (lambda: Agent(load_config())))()
        if argv[:1] == ["--worker"] and len(argv) == 2 and SHA_RE.fullmatch(argv[1]):
            result = agent.work(argv[1])
        else:
            require(not argv, "BAD_REQUEST")
            action, sha, digest = parse_request(environ.get("SSH_ORIGINAL_COMMAND"))
            if action == "deploy":
                result = agent.receive(sha, digest, stdin)
            elif action == "status":
                result = agent.status(sha)
            else:
                result = agent.manual_rollback(sha)
        stdout.write(result + "\n")
        return 0
    except Rejected as exc:
        stdout.write(f"REJECTED {exc.code}\n")
        return 2
    except Exception:
        stdout.write("REJECTED INTERNAL\n")
        return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], dict(os.environ), sys.stdin.buffer, sys.stdout))
