# Maintenance coordinator: offline contract, operational entry blocked

This is a limited fail-closed implementation for issue #99. It **cannot perform
maintenance**. `scripts/vps_maintenance_coordinator.py` contains pure candidate
receipt, service-state and authorization inspectors, a recovery requirements
oracle, and an entry point that always refuses. It contains no host probe
collector, service commands, helper execution, approval issuer, installation,
lock/authorization-file creation or recovery supervisor. Importing it performs no
host IO. There is no environment variable, marker, CLI approval flag or successful
preflight result that enables operations.

No production observation, backup, installation, reboot, deployment or service
operation was performed. Historical host descriptions in the issue are not current
evidence. No capability or restore certification is added to the existing backup
helper. PR #72/#76 remain unready. Historical archives are untouched.

## Retry isolation and duplicate check

Before implementation, read-only GitHub searches in `Vetal12350/FunPayFlow` for
`head:codex/issue-98` and `"98"`, across all PR states, returned no PRs on
2026-10-21. This is a point-in-time duplicate check, not a lock against a later PR;
the trusted publishing worker must recheck before publication. No GitHub mutation
was performed.

Work began on the separate `codex/issue-99` checkout at
`b8189e9fe3e0865392a6f6790aa3b3a658a6c1b0`. No tracked partial changes were present;
an existing unrelated untracked file was left alone. No issue #98 worktree, job
state, lock or historical file was read, resumed, edited or reused.

## What the source implements

| Interface | Checks | Does not establish |
| --- | --- | --- |
| `inspect_receipt` | Exact two-field receipt schema, lowercase commit/digest, byte limits, candidate script SHA-256, duplicate rejection | Review provenance, ownership, safe ancestors or execution identity |
| `inspect_unit` | Exact v2 six-property response, loaded unit, empty Job, zero ControlPID, stable active/running with MainPID or inactive/dead with zero MainPID | Activation exclusion, writer exclusion or fresh host observation |
| `compatibility_preflight` | Both fixed units present, distinguish original active/inactive, strict inactive compatibility with v2 | Permission to stop or invoke helper; result is always `blocked` |
| `inspect_authorization` | Exact canonical v1 bytes against an expected immutable session, strict scalar types, fixed paths/units, digests, boot/PID/start bindings, lease bounds, unchanged subsequent bytes | Live parent, host namespaces, kernel FD/flock evidence, approval, protected file identity or recovery |
| `recovery_requirement` | Pure recovery decision requirements for supplied synthetic facts | Monitoring, termination, reaping, rollback, authorization or any service action |
| CLI | Exact `maintain-backup` argument shape | All operations; exits 1 with fixed blockers, other arguments exit 2 |

There is no supported operational command in this revision. Even a protected
installation of these exact bytes would refuse. The four unconditional blockers
are `protected_installation_unverified`, `owner_approval_verifier_unavailable`,
`independent_guard_unverified` and `activation_barrier_unverified`. Output consists
only of fixed status/blocker enums and booleans; input strings, paths, exception
text, logs, filenames and private data are never echoed. The inspectors do not
read files. They accept supplied bytes only and raise fixed `contract_invalid`.

An active original main service and inactive original legacy service are recorded
as different immutable `UnitSnapshot` values. The diagnostic preflight reports
only booleans; it neither persists these snapshots nor claims they are sufficient
for restoration. Future restoration must preserve the originally inactive unit.
Extra/missing units, unknown/failed/transitional states, queued jobs, control PIDs,
duplicate properties and `LoadState=masked` refuse observation compatibility.

## Independent recovery must precede service stop

A future host implementation must establish this sequence before it can issue
the v1 attestation. This revision executes none of it:

1. Verify protected installation, exact reviewed release, root identities,
   isolated system Python and host namespaces. Acquire the permanent exclusion.
2. Independently verify a separate human-approved host-side release/session
   authorization. An adjacent reviewed receipt only attests installer provenance;
   it is not owner's maintenance approval. No approval format, verifier or trusted
   release mechanism is invented here. Require one-use/session binding to both
   reviewed scripts, release, boot and the bounded maintenance window; reject
   replay, revocation, expired or ambiguous approval.
3. Capture both original unit states and establish all activation/writer barriers.
   Pre-arm a supervisor-managed recovery guard before any stop can be considered.
   Verify readiness and retained recovery responsibility independently of the
   coordinator process, its shell, cancellation scope and threads.
4. Recheck approval, guard, exclusion, activation barriers and unit state. Stop
   only with separately approved fixed host actions, wait for exit, establish
   cooperative quiescence, and recheck all writers/activation paths.
