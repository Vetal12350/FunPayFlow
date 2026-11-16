"""Offline: scripts/vps_bootstrap.sh in a scratch root with stub system tools.

systemctl/useradd/visudo/curl/sshd/hostname are fakes on PATH; FPF_PREFIX moves
every system path into tmp_path. Nothing touches the real system or network.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "vps_bootstrap.sh"
REF = "c" * 40

pytestmark = pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None or sys.version_info[:2] != (3, 13),
    reason="Linux bash + Python 3.13 required")

SYSTEMCTL = r'''#!/usr/bin/env bash
echo "systemctl $*" >> "$FAKE/calls.log"
case "$1" in
  show)
    prop="${3}"
    case "$prop" in
      LoadState) echo loaded ;;
      DropInPaths) ls "$FPF_PREFIX"/etc/systemd/system/funpayflow.service.d/*.conf 2>/dev/null | tr '\n' ' ' ;;
      FragmentPath) echo /etc/systemd/system/funpayflow.service ;;
      ExecStart)
        py="${FAKE_EXEC:-/root/stage/.venv/bin/python}"
        if [ -z "${FAKE_IGNORE_DROPIN:-}" ] && \
           [ -f "$FPF_PREFIX/etc/systemd/system/funpayflow.service.d/99-funpayflow-autoupdate.conf" ]; then
          py=/opt/funpayflow/current/venv/bin/python
        fi
        echo "{ path=$py ; argv[]=$py -m funpayflow.main ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }" ;;
      Environment) echo 'FUNPAY_BOT_DATA_DIR=/root/FunPayFlow-data' ;;
      WorkingDirectory) echo /root/stage ;;
    esac ;;
  is-active) echo "${FAKE_LEGACY:-inactive}"; [ "${FAKE_LEGACY:-}" = active ] ;;
  daemon-reload) exit 0 ;;
  *) echo "FORBIDDEN systemctl $*" >> "$FAKE/calls.log"; exit 99 ;;
esac
'''


def stub(directory, name, body):
    path = directory / name
    path.write_text(body)
    path.chmod(0o755)


@pytest.fixture
def box(tmp_path):
    p = tmp_path / "root"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub(bin_dir, "systemctl", SYSTEMCTL)
    stub(bin_dir, "useradd", '#!/usr/bin/env bash\necho "useradd $*" >> "$FAKE/calls.log"\n')
    stub(bin_dir, "visudo", '#!/usr/bin/env bash\ngrep -q funpayflow-deploy-agent "$2"\n')
    stub(bin_dir, "sshd", '#!/usr/bin/env bash\nexit 1\n')
    stub(bin_dir, "ssh-keygen", '#!/usr/bin/env bash\n'
         'while [ $# -gt 0 ]; do [ "$1" = -f ] && f="$2"; shift; done\n'
         'printf -- "-----BEGIN OPENSSH PRIVATE KEY-----\\nFAKE\\n-----END OPENSSH PRIVATE KEY-----\\n" > "$f"\n'
         'echo "ssh-ed25519 AAAAFAKEKEY funpayflow-github-actions" > "$f.pub"\n'
         'echo "ssh-keygen" >> "$FAKE/calls.log"\n')
    stub(bin_dir, "hostname", '#!/usr/bin/env bash\necho "203.0.113.5 10.0.0.2"\n')
    stub(bin_dir, "curl", '#!/usr/bin/env bash\n'
         'for a in "$@"; do case "$a" in */' + REF + '/scripts/deploy_agent.py) ok=1;; esac; done\n'
         '[ "${ok:-}" = 1 ] || exit 22\n'
         'while [ $# -gt 0 ]; do [ "$1" = -o ] && cp "$AGENT_SRC" "$2"; shift; done\n')
    unit = p / "etc/systemd/system/funpayflow.service"
    unit.parent.mkdir(parents=True)
    unit.write_text((ROOT / "linux/systemd/funpayflow.service.in").read_text()
        .replace("@USER@", "root").replace("@CODE_DIR@", "/root/stage")
        .replace("@ENV_FILE@", "/root/FunPayFlow-data/.env")
        .replace("@DATA_VALUE@", "/root/FunPayFlow-data")
        .replace("@PYTHON@", "/root/stage/.venv/bin/python"))
    data = p / "root/FunPayFlow-data"
    data.mkdir(parents=True)
    (data / "state.sqlite3").write_bytes(b"")
    (data / ".env").write_text("SECRET-CANARY=1\n")
    stage_venv = p / "root/stage/.venv"
    venv.create(stage_venv, with_pip=False, symlinks=True)
    site = next(stage_venv.glob("lib/python3.13/site-packages"))
    (site / "funpayflow").mkdir()
    (site / "funpayflow/__init__.py").write_text("")
    hostkey = p / "etc/ssh/ssh_host_ed25519_key.pub"
    hostkey.parent.mkdir(parents=True)
    hostkey.write_text("ssh-ed25519 AAAAHOSTKEY root@vps\n")
    env = dict(os.environ, FPF_PREFIX=str(p), FAKE=str(tmp_path),
               AGENT_SRC=str(ROOT / "scripts/deploy_agent.py"),
               PATH=f"{bin_dir}:{os.environ['PATH']}")
    return type("Box", (), dict(p=p, unit=unit, env=env, tmp=tmp_path))


def run(box, *extra, **env):
    return subprocess.run(["bash", str(SCRIPT), "--ref", REF, "--yes", *extra],
                          env=dict(box.env, **env), capture_output=True, text=True, timeout=120)


def calls(box):
    log = box.tmp / "calls.log"
    return log.read_text() if log.exists() else ""


def test_full_setup_without_touching_the_running_bot(box):
    original_unit = box.unit.read_text()
    result = run(box)
    assert result.returncode == 0, result.stderr
    p = box.p
    # Agent from the same commit, shebang pinned to the bot's Python 3.13.
    agent = p / "usr/local/sbin/funpayflow-deploy-agent"
    first, rest = agent.read_text().split("\n", 1)
    assert first.startswith("#!") and first.endswith(" -I") and "python3" in first
    assert rest == (ROOT / "scripts/deploy_agent.py").read_text().split("\n", 1)[1]
    config = json.loads((p / "etc/funpayflow-deploy/config.json").read_text())
    assert config["data_dir"] == "/root/FunPayFlow-data"
    assert config["current"] == "/opt/funpayflow/current"
    assert config["forbidden_services"] == ["funpay-bot.service"]
    sudoers = (p / "etc/sudoers.d/funpayflow-deploy").read_text()
    assert 'NOPASSWD: /usr/local/sbin/funpayflow-deploy-agent ""' in sudoers
    assert "ALL=(root) ALL" not in sudoers
    # Current code registered as the first release (rollback target).
    current = p / "opt/funpayflow/current"
    assert current.is_symlink() and Path(os.readlink(current)).name == "0" * 40
    assert (current / "venv/bin/python").exists()
    # Unit file untouched; our last drop-in overrides ExecStart; daemon-reload only.
    assert box.unit.read_text() == original_unit
    dropin = Path(str(box.unit) + ".d") / "99-funpayflow-autoupdate.conf"
    assert dropin.read_text() == ("[Service]\nExecStart=\n"
                                  "ExecStart=/opt/funpayflow/current/venv/bin/python -m funpayflow.main\n")
    assert "sudo rm -f /etc/systemd/system/funpayflow.service.d/99-funpayflow-autoupdate.conf" in result.stdout
    log = calls(box)
    assert "systemctl daemon-reload" in log and "FORBIDDEN" not in log
    assert "useradd --create-home --shell /bin/bash funpayflow-deploy" in log
    keys = (p / "home/funpayflow-deploy/.ssh/authorized_keys").read_text()
    assert keys.startswith('restrict,command="sudo -n /usr/local/sbin/funpayflow-deploy-agent" ssh-ed25519 ')
    out = result.stdout
    assert "DEPLOY_HOST:  203.0.113.5" in out and "DEPLOY_USER:  funpayflow-deploy" in out
    assert "203.0.113.5 ssh-ed25519 AAAAHOSTKEY" in out
    assert "BEGIN OPENSSH PRIVATE KEY" in out
    assert "SECRET-CANARY" not in out + result.stderr


def test_second_run_is_idempotent(box):
    assert run(box).returncode == 0
    keys = (box.p / "home/funpayflow-deploy/.ssh/authorized_keys").read_text()
    dropin = Path(str(box.unit) + ".d") / "99-funpayflow-autoupdate.conf"
    first = dropin.read_text()
    again = run(box)
    assert again.returncode == 0, again.stderr
    assert (box.p / "home/funpayflow-deploy/.ssh/authorized_keys").read_text() == keys
    assert dropin.read_text() == first and "уже переведён" in again.stdout
    assert calls(box).count("ssh-keygen") == 1  # key is not regenerated
    assert "FORBIDDEN" not in calls(box)


@pytest.mark.parametrize("change,message", [
    ({"FAKE_LEGACY": "active"}, "funpay-bot.service активна"),
    ({"FAKE_EXEC": "/root/stage/.venv/bin/python3 -X"}, "ExecStart"),
])
def test_unsafe_server_state_changes_nothing(box, change, message):
    original = box.unit.read_text()
    result = run(box, **change)
    assert result.returncode != 0 and message in result.stderr
    assert box.unit.read_text() == original
    assert not (box.p / "usr/local/sbin/funpayflow-deploy-agent").exists()
    assert "FORBIDDEN" not in calls(box)


def test_bad_ref_is_rejected(box):
    result = subprocess.run(["bash", str(SCRIPT), "--ref", "main", "--yes"], env=box.env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode != 0 and "--ref" in result.stderr


def test_existing_owner_drop_ins_are_kept_and_effective_path_is_used(box):
    """Like the real server: older drop-ins move the bot to another stage."""
    stage = box.p / "root/stage-2/.venv"
    import shutil as sh
    sh.copytree(box.p / "root/stage/.venv", stage, symlinks=True)
    old = Path(str(box.unit) + ".d") / "97-owner-update-abc.conf"
    old.parent.mkdir()
    old.write_text("[Service]\nExecStart=\nExecStart=/root/stage-2/.venv/bin/python -m funpayflow.main\n")
    original = box.unit.read_text()
    result = run(box, FAKE_EXEC="/root/stage-2/.venv/bin/python")
    assert result.returncode == 0, result.stderr
    assert old.read_text().startswith("[Service]") and box.unit.read_text() == original
    current = box.p / "opt/funpayflow/current"
    assert Path(os.readlink(current / "venv")) == stage


def test_drop_in_not_applied_is_removed_and_stops(box):
    result = run(box, FAKE_IGNORE_DROPIN="1")
    assert result.returncode != 0 and "не применил" in result.stderr
    assert not (Path(str(box.unit) + ".d") / "99-funpayflow-autoupdate.conf").exists()
    assert "FORBIDDEN" not in calls(box)


def test_script_never_restarts_or_reads_env():
    text = SCRIPT.read_text()
    for forbidden in ("systemctl restart", "systemctl stop", "systemctl start",
                      "| bash", "| sh", "cat \"$data_dir/.env\"", "source "):
        assert forbidden not in text
