"""Offline synthetic hosts only: no systemd, SSH, network or production reads."""
import builtins
from contextlib import nullcontext
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import stat
import subprocess
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "vps_isolated_wheel_builder.py"
spec = importlib.util.spec_from_file_location("isolated_builder_tests", SCRIPT)
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)


class Host:
    """Descriptor tree. Every operation is recorded; no fallback to the host OS."""
    O_RDONLY, O_DIRECTORY, O_NOFOLLOW, O_NONBLOCK = 0, 1, 2, 4

    def __init__(self):
        self.dirs = {"/", "/var", "/var/lib", str(b.BASE), str(b.ROOTFS), str(b.SNAPSHOT),
                     str(b.JOBS), "/usr", "/usr/bin"}
        self.files = {"/usr/bin/systemd-run": b"trusted", "/usr/bin/systemctl": b"trusted"}
        self.bad, self.fds, self.positions = {}, {}, {}
        self.reads, self.opens, self.created = [], [], []
        self.uid = 0
        self.mountinfo = "1 0 8:1 / / rw - ext4 /dev/sda rw\n"
        self.source_files = {"uv.lock": b"public lock", "pyproject.toml": b"public metadata",
                             "src/funpayflow/main.py": b"raise RuntimeError('never execute')"}
        for part in ("proc", "sys", "dev", "run", "tmp", "work", "input", "usr", "usr/bin", "usr/lib"):
            self.dirs.add(str(b.ROOTFS / part))
        self.files[str(b.ROOTFS / "usr/bin/env")] = b"trusted env binary"
        self.files[str(b.ROOTFS / "usr/bin/python3.13")] = b"trusted interpreter"
        for part in ("src", "src/funpayflow"):
            self.dirs.add(str(b.SNAPSHOT / part))
        for name, data in self.source_files.items():
            self.files[str(b.SNAPSHOT / name)] = data
        self.files[str(b.CONFIG)] = json.dumps({"protocol": 1, "rootfs_digest": self.digest(b.ROOTFS)}).encode()

    def digest(self, base):
        prefix = str(base) + "/"
        entries = {name[len(prefix):]: ["dir"] for name in self.dirs if name.startswith(prefix)}
        entries.update({name[len(prefix):]: ["file", len(data), hashlib.sha256(data).hexdigest()]
                        for name, data in self.files.items() if name.startswith(prefix)})
        return b.source._digest(entries)

    def getuid(self): return self.uid
    def geteuid(self): return self.uid

    def path(self, name, dir_fd=None):
        return self.fds[dir_fd].rstrip("/") + "/" + name if dir_fd is not None else name

    def st(self, path):
        if path not in self.dirs and path not in self.files:
            raise FileNotFoundError()
        mode = (stat.S_IFDIR | (0o700 if path == str(b.JOBS) else 0o755)
                if path in self.dirs else stat.S_IFREG | 0o644)
        values = dict(st_uid=0, st_gid=0, st_mode=mode, st_nlink=1, st_dev=1,
                      st_ino=hash(path), st_size=len(self.files.get(path, b"")),
                      st_mtime_ns=0, st_ctime_ns=0)
        values.update(self.bad.get(path, {}))
        return SimpleNamespace(**values)

    def open(self, name, flags, dir_fd=None):
        assert flags & self.O_NOFOLLOW
        path = self.path(name, dir_fd)
        self.opens.append(path)
        st = self.st(path)
        if stat.S_ISLNK(st.st_mode):
            raise OSError("symlink")
        fd = max(self.fds, default=0) + 1
        self.fds[fd], self.positions[fd] = path, 0
        return fd

    def fstat(self, fd): return self.st(self.fds[fd])
    def stat(self, name, *, dir_fd, follow_symlinks):
        assert not follow_symlinks
        return self.st(self.path(name, dir_fd))
    def close(self, fd): del self.fds[fd]

    def read(self, fd, size):
        path = self.fds[fd]
        self.reads.append(path)
        position = self.positions[fd]
        self.positions[fd] += size
        return self.files[path][position:position + size]

    def scandir(self, fd):
        prefix = self.fds[fd].rstrip("/") + "/"
        return nullcontext(iter(SimpleNamespace(name=name[len(prefix):])
                                for name in sorted(self.dirs | self.files.keys())
                                if name.startswith(prefix) and name != prefix
                                and "/" not in name[len(prefix):]))

    def mkdir(self, name, mode, *, dir_fd):
        assert mode == 0o700 and self.fds[dir_fd] == str(b.JOBS)
        path = self.path(name, dir_fd)
        if path in self.dirs:
            raise FileExistsError()
        self.dirs.add(path)
        self.created.append(path)

    def readlink(self, path):
        assert path in ("/proc/self/ns/mnt", "/proc/self/ns/net")
        return "mnt:[1]" if path.endswith("mnt") else "net:[2]"

    def text_open(self, path, **kwargs):
        assert path == "/proc/self/mountinfo"
        return io.StringIO(self.mountinfo)


