"""Fixed Linux isolation probe, not an installer or a deployment capability.

An owner-provisioned, sealed minimal rootfs is mandatory. Only reviewed stdlib
probe code runs, as a DynamicUser, in that rootfs. No package manager/build hooks,
source imports, SSH, downloads or production inspection occur. Import is passive.
The future trusted caller supplies trusted_source_digest() evidence from the fixed
archive; a caller-selected digest is not itself source provenance.
"""
from __future__ import annotations

import argparse
from enum import Enum
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
import subprocess
import sys
import time
import tomllib

_spec = importlib.util.spec_from_file_location(
    "wheel_source_protocol", Path(__file__).with_name("vps_update_prepare.py"))
source = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(source)
TARGET = source.TARGET
BASE = PurePosixPath("/var/lib/funpayflow-wheel-builder")
ROOTFS = BASE / "rootfs"
SNAPSHOT = BASE / ("source-" + TARGET[:8])
CONFIG = BASE / "bootstrap.json"
JOBS = BASE / "jobs"
ENV = {"PATH": "/usr/bin", "LC_ALL": "C"}
MAX_IMAGE = 256 * 1024 * 1024
MAX_JOBS = 64


class Status(str, Enum):
    ISOLATION_READY = "ISOLATION_READY"
    ISOLATION_MISSING = "ISOLATION_MISSING"
    ISOLATION_REFUSED = "ISOLATION_REFUSED"
    # Reserved: this probe never produces an environment artifact.
    ENV_PREPARED = "ENV_PREPARED"


class Refused(ValueError):
    pass


def _require(condition, reason):
    if not condition:
        raise Refused(reason)


def _result(status, reason, run_id=""):
    return {"status": status.value, "reason": reason, "target": TARGET,
            "deploy_ready": "NO", "run_id": run_id}


def _owned(st, directory, device=None):
    _require(st.st_uid == 0 and st.st_gid == 0 and not st.st_mode & 0o7022
             and (stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode))
             and (directory or st.st_nlink == 1)
             and (device is None or st.st_dev == device), "UNSAFE_PERMISSIONS")


