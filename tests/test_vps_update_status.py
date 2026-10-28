"""Synthetic offline checks only; no real configuration, SSH or network."""
import base64
import importlib.util
import json
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'vps_update_status.py'
spec = importlib.util.spec_from_file_location('vps_tests', SCRIPT)
v = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v)
CONFIG = {'host': '8.8.8.8', 'identity': r'C:\Synthetic User\id_ed25519'}
PIN = '8.8.8.8 ssh-ed25519 ' + base64.b64encode(
    b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20' + b'a' * 32).decode()


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(v.subprocess, 'run', lambda *a, **k: pytest.fail('Unexpected process'))
    monkeypatch.setattr(v.subprocess, 'Popen', lambda *a, **k: pytest.fail('Unexpected launch'))
    monkeypatch.setattr(v.prepare, '_fetch_archive', lambda: pytest.fail('Unexpected network'))


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setattr(v, 'WINDOWS', True)
    monkeypatch.setattr(v.config_api, 'local_directory', lambda: tmp_path)
    (tmp_path / 'config.json').write_text(json.dumps(CONFIG), encoding='utf8')
    (tmp_path / 'update_known_hosts').write_text(PIN, encoding='ascii')
    run = dict(head_sha=v.TARGET, head_branch='main', event='push',
               path='.github/workflows/ci.yml', status='completed', conclusion='success',
               repository={'full_name': v.REPO}, head_repository={'full_name': v.REPO})
    report = {k: 'YES' for k in v.FIELDS if k not in {'target', 'current_main', 'source_integrity'}}
    report.update(current='b567b561', disk='SUFFICIENT', source_digest='a'*64)
    monkeypatch.setattr(v.prepare, 'trusted_source_digest', lambda: 'a'*64)
    replies = [{'sha': v.TARGET}, {'workflow_runs': [run]}, report]
    calls = []
    def invoke(args, **kwargs):
        calls.append((args, kwargs))
        assert kwargs['shell'] is False and kwargs['timeout'] == 35
        assert kwargs['stderr'] == subprocess.DEVNULL
        return SimpleNamespace(returncode=0, stdout=json.dumps(replies[len(calls)-1]).encode())
    monkeypatch.setattr(v.subprocess, 'run', invoke)
    return tmp_path, replies, calls


def test_pinned_read_only_check(setup):
    root, replies, calls = setup
    result = v.check_once()
    assert result['status'] == 'OBSERVED' and result['target'] == v.TARGET[:8]
    assert result['deps_not_installed'] == 'YES'
    assert result['source_integrity'] == 'VERIFIED'
    assert len(calls) == 3
    assert all('--method=GET' in args and '--hostname' in args for args, _ in calls[:2])
    args, kwargs = calls[-1]
    assert args[-1] == 'python3 -I -B -'
    assert args[:6] == ['ssh.exe', '-F', 'NUL', '-T', '-a', '-x']
    for option in ('StrictHostKeyChecking=yes', 'BatchMode=yes', 'ProxyCommand=none',
                   'ProxyJump=none', 'ClearAllForwardings=yes', 'UpdateHostKeys=no',
                   'GlobalKnownHostsFile=NUL', f'UserKnownHostsFile="{(root / "update_known_hosts").as_posix()}"'):
        assert option in args
    assert kwargs['input'] == v.REMOTE_SOURCE.encode()
    assert set(result) == {'status', *v.FIELDS}
    assert '8.8.8.8' not in str(result) and 'Synthetic' not in str(result)


@pytest.mark.parametrize('field,value', [
    ('event', 'pull_request'), ('head_sha', 'b'*40), ('head_branch', 'other'),
    ('path', '.github/workflows/evil.yml'), ('conclusion', 'skipped'),
    ('status', 'in_progress'), ('repository', {'full_name': 'Other/Fork'}),
    ('head_repository', None),
])
def test_unvetted_ci_never_ssh(setup, field, value):
    _, replies, calls = setup
    replies[1]['workflow_runs'][0][field] = value
    assert v.check_once() == v.unknown('REFUSED')
    assert len(calls) == 2


def test_new_main_does_not_replace_pinned_target_or_its_historical_ci(setup):
    _, replies, calls = setup
    replies[0]['sha'] = 'b'*40
    result = v.check_once()
    assert result['status'] == 'OBSERVED' and result['target'] == v.TARGET[:8]
    assert result['current_main'] == 'DIFFERENT'
    assert f'head_sha={v.TARGET}' in calls[1][0][-1]
    assert result['source_staged'] == 'YES'
    assert len(calls) == 3


def test_new_main_ci_cannot_vet_the_old_target(setup):
    _, replies, calls = setup
    replies[0]['sha'] = 'b'*40
    replies[1]['workflow_runs'][0]['head_sha'] = 'b'*40
    assert v.check_once() == v.unknown('REFUSED')
    assert len(calls) == 2


@pytest.mark.parametrize('digest,status', [('b'*64, 'REFUSED'), ('UNKNOWN', 'UNKNOWN'),
                                         ('SYNTHETIC_SECRET', 'UNKNOWN'), ([], 'UNKNOWN')])
def test_source_integrity_never_inferred_from_presence(setup, digest, status):
    _, replies, _ = setup
    replies[2]['source_digest'] = digest
    result = v.check_once()
    assert result['status'] == status
    assert result['source_integrity'] != 'VERIFIED'
    assert 'SYNTHETIC_SECRET' not in str(result)


def test_corrupt_independent_archive_never_ssh(setup, monkeypatch):
    _, _, calls = setup
    def corrupt():
        return v.prepare.archive_digest(b'corrupt gzip')
    monkeypatch.setattr(v.prepare, 'trusted_source_digest', corrupt)
    assert v.check_once() == v.unknown()
    assert len(calls) == 2


@pytest.mark.parametrize('sha', [None, [], 'evil;command', 'a'*41])
def test_malformed_main_is_refused_before_ssh(setup, sha):
    _, replies, calls = setup
    replies[0]['sha'] = sha
    assert v.check_once() == v.unknown('REFUSED')
    assert len(calls) == 1


@pytest.mark.parametrize('config,pin', [
    ({**CONFIG, 'host': '-oProxyCommand=evil'}, PIN),
    ({**CONFIG, 'identity': r'C:\x\..\evil'}, PIN),
    ({**CONFIG, 'command': 'evil'}, PIN),
    (CONFIG, PIN.replace('8.8.8.8', '*')), (CONFIG, PIN + '\n' + PIN),
    (CONFIG, PIN.replace('ssh-ed25519', 'ssh-rsa')), (CONFIG, '8.8.8.8 ssh-ed25519 YQ=='),
])
def test_bad_config_or_pin_never_starts_process(setup, config, pin):
    root, _, calls = setup
    (root / 'config.json').write_text(json.dumps(config), encoding='utf8')
    (root / 'update_known_hosts').write_text(pin, encoding='ascii')
    assert v.check_once()['status'] in {'UNKNOWN', 'REFUSED'}
    assert not calls


@pytest.mark.parametrize('value', [{}, [], {'private': 'SYNTHETIC_SECRET'},
                                  dict.fromkeys(set(v.FIELDS) - {'target'}, [] )])
def test_malformed_collector_never_leaks_or_goes_green(setup, value):
    _, replies, _ = setup
    replies[2] = value
    assert v.check_once() == v.unknown()


@pytest.mark.parametrize('field,value', [('active', 'NO'), ('current', 'UNKNOWN'),
    ('source_staged', 'NO'), ('deps_not_installed', 'UNKNOWN'), ('database_ok', 'NO'),
    ('backup_dir_present', 'NO'), ('disk', 'LOW')])
def test_incomplete_evidence_is_unknown(setup, field, value):
    _, replies, _ = setup
    replies[2][field] = value
    assert v.check_once()['status'] == 'UNKNOWN'


@pytest.mark.parametrize('failure', ['timeout', 'exit', 'malformed', 'oversized'])
@pytest.mark.parametrize('step', [1, 2, 3])
def test_bounded_failure_no_retry_or_diagnostics(setup, monkeypatch, failure, step):
    _, _, calls = setup
    original = v.subprocess.run
    def run(args, **kwargs):
        if len(calls) + 1 != step:
            return original(args, **kwargs)
        calls.append((args, kwargs))
        if failure == 'timeout':
            raise subprocess.TimeoutExpired('SYNTHETIC_SECRET', 35)
        return SimpleNamespace(returncode=1 if failure == 'exit' else 0,
            stdout=b'SYNTHETIC_SECRET' if failure != 'oversized' else b'x'*262145)
    monkeypatch.setattr(v.subprocess, 'run', run)
    assert v.check_once() == v.unknown()
    assert len(calls) == step


def test_import_is_passive(monkeypatch):
    monkeypatch.setattr(Path, 'open', lambda *a, **k: pytest.fail('Private file read'))
    spec.loader.exec_module(importlib.util.module_from_spec(spec))


def collector_namespace():
    namespace = {'__name__': 'offline_collector'}
    exec(compile(v.REMOTE_SOURCE, '<fixed collector>', 'exec'), namespace)
    return namespace


def test_remote_collector_with_fake_systemd_proc_and_sqlite(monkeypatch):
    ns = collector_namespace()
    ns['stage_digest'] = lambda: 'a'*64
    paths = []
    class FakePath:
        def __init__(self, value):
            self.value = str(value)
            self.name = self.value.rsplit('/', 1)[-1]
        def __str__(self): return self.value
        def __truediv__(self, value): return FakePath(self.value + '/' + value)
        def __eq__(self, other): return str(self) == str(other)
        def is_dir(self): return True
        def is_file(self): return True
        def is_symlink(self): return False
        def exists(self): return False
        def stat(self): return SimpleNamespace(st_size=128)
        def lstat(self):
            return SimpleNamespace(st_uid=0, st_mode=0o100644 if self.value.endswith('.service') else 0o40755)
        def resolve(self, **kwargs): return FakePath('/root/FunPayFlow-stage-b567b561')
        def iterdir(self): return [FakePath('/proc/42')]
        def read_bytes(self):
            paths.append(self.value)
            return b'python\0-m\0funpayflow.main\0'
        def as_uri(self): return 'file:///synthetic.sqlite3'
    ns['Path'] = FakePath
    commands = []
    workdir = ['/root/FunPayFlow-stage-b567b561']
    overrides = {}
    def run(args, **kwargs):
        commands.append(args)
        assert kwargs['shell'] is False and kwargs['timeout'] == 5
        if args[0] == 'git':
            return SimpleNamespace(returncode=0, stdout='b567b561e0509de5595fa804d2372aa4cc28bbf1')
        value = ('root' if 'User' in args[2] else '/etc/systemd/system/funpayflow.service' if 'FragmentPath' in args[2]
                 else '' if 'DropInPaths' in args[2] else '42' if 'MainPID' in args[2] else workdir[0] if 'WorkingDirectory' in args[2]
                 else 'inactive' if args[-1] == 'funpay-bot.service' else 'active')
        value = overrides.get(args[2], value)
        return SimpleNamespace(returncode=0, stdout=value)
    monkeypatch.setattr(subprocess, 'run', run)
    monkeypatch.setattr(ns['os'], 'statvfs', lambda p: SimpleNamespace(f_bavail=1024**3, f_frsize=1), raising=False)
    sql = []
    class Connection:
        def execute(self, command): sql.append(command); return self
        def fetchall(self): return [('ok',)]
        def close(self): pass
        def set_progress_handler(self, callback, steps): assert steps == 1000
    def connect(database, **kwargs):
        assert database.endswith('?mode=ro&immutable=1') and kwargs['uri'] is True
        return Connection()
    monkeypatch.setattr(ns['sqlite3'], 'connect', connect)
    result = ns['collect']()
    assert result['active'] == result['database_ok'] == result['source_staged'] == 'YES'
    assert sql == ['PRAGMA query_only=ON', 'PRAGMA quick_check']
    assert paths == ['/proc/42/cmdline']
    assert result['source_digest'] == 'a'*64
    assert len(commands) == 8
    for field, value in [('--property=User', 'other'), ('--property=FragmentPath', '/tmp/unit'),
                         ('--property=DropInPaths', '/tmp/override'), ('--property=MainPID', '43')]:
        overrides[field] = value
        refused = ns['collect']()
        assert refused['active'] != 'YES' and refused['source_digest'] == 'UNKNOWN'
        overrides.clear()
    before_sql = len(sql)
    workdir[0] = '/root/FunPayFlow-next'
    assert ns['collect']()['active'] != 'YES'
    assert len(sql) == before_sql + 2
    workdir[0] = '/root/FunPayFlow-stage-b567b561'
    # No deployment, mkdir, application launch, shell, backup, or install call exists.
    assert all(args[0] in {'systemctl', 'git'} for args in commands)
    assert all(args[1] == 'show' for args in commands if args[0] == 'systemctl')
    FakePath.exists = lambda self: self.value.endswith('-wal')
    before = list(sql)
    assert ns['collect']()['database_ok'] == 'UNKNOWN'
    assert sql == before  # WAL must prevent SQLite quick_check entirely
    FakePath.exists = lambda self: self.value.endswith('.venv')
    FakePath.is_file = lambda self: not self.value.endswith('src/funpayflow/main.py')
    assert ns['collect']()['source_staged'] == 'NO'
    FakePath.is_file = lambda self: True
    assert ns['collect']()['deps_not_installed'] == 'UNKNOWN'
    FakePath.iterdir = lambda self: [FakePath('/proc/42'), FakePath('/proc/43')]
    assert ns['collect']()['active'] == 'NO'


def test_ui_single_flight_main_thread_and_close(monkeypatch):
    spec_cc = importlib.util.spec_from_file_location('cc_vps_tests', SCRIPT.with_name('control_center.py'))
    cc = importlib.util.module_from_spec(spec_cc)
    spec_cc.loader.exec_module(cc)
    work, scheduled, updates = [], [], []
    real_thread = threading.Thread
    monkeypatch.setattr(cc.threading, 'Thread', lambda **kw: SimpleNamespace(start=lambda: work.append(kw['target'])))
    monkeypatch.setattr(cc.vps, 'check_once', lambda: cc.vps.unknown())
    def configure(**kwargs):
        assert threading.current_thread() is threading.main_thread()
        updates.append(kwargs)
    app = SimpleNamespace(after=lambda delay, callback: scheduled.append(callback))
    widget = SimpleNamespace(configure=configure)
    check = cc.VpsCheck(app, widget, widget)
    assert not work and not scheduled  # Merely building the panel does no I/O.
    check.start(); check.start()
    assert len(work) == 1
    count = len(updates)
    thread = real_thread(target=work.pop())
    thread.start(); thread.join(timeout=2)
    assert not thread.is_alive() and len(updates) == count
    scheduled.pop()()
    assert not check.busy and updates[-1]['text'].startswith('VPS: UNKNOWN')
    check.start()
    count = len(updates)
    check.closed.set()
    work.pop()()
    scheduled.pop()()
    assert len(updates) == count
