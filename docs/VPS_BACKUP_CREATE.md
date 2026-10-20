# Standalone backup creator v1

Development only. This document authorizes no installation, VPS access, service
operation, mount operation, backup of user data, or deployment. The Worker must
never execute the production entry. No existing audit helper, deployment executor,
application code, CI, dependency, or old issue state is changed.

`scripts/vps_backup_create.py` implements a working snapshot and isolated recovery
primitive using only the standard library. Importing it has no side effects.
Synthetic tests inject directories and a fake Linux platform into `create`.
The CLI exposes only `create`; paths, service names, commands, environment
overrides, dry-run success, and installation actions are not accepted.

## Deliberately narrower production prerequisite / outstanding blocker

Polling inactive services and processes cannot prevent a writer from starting
between polls. Therefore this version **also requires an already read-only,
dedicated ext4 or XFS filesystem mounted at `/root/FunPayFlow-data`**, with its
filesystem root mounted there, no nested mounts, and both mount options and
filesystem superblock options reporting read-only. All visible aliases of that
device must be read-only. A read-only bind mount over a writable filesystem is
insufficient. `/root/backups` must be on a different writable filesystem.
The helper never creates this prerequisite or remounts anything.

The supplied historical VPS observations do not establish that prerequisite.
If data and backups share the root filesystem, this helper refuses even with
both services stopped. **A practical one-approval update pipeline for that layout
remains blocked** on a separately designed, reviewed, owner-approved means of
providing an immutable all-state source. Do not work around the gate, remount the
root filesystem, or infer that this task authorizes storage changes.

The security boundary assumes a trusted kernel, system Python/SQLite/stdlib,
systemd, and root administrator. Concurrent malicious root, namespace/mount
replacement, raw block-device writes and kernel compromise are outside it.
The future approved maintenance coordinator must hold the filesystem read-only
and prevent service activation throughout the operation. No claim is made that
process-name heuristics alone prove absence of all possible bot implementations.

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

## Snapshot and validation

1. Validate fixed source/destination paths and their ancestors without reading
   private file contents. Require host systemd namespaces, both known units
   loaded/inactive/dead, zero MainPID/ControlPID and no queued Job. Missing units,
   active/failed/transitional states, duplicate fields and failed probes refuse.
   Only fixed, read-only `/usr/bin/systemctl --system --no-pager show` queries run,
   with a clean fixed environment, bounded output and timeout.
2. Scan bounded `/proc` metadata. Refuse FunPay/SecureBot processes, all other
   Python/PyPy processes, writable file descriptors or writable mappings of the
   data device, unreadable/ambiguous state, namespace mismatch or PID-list churn.
   Only the helper's own PID is excluded. This conservative policy can refuse
   unrelated Python maintenance jobs. It never kills or signals bot processes.
3. Require the immutable filesystem prerequisite and acquire a nonblocking
   exclusive local `flock` on private `/root/backups/.backup-create.lock`. Never
   unlink the permanent lock inode. Repeat quiescence and read-only checks under
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
   Copy ordinary files atomically with respect to the immutable source and
   recheck sizes/digests and identities.
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
   quiescence/read-only checks and rehash the archive immediately before publishing.
10. Fsync files and bundle, publish the directory atomically on the destination
    filesystem via Linux `renameat2(RENAME_NOREPLACE)`, then fsync the destination.
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
process metadata reads. The overall deadline is 120 seconds; each systemctl probe
has five seconds. Disk admission requires five times source bytes plus 512 MiB
reserve and manifest allowance, plus free inodes. Subsequent IO/ENOSPC errors
refuse; the helper cannot reserve space against unrelated consumers.

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
Statuses include `untrusted_installation`, `not_quiescent`, `source_not_frozen`,
`unsafe_source`, `limit_exceeded`, `insufficient_space`, `busy`,
`verification_failed`, `io_failed`, `cleanup_failed`, `publication_unknown`.
After any attempted publication failure or post-publication fsync/cleanup failure,
the outcome may be UNKNOWN: preserve possible published artifacts, inspect them
in a separately authorized procedure, and do not blindly retry or delete them.

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

After those gates and a separately owner-approved maintenance window, a future
coordinator must validate previous backups, solve the immutable-source prerequisite,
install the pinned reviewed helper, quiesce writers, create and verify the snapshot,
stage pinned code, activate exactly one unit, health-check and roll back on failure.
Old code rollback, compatibility testing, retention, restricted forced-command SSH
identity and GitHub Environment required reviewer belong to that separate design.
No arbitrary Codex commands should be accepted. This issue does not arm PR #72/#76
or implement any of those operations. `CURRENT_STATE` was considered but is not
edited because this task explicitly allows only these three new files.
