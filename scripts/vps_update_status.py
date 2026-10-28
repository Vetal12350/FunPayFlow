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
WINDOWS = os.name == "nt"
TARGET = "0affd6784f2f903fa69a38ceb783a30b0816594e"
REPO = "Vetal12350/FunPayFlow"
FIELDS = {
    "active": {"YES", "NO", "UNKNOWN"},
    "current": {"b567b561", TARGET[:8], "UNKNOWN"},
    "target": {TARGET[:8], "UNKNOWN"},
    "source_staged": {"YES", "NO", "UNKNOWN"},
    "deps_not_installed": {"YES", "UNKNOWN"},
    "database_ok": {"YES", "NO", "UNKNOWN"},
    "backup_dir_present": {"YES", "NO", "UNKNOWN"},
    "disk": {"SUFFICIENT", "LOW", "UNKNOWN"},
}

# Fixed stdlib collector only: never import or execute staged/application code.
REMOTE_SOURCE = r'''
import json, os, sqlite3, subprocess, time
from contextlib import closing
from pathlib import Path

def run(args):
    p = subprocess.run(args, capture_output=True, text=True, shell=False, timeout=5)
    if p.returncode:
        raise ValueError()
    return p.stdout.strip()

def collect():
    code = Path('/root/FunPayFlow-next')
    stage = Path('/root/.FunPayFlow-source-0affd678')
    data = Path('/root/FunPayFlow-data')
    out = dict.fromkeys(('active', 'current', 'source_staged', 'deps_not_installed',
                         'database_ok', 'backup_dir_present', 'disk'), 'UNKNOWN')
    state = run(['systemctl', 'show', '--property=ActiveState', '--value', 'funpayflow.service'])
    legacy = run(['systemctl', 'show', '--property=ActiveState', '--value', 'funpay-bot.service'])
    pid = run(['systemctl', 'show', '--property=MainPID', '--value', 'funpayflow.service'])
    processes = []
    for entry in Path('/proc').iterdir():
        if entry.name.isdecimal():
            try:
                argv = (entry / 'cmdline').read_bytes().split(b'\0')
            except FileNotFoundError:
                continue
            if b'-m' in argv and b'funpayflow.main' in argv:
                processes.append(entry.name)
    if state == 'active' and legacy == 'inactive' and processes == [pid]:
        if (Path('/proc') / pid / 'cwd').resolve(strict=True) == code:
            out['active'] = 'YES'
    elif state in ('inactive', 'failed') or legacy == 'active' or len(processes) != 1:
        out['active'] = 'NO'
    sha = run(['git', '--no-optional-locks', '-C', str(code), 'rev-parse', '--verify', 'HEAD'])
    if len(sha) == 40 and all(c in '0123456789abcdef' for c in sha):
        if sha.startswith('b567b561') or sha == '0affd6784f2f903fa69a38ceb783a30b0816594e':
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
        page = get(f"actions/workflows/ci.yml/runs?branch=main&event=push&head_sha={head}&per_page=1")
        runs = page.get("workflow_runs") if isinstance(page, dict) else None
        if not isinstance(runs, list) or len(runs) != 1:
            return unknown("REFUSED")
        run = runs[0]
        expected = {"head_sha": head, "head_branch": "main", "event": "push",
                    "path": ".github/workflows/ci.yml", "status": "completed", "conclusion": "success"}
        if (not isinstance(run, dict) or any(run.get(k) != v for k, v in expected.items())
                or any(not isinstance(run.get(k), dict) or run[k].get("full_name") != REPO
                       for k in ("repository", "head_repository"))):
            return unknown("REFUSED")
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
        if not isinstance(value, dict) or set(value) != set(FIELDS) - {"target"}:
            return unknown()
        # A newer main can be observed, but this reviewed source-only stage cannot
        # satisfy it. Never expose arbitrary commit text or silently stage a new SHA.
        value["target"] = TARGET[:8] if head == TARGET else "UNKNOWN"
        if any(not isinstance(value[k], str) or value[k] not in allowed for k, allowed in FIELDS.items()):
            return unknown()
        healthy = all(value[k] == "YES" for k in (
            "active", "source_staged", "deps_not_installed", "database_ok", "backup_dir_present"))
        healthy = healthy and value['current'] != 'UNKNOWN' and value['disk'] == 'SUFFICIENT'
        status = "REFUSED" if head != TARGET else "OBSERVED" if healthy else "UNKNOWN"
        return {"status": status, **value}
    except Exception:
        return unknown()
