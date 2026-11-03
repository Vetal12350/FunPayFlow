"""Offline contracts only; no host services, processes, files or network probes.

Run with --basetemp=tests/.offline-tmp/maintenance and -p no:cacheprovider.
Recovery oracle tests do not certify an installed recovery supervisor.
"""
import ast
from dataclasses import asdict, FrozenInstanceError, replace
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'vps_maintenance_coordinator.py'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


coordinator = load('maintenance_contract', SCRIPT)
backup = load('maintenance_backup_contract', ROOT / 'scripts' / 'vps_backup_create.py')
NOW = 10**12
SESSION = coordinator.Session(
    helper_pid=101, helper_start=300, coordinator_pid=100, coordinator_start=200,
    helper_sha256='a' * 64, coordinator_sha256='b' * 64,
    boot_id='11111111-1111-1111-1111-111111111111', expires_boottime_ns=NOW + 120 * 10**9)
INACTIVE = (b'LoadState=loaded\nActiveState=inactive\nSubState=dead\n'
            b'MainPID=0\nControlPID=0\nJob=\n')
ACTIVE = INACTIVE.replace(b'inactive', b'active').replace(b'dead', b'running').replace(
    b'MainPID=0', b'MainPID=123')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode('utf-8')


def authorization(session=SESSION):
    # Synthetic bytes only, never written as an approval or to a host path.
    return canonical(dict(asdict(session), version=1, units=list(coordinator.UNITS),
                          data=coordinator.DATA, backups=coordinator.BACKUPS,
                          services_inhibited=True, writers_quiescent=True, recovery_armed=True))


def test_candidate_receipt_checks_digest_but_does_not_authorize():
    script = b'synthetic reviewed bytes'
    digest = hashlib.sha256(script).hexdigest()
    raw = canonical(dict(reviewed_commit='c' * 40, script_sha256=digest))
    assert coordinator.inspect_receipt(raw, script) == ('c' * 40, digest)
    with pytest.raises(coordinator.ContractRefused, match='^contract_invalid$'):
        coordinator.inspect_receipt(raw, script + b'changed')


@pytest.mark.parametrize('raw', [
    None, b'', b'[]', b'null', b'\xff', b'x' * 1025,
    b'{"reviewed_commit":"secret","script_sha256":"private"}',
    canonical(dict(reviewed_commit='c' * 40, script_sha256='A' * 64)),
    canonical(dict(reviewed_commit='c' * 40, script_sha256=True)),
    canonical(dict(reviewed_commit='c' * 40, script_sha256='a' * 64, extra=True)),
    b'{"reviewed_commit":0,"reviewed_commit":1,"script_sha256":2}',
])
def test_invalid_receipt_is_sanitized(raw):
    with pytest.raises(coordinator.ContractRefused) as refused:
        coordinator.inspect_receipt(raw, b'synthetic')
    assert str(refused.value) == 'contract_invalid'


def test_original_active_state_and_legacy_inactive_are_distinct():
    active = coordinator.inspect_unit(ACTIVE)
    old = coordinator.inspect_unit(INACTIVE)
    assert active.originally_active and active.main_pid == 123
    assert not active.helper_compatible
    assert not old.originally_active and old.main_pid == 0 and old.helper_compatible
    # Exact inactive representation agrees with the unchanged v2 consumer.
    backup.inactive(INACTIVE)
    with pytest.raises(backup.Refused):
        backup.inactive(ACTIVE)
    with pytest.raises(FrozenInstanceError):
        old.originally_active = True


@pytest.mark.parametrize('old,new', [
    (b'loaded', b'masked'), (b'loaded', b'not-found'), (b'inactive', b'unknown'),
    (b'inactive', b'activating'), (b'inactive', b'failed'), (b'dead', b'auto-restart'),
    (b'MainPID=0', b'MainPID=101'), (b'MainPID=0', b'MainPID=-1'),
    (b'MainPID=0', b'MainPID=00'), (b'MainPID=0', b'MainPID=True'),
    (b'ControlPID=0', b'ControlPID=44'), (b'Job=', b'Job=7'),
    (b'Job=\n', b''), (b'Job=\n', b'Job=\nJob=\n'),
    (b'Job=\n', b'Job=\nRestart=always\n'),
])
def test_unknown_transitional_restart_mask_and_queued_job_refuse(old, new):
    raw = INACTIVE.replace(old, new)
    with pytest.raises(coordinator.ContractRefused):
        coordinator.inspect_unit(raw)
    result = coordinator.compatibility_preflight(dict.fromkeys(coordinator.UNITS, raw))
    assert result['status'] == 'blocked' and not result['service_states_known']
    assert result['blockers'][0] == 'service_state_unknown'