def _open_directory(path):
    """Walk every ancestor without symlinks, including the final component."""
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _owned(os.fstat(fd), True)
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            _owned(os.fstat(fd), True)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read(fd, name, limit, deadline=None):
    deadline = time.monotonic() + 5 if deadline is None else deadline
    expected = os.stat(name, dir_fd=fd, follow_symlinks=False)
    _owned(expected, False, os.fstat(fd).st_dev)
    child = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=fd)
    try:
        before = os.fstat(child)
        _owned(before, False, os.fstat(fd).st_dev)
        _require((expected.st_dev, expected.st_ino) == (before.st_dev, before.st_ino), "MUTATION")
        _require(before.st_size <= limit, "SIZE_LIMIT")
        data = bytearray()
        while block := os.read(child, min(65536, limit + 1 - len(data))):
            data.extend(block)
            _require(len(data) <= limit, "SIZE_LIMIT")
            _require(time.monotonic() <= deadline, "SCAN_TIMEOUT")
        after = os.fstat(child)
        _require((before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
                 (len(data), after.st_mtime_ns, after.st_ctime_ns), "MUTATION")
        return bytes(data)
    finally:
        os.close(child)


def _tree(path, *, image=False):
    """Same source digest format as PR #133; no private names read, no links/mounts."""
    entries, total = {}, 0
    deadline = time.monotonic() + 15
    fd = _open_directory(path)
    device = os.fstat(fd).st_dev
    placeholders = {"proc", "sys", "dev", "run", "tmp", "work", "input"}

    def walk(parent, prefix=""):
        nonlocal total
        before = os.fstat(parent)
        with os.scandir(parent) as iterator:
            for entry in iterator:
                name = prefix + entry.name
                _require(source._safe_name(name) and len(entries) < (16000 if image else source.MAX_ENTRIES)
                         and time.monotonic() < deadline, "UNSAFE_TREE")
                if image:
                    # No host /etc, /root, /home, /var or credential/log tree.
                    _require(name in placeholders or name in {"usr", "usr/bin", "usr/lib", "lib", "lib64"}
                             or name in {"usr/bin/env", "usr/bin/python3.13"}
                             or name.startswith(("usr/lib/", "lib/", "lib64/")), "UNSAFE_IMAGE")
                st = os.stat(entry.name, dir_fd=parent, follow_symlinks=False)
                directory = stat.S_ISDIR(st.st_mode)
                _owned(st, directory, device)
                if directory:
                    child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=parent)
                    try:
                        opened = os.fstat(child)
                        _owned(opened, True, device)
                        _require((st.st_dev, st.st_ino) == (opened.st_dev, opened.st_ino), "MUTATION")
                        entries[name] = ["dir"]
                        walk(child, name + "/")
                    finally:
                        os.close(child)
                else:
                    data = _read(parent, entry.name, 32 * 1024 * 1024 if image else source.MAX_FILE,
                                 deadline)
                    total += len(data)
                    _require(total <= (MAX_IMAGE if image else source.MAX_SOURCE), "SIZE_LIMIT")
                    entries[name] = ["file", len(data), hashlib.sha256(data).hexdigest()]
        after = os.fstat(parent)
        _require((before.st_mtime_ns, before.st_ctime_ns) ==
                 (after.st_mtime_ns, after.st_ctime_ns), "MUTATION")

    try:
        walk(fd)
    finally:
        os.close(fd)
    if image:
        _require(all(entries.get(p) == ["dir"] for p in placeholders)
                 and all(entries.get(p, [None])[0] == "file"
                         for p in ("usr/bin/env", "usr/bin/python3.13")), "INCOMPLETE_IMAGE")
    else:
        _require(len(entries) <= source.MAX_ENTRIES and all(
            entries.get(p, [None])[0] == "file" for p in
            ("uv.lock", "pyproject.toml", "src/funpayflow/main.py")), "INCOMPLETE_SOURCE")
    return source._digest(entries)


def _mounts():
    # Refuse bind mounts too (st_dev alone does not detect them). Escaped paths
    # under our fixed ASCII base are refused rather than decoded ambiguously.
    with open("/proc/self/mountinfo", encoding="ascii") as stream:
        raw = stream.read(1024 * 1024 + 1)
    _require(len(raw) <= 1024 * 1024, "UNEXPECTED_MOUNT")
    for line in raw.splitlines():
        fields = line.split()
        _require(len(fields) >= 10 and " - " in line, "UNEXPECTED_MOUNT")
        mount = fields[4]
        _require(not mount.startswith(str(BASE) + "/") and mount != str(BASE), "UNEXPECTED_MOUNT")


def _configuration(trusted_digest):
    _require(sys.platform == "linux" and os.getuid() == os.geteuid() == 0,
             "TRUSTED_ROOT_BROKER_REQUIRED")
    _require(isinstance(trusted_digest, str) and re.fullmatch(r"[0-9a-f]{64}", trusted_digest),
             "TRUSTED_SOURCE_DIGEST_REQUIRED")
    _mounts()
    fd = _open_directory(BASE)
    try:
        config = json.loads(_read(fd, CONFIG.name, 4096))
    finally:
        os.close(fd)
    _require(type(config) is dict and set(config) == {"protocol", "rootfs_digest"}
             and type(config["protocol"]) is int and config["protocol"] == 1
             and isinstance(config["rootfs_digest"], str)
             and re.fullmatch(r"[0-9a-f]{64}", config["rootfs_digest"]), "BOOTSTRAP_CONTRACT")
    _require(_tree(ROOTFS, image=True) == config["rootfs_digest"], "IMAGE_DIGEST_MISMATCH")
    _require(_tree(SNAPSHOT) == trusted_digest, "SOURCE_DIGEST_MISMATCH")
    fd = _open_directory(JOBS)
    try:
        _require(stat.S_IMODE(os.fstat(fd).st_mode) == 0o700, "UNSAFE_PERMISSIONS")
        with os.scandir(fd) as iterator:
            for count, _ in enumerate(iterator, 1):
                _require(count < MAX_JOBS, "RESERVATION_LIMIT")
    finally:
        os.close(fd)
    for binary in (PurePosixPath("/usr/bin/systemd-run"), PurePosixPath("/usr/bin/systemctl")):
        fd = _open_directory(binary.parent)
        try:
            _owned(os.stat(binary.name, dir_fd=fd, follow_symlinks=False), False)
        finally:
            os.close(fd)


