# Standalone backup creator v2 — single writable ext4

Development only. This document authorizes no installation, VPS access, service
operation, mount operation, backup of user data, or deployment. The Worker must
never execute the production entry. No existing audit helper, deployment executor,
application code, CI, dependency, or old issue state is changed.

`scripts/vps_backup_create.py` implements a working snapshot and isolated recovery
primitive using only the standard library. Importing it has no side effects.
Synthetic tests inject directories and a fake Linux platform into `create`.
The CLI exposes only `create`; paths, service names, commands, environment
overrides, dry-run success, and installation actions are not accepted.

## Operational prerequisite / outstanding production blocker

This version supports the reported layout: `/root/FunPayFlow-data` and
`/root/backups` on the **same writable ext4 root filesystem**. Mount and superblock
options must both report `rw`. Separate devices, XFS, read-only mounts, bind aliases
of the root device, mounts over either path/ancestor and nested mounts refuse.
No device name is assumed. This is a filesystem layout check, not a freeze.

Polling inactive units, process metadata or stable file hashes cannot prevent a
writer starting between checks. A **separate trusted maintenance coordinator is
mandatory and is not implemented here**. Without its pinned installation, live
exclusive lock and process-bound authorization described below the helper refuses
`maintenance_required` before reading source payload or creating backup artifacts.
Running the helper manually after stopping services is insufficient.

Before authorizing a child, the future coordinator must obtain explicit owner
approval; record the original service state; inhibit ALL known activation paths
(including systemd restart, dependencies, timers, socket/path activation, legacy
units, scheduled jobs, manual launch wrappers and concurrent maintenance); stop
both known units; wait for all processes/children to exit; and establish that no
other users of persistent data remain, including SQLite readers and cooperative
root tools. It must retain that exclusion until the helper exits. A failed probe,
unexpected restart or a writer it cannot control means no authorization.

The coordinator must first arm an independent, bounded recovery mechanism that
reaps/waits for the helper and restores inhibition and the original running state
on success, failure, timeout or coordinator death. Never resume writers while the
helper is still copying/publishing. A `finally` block alone does not survive a
coordinator crash. The helper cannot implement or verify that recovery mechanism;
its evidence is a reviewed coordinator's attestation of this obligation. Neither
service mutation nor an arbitrary command runner exists in the helper.
Bounded recovery assumes responsive kernel/storage and verified helper exit.
Uninterruptible IO can defeat even SIGKILL: unconditional bounded restoration and
waiting for exit cannot both be promised in that case. Failure to reap the helper
is a fail-closed manual-recovery blocker, never permission to resume writers while
it may still be running. The later coordinator must explicitly handle this limit.

The security boundary assumes a trusted kernel, system Python/SQLite/stdlib,
systemd, and root administrator. Concurrent malicious root, namespace/mount
replacement, raw block-device writes and kernel compromise are outside it.
Other root tools must cooperate with the maintenance exclusion. A non-cooperating
root writer, a process that opens/writes/closes entirely between probes, private
thread FD tables, raw block writes, kernel faults and abrupt power loss are not
made safe by the probes. The coordinator must control writers independently;
pre/post hash+inode checks are additional detection, not a filesystem transaction.
Same-disk backups do **not** protect against disk failure. This is not production
ready until coordinator/installer bootstrap and an isolated application restore
test have been separately approved and completed. No reboot is needed for this
source-development task, and none is performed or proposed.

## Trust and invocation contract for a later reviewed installation

A separate trusted installer would verify the reviewed full commit and exact
script SHA-256, then place the script at
`/usr/local/libexec/funpayflow-backup-create.py`, owned by root, mode `0600` or
`0644`, single-link, outside every Git checkout. All ancestors must be root-owned
real directories without group/other writes, special permission bits or `.git`.
Neither a script symlink nor a writable ancestor is accepted.

An adjacent protected, root-owned, single-link non-executable receipt,
`/usr/local/libexec/funpayflow-backup-create.reviewed.json`, must contain exactly
`reviewed_commit` (40 lowercase hex characters) and `script_sha256` (64 lowercase
hex characters). The helper checks the installed bytes against this receipt.
The receipt is an **installer attestation**, not cryptographic proof that GitHub
review occurred. Writing it from an unreviewed checkout defeats that attestation;
this task supplies no installer or receipt.

Only this future invocation shape is accepted, using trusted system Python:

```text
/usr/bin/python3 -I -S /usr/local/libexec/funpayflow-backup-create.py create
```

The real executable must resolve to `/usr/bin/python3` or a numeric versioned
equivalent. Real/effective UID and GID must all be zero. The script checks
isolation/no-site flags, `__file__`, `argv[0]`, the exact `/proc/self/cmdline`
argument list, interpreter ownership/mode and stdlib search-path protection.
An explicit versioned system interpreter is also accepted. Direct shebang
execution, import-and-call wrappers, editable checkout invocations and extra
options refuse. `-I -S` is necessary before imports; Python environment injection
is not repaired after startup.