5. Fork a child blocked before exec, bind its PID/start ticks, publish/fsync the
   immutable v1 authorization, then exec only the pinned helper with inherited
   FD 3. Keep coordinator identity, lock and barriers for the whole helper run.
6. On completion, error, cancellation, lease expiry or coordinator loss, the
   independent guard must account for and reap/wait for the exact helper and all
   relevant descendants before restoration. A zombie, timeout, successful signal
   delivery or absent main PID alone is not proof of completed work. Only after
   verified exit may it undo its own barriers and restore the original active
   state once, leaving the originally inactive legacy unit inactive. Keep the
   exclusion until restoration ownership is resolved. Preserve unknown artifacts.

The guard requires a bounded independent deadline and verified host supervisor
semantics, including cancellation isolation, cgroup/process identity, helper
descendants, descriptor ownership, lock retention and idempotent restoration.
If the guard inherits the flock, that must not hide a dead coordinator from the
helper or release inhibition prematurely. A Python `finally`, thread or ordinary
child in the cancelled job is insufficient. No dummy `vps_maintenance_guard.py`
is provided: it would imply an unimplemented independent supervisor.

The oracle uses **requirements**, not permissions:

| Supplied situation | Requirement |
| --- | --- |
| Healthy completion, failure, cancellation or parent loss; helper still running | Reap helper before restoration |
| Same events; exact helper exit confirmed, or it never started | Original-state restoration still required through verified guard |
| Helper exit unknown or kernel D-state prevents reaping | Manual recovery; never resume writers on this evidence |
| Guard lost, changed boot or power loss | Manual recovery; stale state never authorizes automatic restart or a fresh backup |

Power loss kills user-space guards and destroys `/run` locks/authorization.
Restart ordering and durable recovery responsibilities need separately reviewed
host boot design; no automatic boot reconciliation is implemented. A kernel or
storage stall can prevent reaping despite SIGKILL. Bounded recovery and guaranteed
helper exit cannot both be promised in that case. Availability is not guaranteed;
inhibition/manual recovery is safer than resuming writers against a live helper.
Repeated oracle calls have no side effects; this is **not a test of real rollback**.

## Named activation and guard compatibility blockers

The unchanged helper requires both units to report precisely
`LoadState=loaded`, `ActiveState=inactive`, `SubState=dead`, MainPID/ControlPID zero
and blank Job. Runtime masking that reports `masked` is incompatible. Stable
inactive polling does not inhibit a queued/new dependency, restart, timer,
socket/path trigger, legacy wrapper, scheduled job or manual/cooperative writer.

No exact host unit definitions or complete activation graph have been verified.
Consequently this revision implements an unconditional activation barrier refusal,
not speculative systemd modifications. `Restart=always`, dependency activation or
a timer can invalidate a snapshot immediately. Future approved metadata-only
preflight must inventory and pin unit fragments/drop-ins, restart/job semantics,
all triggers/dependencies and manual/scheduled wrappers, plus all cooperating root
tools and data readers. Every activation path needs an enforceable exclusion
compatible with the exact loaded/inactive probe; unknown paths/probe failures
refuse. No proposed override is declared sufficient here.

There is an additional **guard-process compatibility blocker** in the current
helper: `Linux.quiescent` rejects every other Python/PyPy process and every command,
executable or cwd containing `funpay`/`securebot`. Only the validated direct parent
coordinator is exempt from name rejection; its data references are still checked.
An independent Python guard, or a native guard with a FunPay-named command, would
therefore fail quiescence. Regression tests exercise the unchanged rejection. An
independently supervised, compatible mechanism must be demonstrated before an
operational implementation. The protocol is not intrinsically contradictory: it
does not prescribe Python for the independent mechanism. This task neither
implements such a mechanism nor weakens helper checks to admit one.

Process scans cannot prove no writers. The security boundary requires trusted
kernel, systemd, interpreter, installer and cooperative root tools. Concurrent
malicious root, arbitrary raw-device writes and kernel compromise are outside
that boundary. Uncontrolled cooperative writers remain an operational blocker.

## Exact v2 protocol retained