@pytest.mark.parametrize('raw', [b'', b'\xff', b'x' * 4097, None, b'MainPID=0\n'])
def test_failed_or_unreadable_service_probe_refuses(raw):
    with pytest.raises(coordinator.ContractRefused):
        coordinator.inspect_unit(raw)


@pytest.mark.parametrize('pid', [b'0', b'1', b'-1', b'2147483648', b'99999999999'])
def test_active_snapshot_requires_a_bounded_real_main_pid(pid):
    with pytest.raises(coordinator.ContractRefused):
        coordinator.inspect_unit(ACTIVE.replace(b'MainPID=123', b'MainPID=' + pid))


def test_read_only_preflight_never_confuses_inactive_with_inhibited():
    observations = dict.fromkeys(coordinator.UNITS, INACTIVE)
    before = observations.copy()
    result = coordinator.compatibility_preflight(observations)
    assert observations == before
    assert result['service_states_known'] and result['helper_unit_state_compatible']
    assert result['status'] == 'blocked' and not result['maintenance_authorized']
    assert 'activation_barrier_unverified' in result['blockers']
    assert 'independent_guard_unverified' in result['blockers']
    assert 'owner_approval_verifier_unavailable' in result['blockers']
    observations[coordinator.UNITS[0]] = ACTIVE
    result = coordinator.compatibility_preflight(observations)
    assert result['service_states_known'] and not result['helper_unit_state_compatible']
    assert result['blockers'][0] == 'services_not_quiescent'
    assert not result['service_actions_performed'] and not result['backup_invoked']


@pytest.mark.parametrize('observations', [None, {}, {'other.service': INACTIVE},
    {coordinator.UNITS[0]: INACTIVE}, dict.fromkeys((*coordinator.UNITS, 'extra'), INACTIVE)])
def test_missing_old_unit_and_arbitrary_units_are_unknown(observations):
    assert coordinator.compatibility_preflight(observations)['blockers'][0] == 'service_state_unknown'


def test_exact_immutable_protocol_and_invocation_constants():
    raw = authorization()
    assert coordinator.inspect_authorization(raw, SESSION, NOW) is None
    assert coordinator.inspect_authorization(raw, SESSION, NOW + 1, original_raw=raw) is None
    assert coordinator.HELPER_COMMAND == (
        '/usr/bin/python3', '-I', '-S', '/usr/local/libexec/funpayflow-backup-create.py', 'create')
    assert coordinator.COORDINATOR == backup.COORDINATOR.as_posix()
    assert coordinator.COORDINATOR_RECEIPT == backup.COORDINATOR_RECEIPT.as_posix()
    assert coordinator.HELPER == backup.INSTALLED.as_posix()
    assert coordinator.HELPER_RECEIPT == backup.RECEIPT.as_posix()
    assert coordinator.UNITS == backup.UNITS
    with pytest.raises(FrozenInstanceError):
        SESSION.expires_boottime_ns += 1


@pytest.mark.parametrize('field,value', [
    ('version', True), ('version', 2), ('helper_pid', True), ('helper_pid', 102),
    ('helper_start', 301), ('coordinator_pid', 99), ('coordinator_start', 201),
    ('helper_sha256', 'd' * 64), ('coordinator_sha256', 'e' * 64),
    ('boot_id', '22222222-2222-2222-2222-222222222222'),
    ('expires_boottime_ns', SESSION.expires_boottime_ns + 1),
    ('expires_boottime_ns', float(SESSION.expires_boottime_ns)),
    ('units', list(reversed(coordinator.UNITS))), ('units', ['arbitrary.service']),
    ('data', '/tmp/source'), ('backups', '/tmp/backups'),
    ('services_inhibited', False), ('services_inhibited', 1),
    ('writers_quiescent', False), ('recovery_armed', False), ('recovery_armed', 1),
    ('extra', 'private marker'),
])
def test_missing_attestations_wrong_session_and_type_confusion_refuse(field, value):
    evidence = json.loads(authorization())
    evidence[field] = value
    with pytest.raises(coordinator.ContractRefused):
        coordinator.inspect_authorization(canonical(evidence), SESSION, NOW)


