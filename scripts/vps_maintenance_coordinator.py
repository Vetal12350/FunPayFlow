"""Offline maintenance contract; deliberately NOT an operational coordinator.

Every CLI invocation refuses. The pure inspectors below neither collect host
evidence nor confer authority. No service runner, approval issuer, lock creator,
helper launcher or recovery guard is provided. See VPS_MAINTENANCE_COORDINATOR.md.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import sys


COORDINATOR = '/usr/local/libexec/funpayflow-maintenance.py'
COORDINATOR_RECEIPT = '/usr/local/libexec/funpayflow-maintenance.reviewed.json'
HELPER = '/usr/local/libexec/funpayflow-backup-create.py'
HELPER_RECEIPT = '/usr/local/libexec/funpayflow-backup-create.reviewed.json'
HELPER_COMMAND = ('/usr/bin/python3', '-I', '-S', HELPER, 'create')
UNITS = ('funpayflow.service', 'funpay-bot.service')
DATA = '/root/FunPayFlow-data'
BACKUPS = '/root/backups'
MAX_LEASE_NS = 150 * 10**9
BLOCKERS = (
    'protected_installation_unverified',
    'owner_approval_verifier_unavailable',
    'independent_guard_unverified',
    'activation_barrier_unverified',
)


class ContractRefused(Exception):
    """Fixed diagnostics only; never retain supplied bytes or exception text."""

    def __init__(self):
        super().__init__('contract_invalid')


def require(condition):
    if not condition:
        raise ContractRefused()


def _object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result)
        result[key] = value
    return result


def _json(raw, maximum):
    try:
        require(type(raw) is bytes and len(raw) <= maximum)
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_object)
        require(type(value) is dict)
        return value
    except Exception:
        raise ContractRefused() from None


def _hex(value, length):
    return type(value) is str and re.fullmatch('[0-9a-f]{%d}' % length, value) is not None


def inspect_receipt(raw, script_bytes):
    """Check candidate receipt syntax/digest, NOT provenance or host protection."""
    receipt = _json(raw, 1024)
    require(set(receipt) == {'reviewed_commit', 'script_sha256'})
    require(_hex(receipt['reviewed_commit'], 40) and _hex(receipt['script_sha256'], 64))
    require(type(script_bytes) is bytes and len(script_bytes) <= 128 * 1024)
    require(hashlib.sha256(script_bytes).hexdigest() == receipt['script_sha256'])
    return receipt['reviewed_commit'], receipt['script_sha256']


@dataclass(frozen=True)
class UnitSnapshot:
    originally_active: bool
    main_pid: int

    @property
    def helper_compatible(self):
        return not self.originally_active


def inspect_unit(raw):
    """Parse a supplied exact v2 systemctl-show response without running probes.

    Capture stable active/running or inactive/dead only. A compatible inactive
    snapshot is NOT an activation barrier and cannot establish absence of writers.
    """
    try:
        require(type(raw) is bytes and len(raw) <= 4096)
        fields = {}
        for line in raw.decode('ascii').splitlines():
            key, separator, value = line.partition('=')
            require(separator and key not in fields)
            fields[key] = value
        require(set(fields) == {'LoadState', 'ActiveState', 'SubState',
                                'MainPID', 'ControlPID', 'Job'})
        require(fields['LoadState'] == 'loaded' and fields['Job'] == '')
        require(fields['ControlPID'] == '0')
        pid = fields['MainPID']
        require(re.fullmatch(r'0|[1-9][0-9]{0,9}', pid) is not None)
        pid = int(pid)
        active = (fields['ActiveState'], fields['SubState'])
        require(active == ('inactive', 'dead') and pid == 0
                or active == ('active', 'running') and 1 < pid <= 2**31 - 1)
        return UnitSnapshot(active == ('active', 'running'), pid)
    except Exception:
        raise ContractRefused() from None


@dataclass(frozen=True)
class Session:
    """Expected synthetic/live observations, never an approval or attestation."""

    helper_pid: int
    helper_start: int
    coordinator_pid: int
    coordinator_start: int
    helper_sha256: str
    coordinator_sha256: str
    boot_id: str
    expires_boottime_ns: int


def inspect_authorization(raw, session, now_boottime_ns, *, original_raw=None):
    """Check v1 bytes against an expected session; never write backup.json.

    This is deliberately insufficient for helper authorization: it does not
    inspect processes, namespaces, protected files, FD 3 or a kernel flock.
    The immutable deadline comes from session; original_raw pins subsequent
    observations. File/lock identities must ALSO be pinned by a future executor.
    """
    require(type(session) is Session)
    for value in (session.helper_pid, session.coordinator_pid):
        require(type(value) is int and 1 < value <= 2**31 - 1)
    require(session.helper_pid != session.coordinator_pid)
    for value in (session.helper_start, session.coordinator_start):
        require(type(value) is int and value > 0)
    require(_hex(session.helper_sha256, 64) and _hex(session.coordinator_sha256, 64))
    require(type(session.boot_id) is str and re.fullmatch(
        r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
        session.boot_id) is not None)
    require(type(now_boottime_ns) is int and now_boottime_ns >= 0)
    deadline = session.expires_boottime_ns
    require(type(deadline) is int and 0 < deadline - now_boottime_ns <= MAX_LEASE_NS)
    evidence = _json(raw, 4096)
    require(raw == json.dumps(evidence, sort_keys=True, separators=(',', ':')).encode('utf-8'))
    if original_raw is not None:
        require(type(original_raw) is bytes and raw == original_raw)
    expected = dict(version=1, helper_pid=session.helper_pid, helper_start=session.helper_start,
                    coordinator_pid=session.coordinator_pid, coordinator_start=session.coordinator_start,
                    helper_sha256=session.helper_sha256, coordinator_sha256=session.coordinator_sha256,
                    boot_id=session.boot_id, expires_boottime_ns=deadline, units=list(UNITS),
                    data=DATA, backups=BACKUPS, services_inhibited=True,
                    writers_quiescent=True, recovery_armed=True)
    require(evidence.keys() == expected.keys())
    # JSON bools compare equal to integers in Python; require exact types too.
    require(all(type(evidence[key]) is type(value) and evidence[key] == value
                for key, value in expected.items()))
    return None


def recovery_requirement(event, helper_state, *, same_boot):
    """Pure design oracle, NOT a watchdog, reaper or restoration authorization.

    Even 'restore_original_state_required' requires a separately verified guard,
    retained inhibition/lock, pinned original states and idempotent host actions.
    Unknown/dead guard or a reboot always needs separate manual assessment.
    """
    require(event in ('completed', 'helper_failed', 'coordinator_lost', 'cancelled',
                      'lease_expired', 'timeout', 'guard_lost', 'power_loss'))
    require(helper_state in ('not_started', 'running', 'exit_confirmed', 'unknown', 'unreapable'))
    require(type(same_boot) is bool)
    if not same_boot or event in ('guard_lost', 'power_loss') or helper_state in ('unknown', 'unreapable'):
        return 'manual_recovery_required'
    if helper_state == 'running':
        return 'reap_helper_before_restoration'
    return 'restore_original_state_required'


def _report(status, blockers, *, states_known=False, helper_compatible=False):
    return dict(status=status, blockers=list(blockers), maintenance_authorized=False,
                service_actions_performed=False, backup_invoked=False,
                service_states_known=states_known, helper_unit_state_compatible=helper_compatible)


def compatibility_preflight(unit_observations):
    """Read-only inspection of supplied observations. ALWAYS returns blocked.

    No host probe collector is supplied. CLI intentionally does not call this;
    callers cannot convert snapshots, forged approval markers or booleans into
    an operational capability. Original states are parsed independently per unit.
    """
    try:
        require(type(unit_observations) is dict and set(unit_observations) == set(UNITS))
        snapshots = tuple(inspect_unit(unit_observations[unit]) for unit in UNITS)
    except Exception:
        return _report('blocked', ('service_state_unknown', *BLOCKERS))
    compatible = all(snapshot.helper_compatible for snapshot in snapshots)
    blockers = BLOCKERS if compatible else ('services_not_quiescent', *BLOCKERS)
    return _report('blocked', blockers, states_known=True, helper_compatible=compatible)


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    valid = args == ['maintain-backup']
    # No root/installation/env/marker combination can enable this source version.
    # A later operational implementation requires a separately reviewed change.
    print(json.dumps(_report('blocked' if valid else 'invalid_arguments', BLOCKERS), sort_keys=True))
    return 1 if valid else 2


if __name__ == '__main__':
    raise SystemExit(main())