## Live coordinator authorization protocol (version 1)

The trusted installer must also pin a separately reviewed coordinator at
`/usr/local/libexec/funpayflow-maintenance.py`, with adjacent
`funpayflow-maintenance.reviewed.json` using exactly the same receipt schema,
ownership, link, permission, ancestor and digest rules. The helper verifies both
installed digests repeatedly. This task supplies neither that program nor its
receipt. The coordinator must run directly as root with the same protected system
interpreter and exact arguments `-I -S <fixed-coordinator-path> maintain-backup`.
It must be the helper's live direct parent, in the host PID/mount/user namespaces,
with all real/effective/saved/filesystem UID/GID values zero. Only that validated
parent is exempted from the conservative Python/process-name rejection; its data
FDs, cwd and mappings remain subject to the same checks as other processes.

The coordinator creates `/run/funpayflow-maintenance` as a protected private real
root-owned directory (`0700`) and a permanent private single-link regular
`exclusive.lock` (`0600`). All cooperating writers/coordinators must honor this
exclusion. It holds a nonblocking exclusive `flock` for the entire maintenance and
recovery window and passes the **same open file description as inherited FD 3**
to the child. It must keep its own descriptor open. The helper never creates,
replaces, unlocks or removes these objects. It verifies FD 3's inode, the kernel
`fdinfo` FLOCK/ADVISORY/WRITE record, original owning parent PID, device/inode and
whole-file range, and that an independently opened no-follow descriptor cannot
acquire an exclusive lock. Missing/unsupported kernel lock evidence fails closed.
The separate destination `.backup-create.lock` remains helper-managed and is not
authorization evidence.

After forking a blocked child (before exec), the coordinator records the child's
PID/start ticks, writes and fsyncs private single-link `backup.json` in that fixed
directory, then lets the child exec the pinned helper. It must use canonical JSON
(`sort_keys=True`, `separators=(',', ':')`, UTF-8, no newline), at most 4096 bytes,
with **exactly** these fields:

| Field | Required value |
| --- | --- |
| `version` | `1` |
| `helper_pid`, `helper_start` | Child PID and `/proc/<pid>/stat` field 22 |
| `coordinator_pid`, `coordinator_start` | Parent PID and start ticks |
| `helper_sha256`, `coordinator_sha256` | Respective installed reviewed digests |
| `boot_id` | Current `/proc/sys/kernel/random/boot_id` |
| `expires_boottime_ns` | Integer absolute `CLOCK_BOOTTIME` deadline |
| `units` | `["funpayflow.service","funpay-bot.service"]`, in this order |
| `data`, `backups` | `/root/FunPayFlow-data`, `/root/backups` |
| `services_inhibited`, `writers_quiescent`, `recovery_armed` | All `true`, attesting the obligations above |

The lease must be live and no more than 150 seconds ahead at any check (helper
budget 120 seconds plus 30 seconds scheduling allowance); it cannot be extended.
The authorization bytes, file identity, lock inode and parent identity must remain
identical across checks. The installer/coordinator must forbid replacing either
pinned script, receipt, authorization or lock throughout the lease: a hash of
on-disk bytes cannot prove which bytes an already running parent loaded.
Boot/start ticks reject stale PID reuse. No CLI/env/path
override, arbitrary token or helper-generated marker is accepted. The booleans
are not independent proof of service inhibition: authority derives from the live
pinned coordinator and trusted root installer. Root can forge receipts/argv or
replace trusted code; that administrator is explicitly inside the trust boundary.
Do not create a receipt or attestation by hand to bypass the missing coordinator.

The coordinator's inhibition mechanism must remain compatible with the strict
existing `LoadState=loaded`, `ActiveState=inactive`, `SubState=dead`, zero PID and
empty Job probes. A runtime mask that changes LoadState to `masked` currently
refuses; the future coordinator design must resolve/test this deliberately narrow
interface before installation, rather than silently weakening unit checks.

## Snapshot and validation

1. Validate fixed source/destination paths and their ancestors without reading
   private file contents. Require host systemd namespaces, both known units
   loaded/inactive/dead, zero MainPID/ControlPID and no queued Job. Missing units,
   active/failed/transitional states, duplicate fields and failed probes refuse.
   Only fixed, read-only `/usr/bin/systemctl --system --no-pager show` queries run,
   with a clean fixed environment, bounded output and timeout.