@pytest.mark.parametrize('field', list(json.loads(authorization())))
def test_every_protocol_field_is_required(field):
    evidence = json.loads(authorization())
    del evidence[field]
    with pytest.raises(coordinator.ContractRefused):
        coordinator.inspect_authorization(canonical(evidence), SESSION, NOW)


@pytest.mark.parametrize('raw', [
    None, b'', b'null', b'[]', b'\xff', b'x' * 4097,
    authorization() + b'\n', b' ' + authorization(),
    b'{"version":0,' + authorization()[1:],
    json.dumps(json.loads(authorization()), indent=2).encode(),
])
def test_noncanonical_duplicate_or_absent_authorization_refuses(raw):
    with pytest.raises(coordinator.ContractRefused):
        coordinator.inspect_authorization(raw, SESSION, NOW)


@pytest.mark.parametrize('field,value', [
    ('helper_pid', 1), ('helper_pid', True), ('helper_pid', SESSION.coordinator_pid),
    ('helper_start', 0), ('coordinator_start', False), ('coordinator_pid', -1),
    ('boot_id', 'unknown'), ('helper_sha256', 'A' * 64),
    ('expires_boottime_ns', True), ('expires_boottime_ns', NOW),
    ('expires_boottime_ns', NOW + coordinator.MAX_LEASE_NS + 1),
])
def test_invalid_expected_identity_or_lease_refuses(field, value):
    session = replace(SESSION, **{field: value})
    with pytest.raises(coordinator.ContractRefused):
        coordinator.inspect_authorization(authorization(session), session, NOW)


def test_lease_expires_at_deadline_and_cannot_be_replaced_or_extended():
    raw = authorization()
    coordinator.inspect_authorization(raw, SESSION, SESSION.expires_boottime_ns - 1)
    with pytest.raises(coordinator.ContractRefused):
        coordinator.inspect_authorization(raw, SESSION, SESSION.expires_boottime_ns)
    extended = replace(SESSION, expires_boottime_ns=SESSION.expires_boottime_ns + 1)
    with pytest.raises(coordinator.ContractRefused):
        coordinator.inspect_authorization(authorization(extended), extended, NOW, original_raw=raw)
    maximum = replace(SESSION, expires_boottime_ns=NOW + coordinator.MAX_LEASE_NS)
    coordinator.inspect_authorization(authorization(maximum), maximum, NOW)


@pytest.mark.parametrize('fault', [None, 'parent_crash', 'child_crash', 'pid_reused',
                                  'lease_expired', 'power_loss', 'absent_attestation'])