@pytest.fixture
def host(monkeypatch):
    fake = Host()
    monkeypatch.setattr(b, "os", fake)
    monkeypatch.setattr(b, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(b, "open", fake.text_open, raising=False)
    monkeypatch.setattr(b.subprocess, "run", lambda *a, **k: pytest.fail("unexpected subprocess"))
    return fake


def check(host):
    result = b.check_configuration(trusted_digest=host.digest(b.SNAPSHOT))
    assert not host.fds
    return result


def test_passive_import_and_readonly_configuration_is_not_live_readiness(host):
    assert check(host)["reason"] == "LIVE_PROBE_REQUIRED"
    assert check(host)["status"] == "ISOLATION_MISSING"
    assert not host.created
    assert not any(path.startswith(("/root", "/home", "/etc")) for path in host.opens)
    assert b.Status.ENV_PREPARED.value == "ENV_PREPARED"


def test_missing_bootstrap_and_nonroot_broker_do_not_dispatch(host):
    del host.files[str(b.CONFIG)]
    assert check(host)["reason"] == "BOOTSTRAP_REQUIRED"
    host.uid = 1000
    assert check(host)["reason"] == "TRUSTED_ROOT_BROKER_REQUIRED"
    assert not host.created


@pytest.mark.parametrize("path,change", [
    (str(b.BASE), {"st_uid": 1000}), (str(b.BASE), {"st_gid": 1000}),
    ("/var/lib", {"st_mode": stat.S_IFDIR | 0o777}),
    (str(b.CONFIG), {"st_nlink": 2}),
    (str(b.ROOTFS / "usr/bin/python3.13"), {"st_mode": stat.S_IFREG | 0o4755}),
    (str(b.SNAPSHOT / "uv.lock"), {"st_mode": stat.S_IFLNK | 0o777}),
    (str(b.SNAPSHOT / "uv.lock"), {"st_uid": 1001}),
    (str(b.SNAPSHOT / "uv.lock"), {"st_dev": 2}),
    (str(b.JOBS), {"st_mode": stat.S_IFDIR | 0o755}),
])
def test_unsafe_permissions_links_and_device_changes_refused_before_dispatch(host, path, change):
    host.bad[path] = change
    assert check(host)["status"] == "ISOLATION_REFUSED"
    assert path not in host.reads


@pytest.mark.parametrize("mount", [str(b.ROOTFS), str(b.SNAPSHOT / "src"), str(b.BASE),
                                   str(b.JOBS / "bind")])
def test_unexpected_mount_including_same_device_bind_refused(host, mount):
    host.mountinfo += f"2 1 8:1 / {mount} rw - ext4 /dev/sda rw\n"
    assert check(host)["reason"] == "UNEXPECTED_MOUNT"
    assert not host.opens


@pytest.mark.parametrize("raw", [b"{}", b'{"protocol":true,"rootfs_digest":"x"}', b"[]",
                                  b'{"protocol":1,"rootfs_digest":123}', b"malformed"])
def test_invalid_bootstrap_contract_is_sanitized(host, raw):
    host.files[str(b.CONFIG)] = raw
    result = check(host)
    assert result["status"] == "ISOLATION_REFUSED"
    assert "malformed" not in json.dumps(result)


def test_wrong_source_and_image_hash_cannot_dispatch(host, monkeypatch):
    trusted = host.digest(b.SNAPSHOT)
    host.files[str(b.SNAPSHOT / "uv.lock")] = b"changed"
    assert b.probe_once(trusted_digest=trusted)["reason"] == "SOURCE_DIGEST_MISMATCH"
    host.files[str(b.ROOTFS / "usr/bin/env")] = b"changed interpreter"
    assert check(host)["reason"] == "IMAGE_DIGEST_MISMATCH"
    assert not host.created
    assert b.check_configuration(trusted_digest="x")["reason"] == "TRUSTED_SOURCE_DIGEST_REQUIRED"


@pytest.mark.parametrize("base,name", [(b.SNAPSHOT, ".env"), (b.SNAPSHOT, "private"),
                                      (b.ROOTFS, "etc/shadow"), (b.ROOTFS, "work/unexpected")])
def test_private_or_unexpected_entries_never_read(host, base, name):
    path = str(base / name)
    # Make any parent visible so that traversal really encounters the file.
    if "/" in name:
        host.dirs.add(str(base / name.rsplit("/", 1)[0]))
    host.files[path] = b"SYNTHETIC_NEVER_READ"
    assert check(host)["status"] == "ISOLATION_REFUSED"
    assert path not in host.reads


def successful_run(host, monkeypatch, response=None):
    calls = []
    monkeypatch.setattr(b.secrets, "token_hex", lambda n: "a" * 32)
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return response or SimpleNamespace(returncode=0, stdout=json.dumps(
            {"protocol": 1, "uid": 61111, "gid": 61111, "bytes": 8}))
    monkeypatch.setattr(b.subprocess, "run", run)
    result = b.probe_once(trusted_digest=host.digest(b.SNAPSHOT))
    return result, calls


def test_real_invocation_is_fixed_nonroot_chroot_networkless_bounded_and_clean(host, monkeypatch):
    result, calls = successful_run(host, monkeypatch)
    assert result["status"] == "ISOLATION_READY" and result["deploy_ready"] == "NO"
    assert len(calls) == 1 and host.created == [str(b.JOBS / ("a" * 32))]
    args, options = calls[0]
    assert args[0] == "/usr/bin/systemd-run" and "--pipe" in args
    required = {"DynamicUser=yes", "NoNewPrivileges=yes", "ProtectHome=yes", "ProtectSystem=strict",
                f"RootDirectory={b.ROOTFS}", f"BindReadOnlyPaths={b.SNAPSHOT}:/input",
                "PrivateNetwork=yes", "CapabilityBoundingSet=", "AmbientCapabilities=",
                "RestrictAddressFamilies=AF_UNIX", "ProtectProc=invisible", "ProcSubset=pid",
                "RuntimeMaxSec=20", "MemoryMax=128M", "KillMode=control-group",
                "PrivateTmp=no",
                "TemporaryFileSystem=/work:rw,nosuid,nodev,noexec,size=16M,mode=1777 "
                "/tmp:rw,nosuid,nodev,noexec,size=4M,mode=1777"}
    assert {"--property=" + value for value in required} <= set(args)
    assert args[args.index("--") + 1:] == [
        "/usr/bin/env", "-i", "PATH=/usr/bin", "LC_ALL=C", "/usr/bin/python3.13",
        "-I", "-S", "-c", b.PROBE, "mnt:[1]", "net:[2]", "1", str(hash(str(b.SNAPSHOT)))]
    assert options == dict(input="", capture_output=True, text=True, shell=False,
                           cwd="/", env=b.ENV, timeout=28)
    assert all("=/root/" not in arg and not arg.startswith("/root/")
               for arg in args if arg != b.PROBE)
    assert not host.fds


def test_reservation_collision_is_not_reused_or_retried(host, monkeypatch):
    first, calls = successful_run(host, monkeypatch)
    assert first["status"] == "ISOLATION_READY"
    again = b.probe_once(trusted_digest=host.digest(b.SNAPSHOT))
    assert again["reason"] == "RESERVATION_COLLISION" and len(calls) == 1


def test_reservation_count_bounded(host):
    host.dirs.update(str(b.JOBS / str(i)) for i in range(b.MAX_JOBS))
    assert check(host)["reason"] == "RESERVATION_LIMIT"


@pytest.mark.parametrize("stdout,returncode", [
    ("", 0), ("{}", 0), ("malformed private output", 1), ("x" * 1025, 0),
    (json.dumps({"protocol": 1, "uid": 0, "gid": 1, "bytes": 8}), 0),
    (json.dumps({"protocol": 1, "uid": 1, "gid": 0, "bytes": 8}), 0),
    (json.dumps({"protocol": True, "uid": 1, "gid": 1, "bytes": 8}), 0),
    (json.dumps({"protocol": 1, "uid": 1, "gid": 1, "bytes": 9}), 0),
])
def test_partial_failed_root_or_wrong_size_evidence_never_ready(host, monkeypatch, stdout, returncode):
    result, calls = successful_run(host, monkeypatch, SimpleNamespace(stdout=stdout, returncode=returncode))
    assert result["status"] == "ISOLATION_REFUSED" and len(calls) == 1
    assert "private output" not in json.dumps(result)
    assert host.created  # no cleanup, marker survives uncertain execution


@pytest.mark.parametrize("stop_timeout", [False, True])
def test_timeout_stops_only_unique_probe_never_retries_or_removes_reservation(host, monkeypatch, stop_timeout):
    calls = []
    def timeout(args, **kwargs):
        calls.append((args, kwargs))
        if len(calls) == 1 or stop_timeout:
            raise subprocess.TimeoutExpired(args, 28, output="PRIVATE_PARTIAL")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(b.subprocess, "run", timeout)
    result = b.probe_once(trusted_digest=host.digest(b.SNAPSHOT))
    assert result["reason"] == "DISPATCH_UNKNOWN" and len(calls) == 2
    assert calls[1][0] == ["/usr/bin/systemctl", "stop", "fpwf-probe-" + result["run_id"] + ".service"]
    assert "PRIVATE_PARTIAL" not in json.dumps(result) and len(host.created) == 1


def test_source_reattested_after_dispatch(host, monkeypatch):
    def mutate(*args, **kwargs):
        host.files[str(b.SNAPSHOT / "uv.lock")] = b"mutated during probe"
        return SimpleNamespace(returncode=0, stdout=json.dumps(
            {"protocol": 1, "uid": 61111, "gid": 61111, "bytes": 8}))
    monkeypatch.setattr(b.subprocess, "run", mutate)
    result = b.probe_once(trusted_digest=host.digest(b.SNAPSHOT))
    assert result["reason"] == "SOURCE_DIGEST_MISMATCH"


class Sandbox:
    """Execute the real probe against fake kernel observations, not host /proc."""
    def __init__(self):
        self.uid, self.gid, self.version = 61111, 61111, (3, 13)
        self.environment = dict(b.ENV)
        self.status = "CapEff:\t0\nCapPrm:\t0\nCapBnd:\t0\nNoNewPrivs:\t1\nSeccomp:\t2\n"
        self.mounts = ("1 0 1:1 / / ro - ext4 root ro\n"
                       "2 1 1:1 / /input ro - ext4 source ro\n"
                       "3 1 0:1 / /work rw,nosuid,nodev,noexec - tmpfs tmpfs rw\n")
        self.accessible, self.sockets, self.written, self.reads = set(), False, False, []
        self.output_uid, self.output_gid, self.output_size, self.blocks = 61111, 61111, 8, 4096
        self.host_namespace = False
        self.source_inode = 123

    def run(self):
        def forbidden_open(path, flags):
            self.reads.append(path)
            if path in self.accessible:
                return 99
            raise PermissionError(13, "denied")
        def text_open(path, mode="r"):
            if path == "/proc/self/status": return io.StringIO(self.status)
            if path == "/proc/self/mountinfo": return io.StringIO(self.mounts)
            if path == "/input/pyproject.toml": return io.BytesIO(b"metadata")
            assert path == "/work/probe" and mode == "xb"
            self.written = True
            return io.BytesIO()
        def sock(*args):
            if self.sockets: return SimpleNamespace(close=lambda: None)
            raise PermissionError(1, "network denied")
        fake_os = SimpleNamespace(
            getuid=lambda: self.uid, geteuid=lambda: self.uid, getgid=lambda: self.gid,
            getgroups=lambda: [self.gid], environ=self.environment, open=forbidden_open,
            O_RDONLY=0, O_NONBLOCK=1, close=lambda fd: None,
            readlink=lambda path: ("mnt:[1]" if path.endswith("mnt") else "net:[2]")
            if self.host_namespace else "isolated:[9]",
            statvfs=lambda path: SimpleNamespace(f_blocks=self.blocks, f_frsize=4096),
            stat=lambda path: SimpleNamespace(st_dev=1, st_ino=self.source_inode, st_uid=0, st_gid=0),
            lstat=lambda path: SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_nlink=1,
                                               st_uid=self.output_uid, st_gid=self.output_gid,
                                               st_size=self.output_size),
            listdir=lambda path: ["probe"])
        modules = {"os": fake_os, "sys": SimpleNamespace(version_info=self.version,
                   argv=["-c", "mnt:[1]", "net:[2]", "1", "123"]),
                   "socket": SimpleNamespace(AF_INET=2, AF_INET6=10, SOCK_STREAM=1, socket=sock)}
        def import_module(name, *args, **kwargs):
            return modules[name] if name in modules else builtins.__import__(name, *args, **kwargs)
        output = []
        ns = {"__builtins__": {**vars(builtins), "__import__": import_module},
              "open": text_open, "print": output.append}
        exec(compile(b.PROBE, "<fixed probe>", "exec"), ns)
        return json.loads(output[0])