2. Scan bounded `/proc` metadata and enumerate source inode metadata without
   reading payload. Refuse FunPay/SecureBot processes, other Python/PyPy processes
   except the validated coordinator, **all** file descriptors/mappings of source
   inodes or paths (including read-only WAL readers and deleted paths), cwd inside
   data, unreadable/ambiguous state, namespace mismatch or PID-list churn.
   Only the helper's own PID is excluded. This conservative policy can refuse
   unrelated Python jobs; ordinary descriptors elsewhere on the same root device
   are allowed. It never kills or signals bot processes.
3. Require the rw ext4 layout and live coordinator evidence; acquire a nonblocking
   exclusive local `flock` on private `/root/backups/.backup-create.lock`. Never
   unlink the permanent lock inode. Repeat authorization, quiescence and layout checks under
   the lock before inventory or snapshot creation.
4. Enumerate and SHA-256 hash the **whole** bounded data tree. Require `.env`,
   `bot_settings.json`, `state.sqlite3` as root regular files. Include optional
   progress, statistics, logs, imports, nested assets and empty directories.
   Files must be root-owned single-link regular files with owner read permission,
   no executable/special bits and no group/other permissions. Directories must be
   root-owned, private and owner-readable/searchable. Refuse links, sockets,
   devices, FIFOs, cross-device subtrees, missing/unreadable files and mutations.
   Unsafe names, case collisions and oversized inventories refuse the entire
   backup; nothing unknown is silently skipped.
5. Create one random private `.pending-*` directory below backups, with separate
   input, tree, recovery and publication-bundle subdirectories. Use umask `077`,
   `0700` directories, `0600` files, exclusive creation and no-follow reads.
   Copy ordinary files under coordinator exclusion and recheck sizes/digests and
   identities. Filesystem immutability is not claimed.