def test_synthetic_protocol_interoperates_with_unchanged_live_consumer(monkeypatch, fault):
    """Exercise v2's actual evidence consumer, with ALL Linux APIs replaced."""
    raw = authorization()
    metadata = SimpleNamespace(st_dev=7, st_ino=42, st_uid=0, st_nlink=1,
                               st_mode=stat.S_IFREG | 0o600, st_size=len(raw),
                               st_mtime_ns=1, st_ctime_ns=1)

    class FakePath(PurePosixPath):
        def resolve(self):
            return self

        def lstat(self):
            values = vars(metadata).copy()
            if self == backup.MAINTENANCE:
                values['st_mode'] = stat.S_IFDIR | 0o700
            return SimpleNamespace(**values)

    for name in ('DATA', 'BACKUPS', 'INSTALLED', 'RECEIPT', 'COORDINATOR',
                 'COORDINATOR_RECEIPT', 'MAINTENANCE', 'AUTHORIZATION', 'MAINTENANCE_LOCK'):
        monkeypatch.setattr(backup, name, FakePath(getattr(backup, name).as_posix()))
    monkeypatch.setattr(backup, 'Path', FakePath)
    monkeypatch.setattr(backup, 'protected', lambda *args, **kwargs: None)
    monkeypatch.setattr(backup, 'pinned_digest', lambda script, receipt:
                        SESSION.helper_sha256 if script == backup.INSTALLED else SESSION.coordinator_sha256)
    monkeypatch.setattr(backup, 'sys', SimpleNamespace(executable='/usr/bin/python3'))

    def identity(pid):
        if fault == 'parent_crash' and pid == 100 or fault == 'child_crash' and pid == 101:
            raise backup.Refused()
        if pid == 101:
            return 100, 300
        assert pid == 100
        return 1, 201 if fault == 'pid_reused' else 200

    def read(path, maximum):
        values = {
            '/proc/100/cmdline': b'/usr/bin/python3\0-I\0-S\0' +
                coordinator.COORDINATOR.encode() + b'\0maintain-backup\0',
            '/proc/100/status': b'Uid:\t0\t0\t0\t0\nGid:\t0\t0\t0\t0\n',
            '/proc/self/fdinfo/3': b'lock:\t1: FLOCK ADVISORY WRITE 100 00:07:42 0 EOF\n',
            str(backup.AUTHORIZATION): b'{}' if fault == 'absent_attestation' else raw,
            '/proc/sys/kernel/random/boot_id': ('22222222-2222-2222-2222-222222222222'
                                               if fault == 'power_loss' else SESSION.boot_id).encode(),
        }
        assert str(path) in values, 'unexpected host metadata access'
        return values[str(path)]

    def flock(fd, flags):
        assert fd == 4 and flags == 6
        raise BlockingIOError

    monkeypatch.setattr(backup, 'process_identity', identity)
    monkeypatch.setattr(backup, 'read_bounded', read)
    monkeypatch.setattr(backup, 'time', SimpleNamespace(CLOCK_BOOTTIME=7, clock_gettime_ns=lambda clock:
        SESSION.expires_boottime_ns if fault == 'lease_expired' else NOW))
    monkeypatch.setattr(backup, 'os', SimpleNamespace(
        name=os.name, getpid=lambda: 101, getppid=lambda: 100, fsdecode=os.fsdecode,
        readlink=lambda path: '/usr/bin/python3' if str(path).endswith('/exe') else 'same-host-ns',
        fstat=lambda fd: metadata, major=lambda device: 0, minor=lambda device: 7,
        open=lambda *args: 4, close=lambda fd: None, O_RDONLY=0, O_NOFOLLOW=1, O_NONBLOCK=2))
    monkeypatch.setitem(sys.modules, 'fcntl', SimpleNamespace(flock=flock, LOCK_EX=2, LOCK_NB=4))
    if fault is None:
        coordinator.inspect_authorization(raw, SESSION, NOW)
        assert backup.maintenance_evidence()[:2] == (100, raw)
    else:
        with pytest.raises(backup.Refused) as refused:
            backup.maintenance_evidence()
        assert refused.value.code == 'maintenance_required'


@pytest.mark.parametrize('command', [
    b'/usr/bin/python3 -I -S /usr/local/libexec/funpayflow-maintenance-guard.py',
    b'/usr/local/libexec/funpayflow-maintenance-guard',
])
def test_python_or_funpay_named_guard_is_incompatible_with_v2_process_scan(command):
    # The direct coordinator parent is the ONLY exception in v2's process scan.
    with pytest.raises(backup.Refused):
        backup.process_safe(command)


@pytest.mark.parametrize('event', ['completed', 'helper_failed', 'coordinator_lost',
                                  'cancelled', 'lease_expired', 'timeout'])
def test_recovery_design_requires_helper_exit_before_original_state_restoration(event):
    assert coordinator.recovery_requirement(event, 'running', same_boot=True) == 'reap_helper_before_restoration'
    assert coordinator.recovery_requirement(event, 'exit_confirmed', same_boot=True) == 'restore_original_state_required'
    assert coordinator.recovery_requirement(event, 'not_started', same_boot=True) == 'restore_original_state_required'
    for state in ('unknown', 'unreapable'):
        assert coordinator.recovery_requirement(event, state, same_boot=True) == 'manual_recovery_required'