def test_actual_probe_checks_kernel_evidence_and_output_ownership():
    sandbox = Sandbox()
    assert sandbox.run() == {"protocol": 1, "uid": 61111, "gid": 61111, "bytes": 8}
    assert sandbox.written
    assert "/proc/1/root" in sandbox.reads and "/run/credentials" in sandbox.reads


@pytest.mark.parametrize("attribute,value", [
    ("uid", 0), ("gid", 0), ("version", (3, 14)), ("sockets", True),
    ("host_namespace", True), ("output_uid", 0), ("output_gid", 0), ("output_size", 9), ("blocks", 8192),
    ("source_inode", 999),
    ("environment", {**b.ENV, "SYNTHETIC_PRODUCTION_TOKEN": "never inherit"}),
    ("accessible", {"/proc/1/root"}), ("accessible", {"/root"}),
    ("accessible", {"/run/credentials"}),
    ("status", "CapEff:1\nCapPrm:0\nCapBnd:0\nNoNewPrivs:1\nSeccomp:2\n"),
    ("status", "CapEff:0\nCapPrm:0\nCapBnd:0\nNoNewPrivs:0\nSeccomp:2\n"),
    ("status", "CapEff:0\nCapPrm:0\nCapBnd:0\nNoNewPrivs:1\nSeccomp:0\n"),
])
def test_actual_probe_refuses_root_network_credentials_and_bad_artifact(attribute, value):
    sandbox = Sandbox()
    setattr(sandbox, attribute, value)
    with pytest.raises(ValueError): sandbox.run()