def check_configuration(*, trusted_digest):
    """Read-only bootstrap validator. Success alone never means ISOLATION_READY."""
    try:
        _configuration(trusted_digest)
        return _result(Status.ISOLATION_MISSING, "LIVE_PROBE_REQUIRED")
    except FileNotFoundError:
        return _result(Status.ISOLATION_MISSING, "BOOTSTRAP_REQUIRED")
    except Refused as exc:
        return _result(Status.ISOLATION_REFUSED, str(exc))
    except (OSError, ValueError, TypeError):
        return _result(Status.ISOLATION_REFUSED, "CONFIGURATION_UNVERIFIABLE")


# Fixed reviewed code, never application/lock-provided Python. The host sends no
# production paths for traversal and no file descriptors except empty stdin/pipe.
PROBE = r'''
import errno, json, os, socket, stat, sys
def require(value):
    if not value:
        raise ValueError('isolation')
require(sys.version_info[:2] == (3, 13))
require(os.getuid() == os.geteuid() != 0 and os.getgid() != 0 and 0 not in os.getgroups())
require(dict(os.environ) == {'PATH': '/usr/bin', 'LC_ALL': 'C'})
with open('/proc/self/status') as stream:
    status = dict(line.split(':', 1) for line in stream if ':' in line)
require(all(int(status[k].strip(), 16) == 0 for k in ('CapEff', 'CapPrm', 'CapBnd')))
require(status['NoNewPrivs'].strip() == '1' and status['Seccomp'].strip() == '2')
require(os.readlink('/proc/self/ns/mnt') != sys.argv[1])
require(os.readlink('/proc/self/ns/net') != sys.argv[2])
source = os.stat('/input')
require((source.st_dev, source.st_ino) == (int(sys.argv[3]), int(sys.argv[4])))
require(source.st_uid == source.st_gid == 0)
for path in ('/root', '/home', '/var/log', '/etc/shadow', '/run/credentials',
             '/run/systemd/private', '/proc/1/root', '/proc/1/environ'):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as exc:
        require(exc.errno in (errno.ENOENT, errno.EACCES, errno.EPERM, errno.ENXIO))
    else:
        os.close(fd)
        raise ValueError('forbidden access')
for family in (socket.AF_INET, socket.AF_INET6):
    try:
        sock = socket.socket(family, socket.SOCK_STREAM)
    except OSError as exc:
        require(exc.errno in (errno.EPERM, errno.EACCES, errno.EAFNOSUPPORT))
    else:
        sock.close()
        raise ValueError('network permitted')
with open('/proc/self/mountinfo') as stream:
    mounts = [line.split() for line in stream]
def mount(path):
    matches = [m for m in mounts if m[4] == path]
    require(len(matches) == 1)
    return matches[0]
for path in ('/', '/input'):
    require('ro' in mount(path)[5].split(','))
work = mount('/work')
require(work[work.index('-') + 1] == 'tmpfs')
require({'rw', 'nosuid', 'nodev', 'noexec'} <= set(work[5].split(',')))
require(os.statvfs('/work').f_blocks * os.statvfs('/work').f_frsize <= 16 * 1024 * 1024)
with open('/work/probe', 'xb') as stream:
    stream.write(b'probe-v1')
st = os.lstat('/work/probe')
require(stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and st.st_uid == os.getuid()
        and st.st_gid == os.getgid() and not st.st_mode & 0o077 and st.st_size == 8)
require(os.listdir('/work') == ['probe'])
with open('/input/pyproject.toml', 'rb') as stream:
    require(bool(stream.read(1)))
print(json.dumps({'protocol': 1, 'uid': os.getuid(), 'gid': os.getgid(), 'bytes': st.st_size}))
'''


