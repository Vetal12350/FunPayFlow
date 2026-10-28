"""Explicit, single-shot read-only observation; no import-time I/O or deploy API."""
from __future__ import annotations

import base64
import importlib.util
import os
from pathlib import Path
import re
import subprocess

_spec = importlib.util.spec_from_file_location(
    "vps_status_config", Path(__file__).with_name("vps_status_controller.py"))
config_api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(config_api)
_prepare_spec = importlib.util.spec_from_file_location(
    "vps_update_prepare", Path(__file__).with_name("vps_update_prepare.py"))
prepare = importlib.util.module_from_spec(_prepare_spec)
_prepare_spec.loader.exec_module(prepare)
WINDOWS = os.name == "nt"
TARGET = prepare.TARGET
REPO = prepare.REPO
FIELDS = {
    "active": {"YES", "NO", "UNKNOWN"},
    "current": {"b567b561", TARGET[:8], "UNKNOWN"},
    "target": {TARGET[:8], "UNKNOWN"},
    "current_main": {"TARGET", "DIFFERENT", "UNKNOWN"},
    "source_staged": {"YES", "NO", "UNKNOWN"},
    "source_integrity": {"VERIFIED", "MISMATCH", "UNKNOWN"},
    "deps_not_installed": {"YES", "UNKNOWN"},
    "database_ok": {"YES", "NO", "UNKNOWN"},
    "backup_dir_present": {"YES", "NO", "UNKNOWN"},
    "disk": {"SUFFICIENT", "LOW", "UNKNOWN"},
}

# Fixed stdlib collector only: never import or execute staged/application code.
REMOTE_SOURCE = prepare.SOURCE_COLLECTOR + r'''
import json, os, sqlite3, stat, subprocess, time
from contextlib import closing
from pathlib import Path

def run(args):
    p = subprocess.run(args, capture_output=True, text=True, shell=False, timeout=5)
    if p.returncode:
        raise ValueError()
    return p.stdout.strip()

def collect():
    # Owner-verified production directory; never infer active stage from an old install path.
    code = Path('/root/FunPayFlow-stage-b567b561')
    stage = Path('/root/.FunPayFlow-source-0affd678')
    data = Path('/root/FunPayFlow-data')
    out = dict.fromkeys(('active', 'current', 'source_staged', 'deps_not_installed',
                         'database_ok', 'backup_dir_present', 'disk'), 'UNKNOWN')
    state = run(['systemctl', 'show', '--property=ActiveState', '--value', 'funpayflow.service'])
    legacy = run(['systemctl', 'show', '--property=ActiveState', '--value', 'funpay-bot.service'])
    pid = run(['systemctl', 'show', '--property=MainPID', '--value', 'funpayflow.service'])
    workdir = run(['systemctl', 'show', '--property=WorkingDirectory', '--value', 'funpayflow.service'])
    user = run(['systemctl', 'show', '--property=User', '--value', 'funpayflow.service'])
    fragment = run(['systemctl', 'show', '--property=FragmentPath', '--value', 'funpayflow.service'])
    dropins = run(['systemctl', 'show', '--property=DropInPaths', '--value', 'funpayflow.service'])
    unit_owned = user in ('', 'root') and fragment == '/etc/systemd/system/funpayflow.service' and not dropins
    for name in ('/', '/etc', '/etc/systemd', '/etc/systemd/system',
                 '/etc/systemd/system/funpayflow.service', '/root', str(code)):
        p = Path(name)
        st = p.lstat()
        directory = name != fragment
        if (p.is_symlink() or st.st_uid != 0 or st.st_mode & 0o022 or
            not (stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode))):
            unit_owned = False
    processes = []
    for entry in Path('/proc').iterdir():
        if entry.name.isdecimal():
            try:
                argv = (entry / 'cmdline').read_bytes().split(b'\0')
            except FileNotFoundError:
                continue
            if b'-m' in argv and b'funpayflow.main' in argv:
                processes.append(entry.name)
    if unit_owned and state == 'active' and legacy == 'inactive' and processes == [pid] and workdir == str(code):
        if (Path('/proc') / pid / 'cwd').resolve(strict=True) == code:
            out['active'] = 'YES'
    elif state in ('inactive', 'failed') or legacy == 'active' or len(processes) != 1:
        out['active'] = 'NO'
    sha = run(['git', '--no-optional-locks', '-C', str(code), 'rev-parse', '--verify', 'HEAD'])
    if len(sha) == 40 and all(c in '0123456789abcdef' for c in sha):
        if sha in {'b567b561e0509de5595fa804d2372aa4cc28bbf1',
                   '0affd6784f2f903fa69a38ceb783a30b0816594e'}:
            out['current'] = sha[:8]
    # Evidence of source files is deliberately not an integrity/deployability claim.
    out['source_staged'] = 'YES' if (stage.is_dir() and not stage.is_symlink() and all(
        (stage / p).is_file() and not (stage / p).is_symlink()
        for p in ('pyproject.toml', 'uv.lock', 'src/funpayflow/main.py'))) else 'NO'
    if out['source_staged'] == 'YES' and not any((stage / p).exists() or (stage / p).is_symlink()
            for p in ('.venv', 'venv', 'site-packages')):
        out['deps_not_installed'] = 'YES'
    out['backup_dir_present'] = 'YES' if Path('/root/backups').is_dir() else 'NO'
    disk = os.statvfs('/root')
    out['disk'] = 'SUFFICIENT' if disk.f_bavail * disk.f_frsize >= 512*1024*1024 else 'LOW'
    db = data / 'state.sqlite3'
    # immutable avoids SQLite creating SHM/journal files. Refuse WAL/journal databases:
    # checking their main file alone would give misleading integrity evidence.
    if not data.is_symlink() and not db.is_symlink() and db.is_file() and db.stat().st_size <= 256*1024*1024 and not any(
            Path(str(db) + suffix).exists() for suffix in ('-wal', '-journal')):
        deadline = time.monotonic() + 5
        with closing(sqlite3.connect(db.as_uri() + '?mode=ro&immutable=1', uri=True, timeout=1)) as con:
            con.execute('PRAGMA query_only=ON')
            con.set_progress_handler(lambda: time.monotonic() > deadline, 1000)
            out['database_ok'] = 'YES' if con.execute('PRAGMA quick_check').fetchall() == [('ok',)] else 'NO'
    out['source_digest'] = (stage_digest() if out['active'] == 'YES' and
                            out['current'] == 'b567b561' and
                            out['deps_not_installed'] == 'YES' else 'UNKNOWN')
    return out

if __name__ == '__main__':
    try:
        print(json.dumps(collect()))
    except Exception:
        print('{}')
'''