The authority is the [existing live protocol](VPS_BACKUP_CREATE.md#live-coordinator-authorization-protocol-version-1),
not this inspector. No helper changes or protocol extensions were made.

Future fixed installation paths are `/usr/local/libexec/funpayflow-maintenance.py`
and `/usr/local/libexec/funpayflow-backup-create.py`, each with its adjacent
`*.reviewed.json` receipt. Receipts contain exactly `reviewed_commit` and
`script_sha256`. A separately approved installer must pin independently reviewed
bytes and full commits; root-only real ancestors, no checkout ancestor, symlinks,
hardlinks, special bits or writable parents; scripts/receipts root-owned single
link with the helper's permitted non-executable modes. Never execute a Git checkout
as root. No installer, receipt or approval marker is created by this revision.

The coordinator invocation must be `/usr/bin/python3 -I -S` followed by the fixed
coordinator path and `maintain-backup`. It must be the helper's direct live parent,
with pinned PID/start ticks, host PID/mount/user namespaces, all root credential
identities and protected interpreter/stdlib. The helper invocation must be exactly
`/usr/bin/python3 -I -S /usr/local/libexec/funpayflow-backup-create.py create`.
No arbitrary arguments, services, paths, environment overrides or shell exist.

The permanent `/run/funpayflow-maintenance/exclusive.lock` must remain a protected
single-link `0600` file in a private `0700` root directory. A nonblocking exclusive
whole-file flock must be held by the coordinator, retained through recovery and
inherited as the same open file description at FD 3. The helper checks fdinfo
FLOCK/ADVISORY/WRITE owner PID/device/inode/whole-file range and independently
failed acquisition. A marker or unrelated lock is insufficient.

`backup.json` must be private, single-link, fsynced, at most 4096 bytes, with
canonical UTF-8 JSON (`sort_keys=True`, `separators=(',', ':')`, no newline) and
exactly: `version`, `helper_pid`, `helper_start`, `coordinator_pid`,
`coordinator_start`, `helper_sha256`, `coordinator_sha256`, `boot_id`,
`expires_boottime_ns`, `units`, `data`, `backups`, `services_inhibited`,
`writers_quiescent`, `recovery_armed`. Version is integer 1; all attestations are
literal true. Unit order and both paths are fixed. Deadline uses CLOCK_BOOTTIME,
is live and at most 150 seconds ahead (120-second helper work budget plus 30
seconds scheduling allowance), and cannot be extended. Authorization bytes and
identity, lock inode, script/receipt identities and parent identities must stay
pinned across checks and until the helper exits. Installer replacement must be
excluded for the entire session: an on-disk hash alone cannot identify bytes
already loaded into a process.

`inspect_authorization` deliberately verifies only the byte/session part. The
regression test supplies those bytes to the actual unchanged
`maintenance_evidence` with synthetic Linux metadata, descriptors, flock and time.
It is not a Linux integration test or proof of live FD inheritance.

## Offline verification and outstanding gates

Use the existing environment, without dependency installation:

```text
python -B -c "from pathlib import Path; Path('tests/.offline-tmp').mkdir(parents=True, exist_ok=True)"
python -B -m pytest -q -p no:cacheprovider --basetemp=tests/.offline-tmp/maintenance tests/test_vps_maintenance_coordinator.py tests/test_vps_backup_create.py
```

Local verification on Windows, Python 3.13.15: **338 passed** with the command
above. No skips were reported. This is the targeted coordinator/backup regression
suite; the full application suite and Linux integration were not run for this
standalone, non-operational source contract.

All scratch belongs under `tests/.offline-tmp/`; the new contract tests themselves
need no scratch files. Tests cover strict receipts/states/canonical evidence,
missing attestations, PID reuse and parent/child loss, boot change, expiry and
lease extension, incompatible guard process names, loaded versus masked units,
always-blocked approval/guard/barrier gates, argument injection, output sanitation,
import safety and recovery requirements. Parent/child loss checks exercise mocked
helper refusal; cancellation/timeouts/power loss/rollback checks cover the design
oracle only. There is no healthy end-to-end maintenance run, actual crash recovery,
supervisor integration or service restoration test. Existing helper tests provide
synthetic snapshot/SQLite coverage without real customer data.

Before any merge/use, obtain actual Windows/Linux CI, Product/Core QA and an
independent ChatGPT audit. Local results do not claim those external passes.
Before any future production maintenance, resolve and separately review/approve:

- Complete host compatibility inventory and a tested compatible activation barrier.
- Protected installer, release pinning, replacement exclusion and a separate
  human-approved host-side authorization verifier with replay/session protection.
- Independent supervised recovery, process-name compatibility, bounded helper
  termination/reaping, cancellation isolation and durable boot/manual recovery.
- Fault-injected host integration tests for crash, timeout, duplicate process,
  auto-restart, power loss and repeated original-state rollback, on synthetic data.
- Isolated application-level restore tests, runtime/code compatibility and
  operator-approved health/rollback procedure. `created` is not restore certification.

An operational executor requires a new reviewed source change and explicit
separate owner approval for actual host actions. No forced-command key, Environment
reviewer, updater, deployment, retry of UNKNOWN publication, retention or offsite
backup is implemented. `CURRENT_STATE` was considered; existing docs are not
modified because this task permits new files only. This document records the
capabilities and unresolved prerequisites instead.