def test_power_loss_guard_loss_and_kernel_stall_have_no_bounded_restoration_claim():
    for event in ('power_loss', 'guard_lost'):
        assert coordinator.recovery_requirement(event, 'exit_confirmed', same_boot=True) == 'manual_recovery_required'
    assert coordinator.recovery_requirement('completed', 'exit_confirmed', same_boot=False) == 'manual_recovery_required'
    assert coordinator.recovery_requirement('timeout', 'unreapable', same_boot=True) == 'manual_recovery_required'


@pytest.mark.parametrize('event,state,boot', [
    ('unknown', 'exit_confirmed', True), ('completed', 'dead', True),
    ('completed', 'exit_confirmed', 1), ('completed', 'exit_confirmed', None),
])
def test_unknown_recovery_observations_do_not_select_a_restoration_requirement(event, state, boot):
    with pytest.raises(coordinator.ContractRefused):
        coordinator.recovery_requirement(event, state, same_boot=boot)


@pytest.mark.parametrize('args', [[], ['--approve'], ['maintain-backup', '--approved'],
    ['maintain-backup', 'arbitrary.service'], ['maintain-backup', '/tmp/marker'],
    ['maintain-backup; stop'], ['create'], ['--help']])
def test_cli_rejects_arbitrary_arguments_without_echo_or_side_effects(args, capsys):
    assert coordinator.main(args) == 2
    output = capsys.readouterr()
    assert not output.err
    result = json.loads(output.out)
    assert result['status'] == 'invalid_arguments'
    assert not result['maintenance_authorized'] and not result['service_actions_performed']
    assert not result['backup_invoked']


def test_cli_always_refuses_even_with_self_asserted_approval_and_never_reads_host(monkeypatch, capsys):
    monkeypatch.setenv('FUNPAYFLOW_MAINTENANCE_APPROVED', '1')
    monkeypatch.setenv('FUNPAYFLOW_GUARD_ARMED', '1')

    def forbidden(*args, **kwargs):
        pytest.fail('unexpected host IO or operational path')

    monkeypatch.setattr('builtins.open', forbidden)
    monkeypatch.setattr(coordinator, 'compatibility_preflight', forbidden)
    assert coordinator.main(['maintain-backup']) == 1
    output = capsys.readouterr()
    assert output.err == ''
    result = json.loads(output.out)
    assert result['status'] == 'blocked'
    assert result['blockers'] == list(coordinator.BLOCKERS)
    assert not any(result[key] for key in ('maintenance_authorized', 'backup_invoked', 'service_actions_performed'))


def test_private_probe_failure_and_candidate_bytes_never_enter_reports(capsys):
    marker = 'SYNTHETIC_PRIVATE_DATA_DO_NOT_EMIT'
    result = coordinator.compatibility_preflight(dict.fromkeys(coordinator.UNITS, marker.encode()))
    assert marker not in json.dumps(result)
    with pytest.raises(coordinator.ContractRefused) as refused:
        coordinator.inspect_authorization(canonical({'private': marker}), SESSION, NOW)
    assert str(refused.value) == 'contract_invalid'
    assert capsys.readouterr() == ('', '')


def test_import_and_source_have_no_operational_implementation(monkeypatch):
    tree = ast.parse(SCRIPT.read_text(encoding='utf-8'))
    imports = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    imports.update(node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom))
    assert imports <= {'__future__', 'dataclasses', 'hashlib', 'json', 're', 'sys'}
    forbidden = {'open', 'exec', 'eval', '__import__', 'Popen', 'run', 'system', 'fork',
                 'kill', 'flock', 'write_bytes', 'mkdir', 'unlink', 'rmdir'}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, 'attr', '')
            assert name not in forbidden
    monkeypatch.setattr('builtins.print', lambda *args, **kwargs: pytest.fail('import output'))
    load('maintenance_contract_reimport', SCRIPT)