def _command(run_id, namespaces):
    properties = {
        "DynamicUser": "yes", "NoNewPrivileges": "yes", "ProtectSystem": "strict",
        "ProtectHome": "yes", "RootDirectory": str(ROOTFS), "MountAPIVFS": "yes",
        "BindReadOnlyPaths": f"{SNAPSHOT}:/input", "PrivateNetwork": "yes",
        "PrivateDevices": "yes", "PrivateTmp": "no", "ProtectProc": "invisible",
        "ProcSubset": "pid", "RestrictAddressFamilies": "AF_UNIX",
        "CapabilityBoundingSet": "", "AmbientCapabilities": "", "UMask": "0077",
        "RestrictSUIDSGID": "yes", "RestrictRealtime": "yes", "RestrictNamespaces": "yes",
        "LockPersonality": "yes", "ProtectKernelTunables": "yes", "ProtectKernelModules": "yes",
        "ProtectKernelLogs": "yes", "ProtectControlGroups": "yes",
        "SystemCallFilter": "@system-service", "SystemCallErrorNumber": "EPERM",
        # RootDirectory already excludes host /tmp. Explicit bounded tmpfs avoids
        # PrivateTmp's host-backed /var/tmp mount and any unbounded scratch space.
        "TemporaryFileSystem": ("/work:rw,nosuid,nodev,noexec,size=16M,mode=1777 "
                                "/tmp:rw,nosuid,nodev,noexec,size=4M,mode=1777"),
        "WorkingDirectory": "/work", "RuntimeMaxSec": "20", "TimeoutStopSec": "3",
        "KillMode": "control-group", "SendSIGKILL": "yes", "MemoryMax": "128M",
        "MemorySwapMax": "0", "TasksMax": "8", "CPUQuota": "50%",
        "LimitFSIZE": "1048576", "LimitCORE": "0", "LimitNOFILE": "64",
        "StandardError": "null", "Restart": "no",
    }
    return ["/usr/bin/systemd-run", "--quiet", "--wait", "--pipe", "--collect",
            "--service-type=exec", "--unit=fpwf-probe-" + run_id,
            *(f"--property={key}={value}" for key, value in properties.items()),
            "--", "/usr/bin/env", "-i", "PATH=/usr/bin", "LC_ALL=C",
            "/usr/bin/python3.13", "-I", "-S", "-c", PROBE, *namespaces]