@pytest.mark.parametrize("old,new", [("/input ro", "/input rw"), ("/ ro", "/ rw"),
                                      ("rw,nosuid,nodev,noexec", "rw,nosuid,nodev"),
                                      ("tmpfs tmpfs", "ext4 disk")])
def test_actual_probe_refuses_unexpected_effective_mounts(old, new):
    sandbox = Sandbox()
    sandbox.mounts = sandbox.mounts.replace(old, new)
    with pytest.raises(ValueError): sandbox.run()


LOCK = '''version = 1
revision = 3
requires-python = ">=3.13"
[[package]]
name = "funpayflow"
version = "1.0.0"
source = { editable = "." }
dependencies = [{name = "dependency"}]
[[package]]
name = "dependency"
version = "1.2.3"
source = {registry = "https://pypi.org/simple"}
'''


def test_sdist_only_dependency_is_explicit_blocker_and_no_resolver_fallback():
    assert b.binary_lock_blocker(LOCK.encode()) == "BINARY_WHEELS_MISSING"
    assert b.binary_lock_blocker((LOCK + 'wheels = [{url = "synthetic.whl"}]\n').encode()) == "LOCK_RESOLVER_REQUIRED"
    # An all-wheel claim never attests usable wheels, metadata, hashes or an env.
    assert b.binary_lock_blocker(b"invalid") == "LOCK_UNSUPPORTED"


@pytest.mark.parametrize("old,new", [
    ('name = "dependency"}', 'name = "dependency", marker = "unknown"}'),
    ('registry = "https://pypi.org/simple"', 'git = "https://example.invalid"'),
    ('revision = 3', 'revision = 99'), ('requires-python = ">=3.13"', 'requires-python = ">=3.12"'),
])
def test_unknown_lock_resolution_always_refused(old, new):
    assert b.binary_lock_blocker(LOCK.replace(old, new).encode()) == "LOCK_UNSUPPORTED"


def test_checkout_runtime_sdist_blocker_without_network_or_imports():
    raw = (SCRIPT.parents[1] / "uv.lock").read_bytes()
    assert b.binary_lock_blocker(raw) == "BINARY_WHEELS_MISSING"