def unknown(status="UNKNOWN"):
    return {"status": status, **dict.fromkeys(FIELDS, "UNKNOWN")}


def _invoke(args, payload=None):
    result = subprocess.run(args, input=payload, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, shell=False, timeout=35,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode or len(result.stdout) > 262144:
        raise ValueError()
    return result.stdout


def check_once():
    """Only an explicit owner action calls this. Never retry or return diagnostics."""
    try:
        root = config_api.local_directory()
        config = config_api.validate_config(config_api.decode_json(
            config_api.read_bounded(root / "config.json", 4096), 4096, "local"))
        # Exactly one owner-verified Ed25519 key for exactly the configured IPv4.
        pin = root / "update_known_hosts"
        text = config_api.read_bounded(pin, 4096).decode("ascii").strip()
        match = re.fullmatch(re.escape(config['host']) + r" ssh-ed25519 ([A-Za-z0-9+/]+={0,2})", text)
        if not match:
            return unknown("REFUSED")
        key = base64.b64decode(match[1], validate=True)
        if len(key) != 51 or key[:19] != b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20':
            return unknown("REFUSED")
        if not WINDOWS:
            return unknown("REFUSED")
        def get(endpoint):
            return config_api.decode_json(_invoke([
                "gh.exe", "api", "--hostname", "github.com", "--method=GET", "--",
                f"repos/{REPO}/{endpoint}"]), 262144, "github")
        main = get("commits/main")
        head = main.get("sha") if isinstance(main, dict) else None
        if not isinstance(head, str) or not re.fullmatch('[0-9a-f]{40}', head):
            return unknown("REFUSED")
        # Historical target push CI is independent of the moving main observation.
        # The newest run/latest attempt for this exact SHA must still be successful.
        page = get(f"actions/workflows/ci.yml/runs?branch=main&event=push&head_sha={TARGET}&per_page=1")
        runs = page.get("workflow_runs") if isinstance(page, dict) else None
        if not isinstance(runs, list) or len(runs) != 1:
            return unknown("REFUSED")
        run = runs[0]
        expected = {"head_sha": TARGET, "head_branch": "main", "event": "push",
                    "path": ".github/workflows/ci.yml", "status": "completed", "conclusion": "success"}
        if (not isinstance(run, dict) or any(run.get(k) != v for k, v in expected.items())
                or any(not isinstance(run.get(k), dict) or run[k].get("full_name") != REPO
                       for k in ("repository", "head_repository"))):
            return unknown("REFUSED")
        expected_digest = prepare.trusted_source_digest()
        args = ["ssh.exe", "-F", "NUL", "-T", "-a", "-x"]
        for option in ("StrictHostKeyChecking=yes", "BatchMode=yes", "ForwardAgent=no",
                       "ClearAllForwardings=yes", "ProxyCommand=none", "ProxyJump=none",
                       "IdentitiesOnly=yes", "PasswordAuthentication=no", "KbdInteractiveAuthentication=no",
                       "ConnectionAttempts=1", "ConnectTimeout=8", "ServerAliveInterval=5",
                       "ServerAliveCountMax=2", "UpdateHostKeys=no", "HostKeyAlgorithms=ssh-ed25519",
                       "GlobalKnownHostsFile=NUL", f'UserKnownHostsFile="{pin.as_posix()}"'):
            args.extend(("-o", option))
        args.extend(("-i", config["identity"], "-l", "root", config["host"], "python3 -I -B -"))
        value = config_api.decode_json(_invoke(args, REMOTE_SOURCE.encode()), 4096, "schema")
        if not isinstance(value, dict) or set(value) != (set(FIELDS) - {
                "target", "current_main", "source_integrity"}) | {"source_digest"}:
            return unknown()
        digest = value.pop('source_digest')
        if not isinstance(digest, str) or not (digest == 'UNKNOWN' or re.fullmatch('[0-9a-f]{64}', digest)):
            return unknown()
        value["target"] = TARGET[:8]
        value["current_main"] = 'TARGET' if head == TARGET else 'DIFFERENT'
        value["source_integrity"] = ('UNKNOWN' if digest == 'UNKNOWN' else
                                     'VERIFIED' if digest == expected_digest else 'MISMATCH')
        if any(not isinstance(value[k], str) or value[k] not in allowed for k, allowed in FIELDS.items()):
            return unknown()
        healthy = all(value[k] == "YES" for k in (
            "active", "source_staged", "deps_not_installed", "database_ok", "backup_dir_present"))
        healthy = (healthy and value['current'] == 'b567b561' and value['disk'] == 'SUFFICIENT'
                   and value['source_integrity'] == 'VERIFIED')
        status = "REFUSED" if value['source_integrity'] == 'MISMATCH' else "OBSERVED" if healthy else "UNKNOWN"
        return {"status": status, **value}
    except Exception:
        return unknown()