def probe_once(*, trusted_digest):
    """Explicit local trusted-broker operation; no fallback/retry/automatic cleanup.

    Reservations survive timeout/partial execution for owner reconciliation. systemd
    bounds the service independently of the client. Never wire this to GUI startup.
    """
    checked = check_configuration(trusted_digest=trusted_digest)
    if checked["reason"] != "LIVE_PROBE_REQUIRED":
        return checked
    run_id = secrets.token_hex(16)
    try:
        fd = _open_directory(JOBS)
        try:
            os.mkdir(run_id, mode=0o700, dir_fd=fd)  # EEXIST is never reused.
        finally:
            os.close(fd)
        namespaces = [os.readlink("/proc/self/ns/" + kind) for kind in ("mnt", "net")]
        fd = _open_directory(SNAPSHOT)
        try:
            identity = os.fstat(fd)
            namespaces.extend((str(identity.st_dev), str(identity.st_ino)))
        finally:
            os.close(fd)
        completed = subprocess.run(_command(run_id, namespaces), input="", capture_output=True,
                                   text=True, shell=False, cwd="/", env=ENV, timeout=28)
        _require(completed.returncode == 0 and len(completed.stdout) <= 1024,
                 "PROBE_FAILED")
        evidence = json.loads(completed.stdout)
        _require(type(evidence) is dict and set(evidence) == {"protocol", "uid", "gid", "bytes"}
                 and all(type(v) is int for v in evidence.values())
                 and evidence["protocol"] == 1 and evidence["bytes"] == 8
                 and 0 < evidence["uid"] < 2**32 and 0 < evidence["gid"] < 2**32,
                 "PROBE_EVIDENCE_INVALID")
        # Reattest inputs after the service, not just before dispatch.
        _configuration(trusted_digest)
        return _result(Status.ISOLATION_READY, "PROBE_VERIFIED", run_id)
    except FileExistsError:
        return _result(Status.ISOLATION_REFUSED, "RESERVATION_COLLISION", run_id)
    except subprocess.TimeoutExpired:
        # Stop only this transient probe. A timeout remains UNKNOWN even if stop
        # succeeds; never remove its reservation or retry the operation.
        try:
            subprocess.run(["/usr/bin/systemctl", "stop", "fpwf-probe-" + run_id + ".service"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, shell=False, cwd="/", env=ENV, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
        return _result(Status.ISOLATION_REFUSED, "DISPATCH_UNKNOWN", run_id)
    except Refused as exc:
        return _result(Status.ISOLATION_REFUSED, str(exc), run_id)
    except (OSError, ValueError, TypeError):
        return _result(Status.ISOLATION_REFUSED, "PROBE_UNVERIFIABLE", run_id)


def binary_lock_blocker(raw):
    """Static fail-closed runtime-graph preflight, not a wheel resolver.

    In particular an sdist-only dependency must never lead to a build-hook fallback.
    Even an all-wheel graph still requires a separate reviewed resolver/metadata and
    artifact verifier; this probe has no ENV_PREPARED path.
    """
    try:
        _require(type(raw) is bytes and len(raw) <= source.MAX_FILE, "LOCK_UNSUPPORTED")
        lock = tomllib.loads(raw.decode("utf-8"))
        _require(lock.get("version") == 1 and lock.get("revision") == 3
                 and lock.get("requires-python") == ">=3.13"
                 and not lock.get("resolution-markers"), "LOCK_UNSUPPORTED")
        packages = lock["package"]
        by_name = {p["name"]: p for p in packages}
        _require(len(packages) == len(by_name), "LOCK_UNSUPPORTED")
        project = by_name["funpayflow"]
        _require(project["source"] == {"editable": "."}, "LOCK_UNSUPPORTED")
        pending, visited = list(project.get("dependencies", [])), set()
        missing = False
        while pending:
            edge = pending.pop()
            _require(set(edge) == {"name"}, "LOCK_UNSUPPORTED")
            name = edge["name"]
            if name in visited:
                continue
            visited.add(name)
            package = by_name[name]
            _require(package["source"] == {"registry": "https://pypi.org/simple"}
                     and isinstance(package["version"], str), "LOCK_UNSUPPORTED")
            missing |= not bool(package.get("wheels"))
            pending.extend(package.get("dependencies", []))
        _require(bool(visited), "LOCK_UNSUPPORTED")
        return "BINARY_WHEELS_MISSING" if missing else "LOCK_RESOLVER_REQUIRED"
    except (Refused, KeyError, TypeError, ValueError, AttributeError):
        return "LOCK_UNSUPPORTED"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trusted-source-digest", required=True)
    parser.add_argument("--probe", action="store_true", help="explicit transient sandbox execution")
    args = parser.parse_args()
    operation = probe_once if args.probe else check_configuration
    result = operation(trusted_digest=args.trusted_source_digest)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == Status.ISOLATION_READY.value else 2


if __name__ == "__main__":
    raise SystemExit(main())