6. Clone the source database and any WAL into private input. Validate nonempty WAL
   headers, page sizes, salts, cumulative checksums, complete frames and a final
   commit. A damaged, truncated, uncommitted or stale reused tail refuses, even
   when SQLite itself might ignore it. This intentionally rejects some legitimate
   reused WALs. The parser follows the
   [SQLite WAL format](https://www.sqlite.org/fileformat2.html#wal_file_format).
   Use `sqlite3.Connection.backup` from a read-only connection to this private
   clone into a fresh staged standalone database. This includes committed WAL
   transactions without permitting SQLite to write source SHM or checkpoints.
   Disable extension loading and trusted schema, bound VM work/page counts, and
   run both `quick_check(1)` and `integrity_check(1)` on the standalone copy.
7. Root `bot.lock`, `state.sqlite3-wal` and `state.sqlite3-shm` are enumerated and
   hashed but excluded from the restoration tree. Never restore stale `bot.lock`.
   The coherent standalone database supersedes WAL/SHM. A root rollback journal
   refuses and needs separate review; unknown nested progress is retained.
8. Write a deterministic, uncompressed, strict ustar archive and hash it. Verify
   exact expected headers, payload hashes, padding and termination while extracting
   into the **separate newly created recovery directory**. No general tar extraction,
   archive-selected path, script execution, bot import, authentication, replay or
   production restore occurs. Recheck all recovered file/directory entries and
   run both SQLite checks again.
9. Write and reread a bounded private manifest recording every archived entry's
   size/hash, transient exclusions, archive SHA-256/size, verification scope,
   `runtime_sha: "unknown"` and `code_compatibility: "unknown"`. No runtime SHA is
   inferred from the checkout. Rehash the exact original inventory, repeat
   authorization/quiescence/layout checks and rehash the archive immediately
   before publishing.
10. Fsync files and bundle, publish the directory atomically on the destination
    filesystem via Linux `renameat2(RENAME_NOREPLACE)`, then fsync the destination
    and revalidate coordinator evidence before returning success.
    The bundle contains `data.tar` and `manifest.json`. Existing names, including
    empty directories, are never replaced. No retention or old-backup deletion
    exists. Missing kernel/libc support refuses.

`restore_verified: true` means only that this newly created artifact passed the
documented offline filesystem and SQLite checks. It does **not** establish
settings semantics, application-level cross-file invariants, version compatibility,
live recoverability, authentication, encryption, offsite durability or safe deploy.
The manifest contains private filenames and hashes: keep it private alongside the
archive. Hashes detect corruption here, not tampering by an administrator who can
replace both the archive and manifest. This is not a general verifier for old or
untrusted archives; the existing audit helper is unchanged and has smaller limits.

## Bounds, failures and operator-visible output

Limits: 2048 entries including directories, depth 12, 100-byte printable ASCII
relative paths, 256 MiB/file, 1 GiB total source data, 2 MiB manifest, uncompressed
archive at most 1 GiB plus bounded headers/padding. Non-ASCII names, reserved device
names, control characters, traversal and nonportable separators refuse. Streaming
uses 64 KiB chunks. SQLite checks each have a 20-million-VM-step budget.
Up to 4096 processes and 16384 descriptors/process are inspected, with bounded
process metadata reads. The work budget is 120 seconds; each systemctl probe
has five seconds. Disk admission requires five times source bytes plus 512 MiB
reserve, four times the maximum standalone database size (1 GiB), bounded
archive overhead and manifest allowance, plus free inodes. Subsequent IO/ENOSPC
errors refuse; the helper cannot reserve space against unrelated consumers.

The Linux CLI additionally sets a process alarm. A kernel IO call stuck in
uninterruptible sleep cannot be given a user-space hard completion guarantee.
Power loss/SIGKILL can leave private pending directories. Cleanup deletes only
recorded newly created objects after checking owner, type, inode and parent
identities; unexpected objects, replacements or permission/IO errors refuse
cleanup instead of deleting unknown data. It never reuses or sweeps old pending
directories. Any later cleanup requires separate ownership verification.

Output is one small JSON object: fixed-enum `status`, `files`, `bytes` and
`restore_verified`. Failures have zero counts and false verification. No paths,
names, private values, exception text, stderr diagnostics or traceback are emitted
by the helper. Exit codes: 0 created, 1 refusal/failure, 2 invalid arguments.
Statuses include `untrusted_installation`, `maintenance_required`, `not_quiescent`,
`source_not_frozen`,
`unsafe_source`, `limit_exceeded`, `insufficient_space`, `busy`,
`verification_failed`, `io_failed`, `cleanup_failed`, `publication_unknown`.
After any attempted publication failure or post-publication fsync/cleanup failure,
the outcome may be UNKNOWN: preserve remaining pending and possible published
artifacts, inspect them in a separately authorized procedure, and do not blindly
retry or delete them.

## Offline verification and later operator path

Use an existing development environment; do not install dependencies for this task.
Create `tests/.offline-tmp/` if absent and direct **all** scratch there:

```text
PYTHONDONTWRITEBYTECODE=1 python -B -m pytest -q -p no:cacheprovider --basetemp=tests/.offline-tmp/backup-create tests/test_vps_backup_create.py
```

Windows tests simulate Linux ownership, permissions, service states, mount status
and process metadata. A test-only scratch-scoped mkdir translation avoids Python
3.13's Windows `0700` ACL behavior excluding the Worker's sandbox identity.
No test calls real systemctl, scans real service processes, touches private data,
uses network/SSH, installs the helper or runs the production CLI successfully.
Windows/Linux CI and Product/Core QA plus independent ChatGPT code/security review
remain mandatory external merge gates; a local test pass does not claim them.

Before production use, the remaining separate owner-approved work is:

- Implement/review the coordinator, all activation inhibition, bounded independent
  recovery and the exact protocol above, including systemd-probe compatibility.
- Bootstrap the protected installer/receipts from independently verified commits
  and digests; never run a writable Git checkout as root.
- Fully validate historical candidates and the new artifact in isolated restore
  tests, including application settings/progress, code/state compatibility and
  old-code rollback. None of the five historical candidates is certified here.
- Design restricted forced-command SSH identity, GitHub Environment required
  reviewer, explicit maintenance approval, staging, health checks and rollback.
  Retention/offsite durability and disk-failure protection remain separate work.

No arbitrary Codex commands should be accepted. This issue does not arm PR #72/#76
or implement any of those operations. `CURRENT_STATE` was considered but is not
edited because this task explicitly allows only these three files.

## v1 feasibility audit / intentional refusals

The dedicated RO/different-device gates in v1 were incompatible with the reported
root layout. Its device-wide writable FD/mapping rejection would also reject
ordinary root-filesystem system daemons in v2. Scans now target source inode/path
references; they do not supply the missing exclusion contract. V1's space estimate
also missed that WAL page numbers can expand a small clone into a much larger
standalone database; v2 budgets that maximum expansion. Archive and staged-tree
limits remain checked after SQLite conversion, so expansion may still safely
refuse rather than exceed limits.

Actual data sizes/permissions/filenames have not been inspected. Existing strict
limits may reject the host's logs, imported assets, non-ASCII/long filenames,
group-readable files, more than 2048 entries, over 256 MiB per file, over 1 GiB
source or staged tree, or a backup taking over 120 seconds. The WAL validator also
intentionally rejects reused stale tails and uncommitted/truncated tails that
SQLite itself may ignore. Do not raise limits or discard files based on guessing;
later authorized metadata-only assessment and separate review are required.
Whole-tree inventory preserves nested settings/progress, historical data and
unknown regular files, or refuses the entire operation. A coherent standalone
SQLite DB supersedes only the enumerated root WAL/SHM; no source SQLite connection
is opened and source SHM/checkpoints are never modified.
