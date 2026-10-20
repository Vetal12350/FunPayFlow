# Offline VPS backup readiness

This change implements an opt-in local archive verifier and a future backup plan.
It does not create backups, connect to a VPS, deploy, invoke subprocesses, contact
FunPay/Telegram, or change services. No bot modules are imported. Review the helper
before an owner-approved future VPS run, from the reviewed source checkout:

```text
python3 scripts/vps_backup_audit.py audit
```

This is the only CLI operation. There are no path arguments, environment path
overrides, backup/deploy modes or service commands. Default locations are the
historical `/root/backups` and `/root/FunPayFlow-data`. The latter is only a temp
containment boundary: live data files are never opened. Tests inject synthetic
roots through the Python API. Do not run the helper against production as part of
source review or CI.

## What the historical findings establish

Owner findings dated 2026-10-20 describe one active `funpayflow.service` runtime,
an inactive legacy unit and actual code in immutable `/root/FunPayFlow-stage-*`
at commit `5e605a90...`. `/root/FunPayFlow-next` is older and not running. These
are supplied historical observations, not independently rechecked here.

Five archive candidates were observed. Three recent small archives lacked the
three required members. One roughly 29.3 MB archive, about 6.7 days old, had member
names for SQLite, settings and environment. An older roughly 9.7 MB archive had
settings/environment but lacked SQLite. Member names and age cannot establish
compression integrity, SQLite validity, WAL completeness, cross-file consistency,
recoverability, code compatibility, encryption or a full restore. None of those
backups can yet be counted as a verified restorable backup. The historical roughly
20 GiB free-space observation is not a current capacity check.

## Audit contract and limits

No source/archive/data writes occur. The necessary exception to a literally
write-free audit is one private temporary SQLite copy: only a known member is
streamed there, checked and deleted in a finally-backed context, including on
ordinary failures. Abrupt process termination or power loss can leave temporary
private data; the owner must arrange private temporary storage and cleanup.
Default temporary storage is `/tmp`, unrelated to backup/data
trees. It must have capacity for 64 MiB plus SQLite checking overhead. No other
archive member is written. OS access-time updates on reads are possible.

Supported inputs are strict UTF-8 ustar `.tar.gz` and single-disk `.zip` using
stored/deflated members. gzip CRC/length, tar header checksum, ZIP CRC and archive
termination are checked. TAR requires two zero end blocks and zero-only trailing
padding. GNU/PAX extensions, ZIP64, ZIP comments/extra fields, encrypted ZIP,
multi-disk ZIP and other compression methods are rejected. `.tar.zst` and `.bak`
are always `unsupported` in this stdlib-only implementation; they are never
guessed as gzip. Other suffixes count as unknown entries. Conservative rejection
does not prove an archive is corrupt: it may require a separately reviewed tool.

Limits are fixed: 128 directory entries, 2048 members, 512 characters per member
path, 64 MiB archive, 64 MiB per member, 256 MiB expanded stream (including TAR
headers/padding), ZIP expansion ratio at most 200. ZIP central directory allocation
is bounded before opening it. Streaming uses at most 64 KiB reads. Nested archive
suffixes and common archive magic are rejected; there is no recursion or execution
of member data. Unrecognized embedded encodings are inert bounded bytes, never
decoded. This is a conservative format verifier, not a general malware scanner.

Absolute paths, drive paths, backslashes, traversal, empty/dot path segments,
control characters, duplicate normalized names, duplicate required components
under different prefixes, links and special files are rejected. Archive inputs
must be regular single-link files. On POSIX they must belong to the effective user
and have no group/other permissions; the backup directory must likewise be private.
Directory ancestors must be real directories owned by root/the effective user,
without group/other writes except sticky directories. Linked ancestors are refused.
File identity/size/timestamps are checked before/after; same-user malicious writers
are outside this protection boundary. Do not run alongside an untrusted writer.
Windows tests exercise parsing; POSIX ownership/mode checks do not establish
Windows ACL safety. The intended owner execution platform is Linux.

Only these exact member layouts identify the required three files:

| Prefix | Required leaves |
| --- | --- |
| empty | `.env`, `bot_settings.json`, `state.sqlite3` |
| `FunPayFlow-data/` | the same three leaves |
| `root/FunPayFlow-data/` | the same three leaves |

Other paths cannot qualify by basename. Settings/env contents are consumed only
for archive validation, never parsed or printed. The SQLite copy has a private
0700 directory and 0600 file on POSIX. It opens with `mode=ro&immutable=1`, disables
extensions, uses `query_only` and untrusted schema, and runs `quick_check(1)` and
`integrity_check(1)` with a shared maximum of 20 million VM steps. Over-budget or
invalid DBs fail closed. An immutable standalone copy does not replay archived WAL;
a passing check cannot show that uncheckpointed transactions were included.

Output is one JSON object with fixed keys, booleans, bounded counters and integer
bands; no filenames, private paths, member names, exception text, schema, records,
credentials or traceback. The `results` array follows enumeration order, with no
stable archive identifier. `size_band`: 0 unknown, 1 under 1 MiB, 2 under 32 MiB,
3 up to the cap (also used for rejected oversize). `age_band`: 0 unknown, 1 under
one day, 2 under seven days, 3 under thirty days, 4 older. Future timestamps fall
in band 1 and do not prove freshness. `unsupported` and `rejected` are separate
booleans. Presence flags are reported only after complete archive validation.
`verified` means supported archive + all three known members + both SQLite checks;
it does not validate settings semantics or all persistent state.
`restore_verified` is **always false**. `audit_complete` describes enumeration,
not backup readiness. Exit 0 requires a nonempty audit where every candidate is
verified and there are no unknown entries; exit 1 includes incomplete/unverified
results, and exit 2 rejects arguments. No result authorizes deployment.

## Future backup sequence — plan only, not executable

Each numbered gate must pass before the next. A future separately reviewed helper
should make these checks automatic to minimize owner commands, with one explicit
owner approval for its exact scope. This PR implements none of that executor.

1. Reconfirm owner-approved host, actual unit, immutable running commit, process
   count, effective service identity and actual data locations using redacted
   metadata. Do not infer them from the inactive next checkout. Preserve SAFE_MODE,
   late action gates and UNKNOWN request semantics; never replay uncertain remote
   operations as a recovery test.
2. Inventory **all** persistent configuration/state/progress: SQLite (including
   autobump progress and critical backlog), `.env`, `bot_settings.json`, legacy
   `stats_log.json`, imports and any persistent runtime assets. Decide explicitly
   which logs/history must be retained; locks are recreated, not restored as live
   locks. Record required/optional classification privately. Missing or unreadable
   required files, unreadable directories, symlinks/hardlinks/special files,
   wrong owner, permissive modes/ACLs or an unknown state location block creation.
   Do not silently exclude progress or secrets needed for recovery.
3. Require controlled source ancestors, service ownership, private destination
   ownership, 0700 staging directories and 0600 files (umask 077), with no public
   ACLs. Use no-follow descriptor-relative opens, exclusive files, fstat identities
   and single-link checks. Verify writability using only private staging, outside
   the data tree and outside source checkout. Recheck capacity for SQLite copy,
   all assets, archive, isolated recovery copy, overhead and an explicit reserve;
   insufficient/unknown capacity blocks. Do not assume compression or historical
   free space. Bound bytes, entries, duration and disk use; abort on growth.
4. Establish a genuine writer barrier covering **all** settings/progress writers,
   including external editors. Stable stat readings alone cannot prove cross-file
   consistency. If safe quiescence cannot be demonstrated, require a separately
   approved bounded service stop at actual deploy time, including timeout, verified
   process exit and a preapproved recovery/resume procedure. No silent stop/restart;
   if stop approval is absent, abort. Detect unexpected writers and abort. A lock
   in one data directory is not proof that another installation is inactive.
5. While the consistency barrier holds, use `sqlite3.Connection.backup` from a
   read-only live source connection to a new private staged database. This is
   WAL-safe and includes committed WAL transactions; never raw-copy only the live
   main DB. Apply bounded backup progress/deadline handling, fail on busy timeout
   or storage failure, and close both connections. Copy remaining inventory through
   verified descriptors, rejecting size/identity/mtime/ctime changes before/after
   each copy and rechecking the entire inventory for added/deleted/replaced files.
   Online SQLite backup alone guarantees no cross-file snapshot. Any race invalidates
   the entire attempt; discard only this attempt's private staging, retain previous
   backups and require a new controlled attempt. Do not retry remote actions.
6. With the staged snapshot immutable to writers, validate SQLite and settings
   structure privately, inventory completeness, file permissions/ownership, byte
   counts and content digests. Build a bounded archive and private metadata manifest
   identifying runtime commit/schema/inventory and digests without publishing any
   private names/values. Independently read the entire new archive to check metadata,
   checksums and limits. Manifest/hashes remain private; they are not encryption or
   authenticity against an attacker who can rewrite both files.
7. Perform isolated recovery validation in a separate private offline directory:
   restore every required member with a reviewed safe restorer, verify digests,
   SQLite quick/integrity checks, configuration parsing and compatibility with the
   captured immutable code and intended rollback code. Use fake network adapters,
   no real credentials in active clients, no FunPay/Telegram calls, no order/event
   replay, and no service start. Verify all progress and settings survive, including
   UNKNOWN records. Parsing an archive is not this full restore test. Any failure
   prevents publication and activation.
8. Only after all gates pass, fsync files and staging directories and atomically
   publish the complete immutable backup set on the same filesystem; fsync its
   parent. Never replace a previously verified backup in place. Apply a private
   bounded retention policy only after new validation: preserve last known good
   copies and owner-required rollback ages; if capacity conflicts, fail closed
   instead of deleting the only usable backup. Define off-host encrypted retention
   and recovery-key custody in a separate owner-approved task; local plaintext
   archives are not encrypted or protection against VPS loss. Confirm service
   restoration only through the separately approved stop/resume procedure.

Failure at any gate means **no published baseline and no deployment**. Never put
data, credentials, archive payloads, manifests, recovery copies or private logs in
issue comments, GitHub Actions logs, ChatGPT, or test artifacts. Only synthetic
fixtures belong in source. Review and independent ChatGPT audit before merge,
plus owner approval of future production operations, remain separate gates.

## Offline verification

Use installed dependencies, no syncing or remote execution:

Create `tests/.offline-tmp/` first if it is absent.

```text
python -B -m pytest -q -p no:cacheprovider --basetemp=tests/.offline-tmp/backup-audit tests/test_vps_backup_audit.py
```

Set `PYTHONDONTWRITEBYTECODE=1` and `TMP`/`TEMP`/`TMPDIR` to a directory under
`tests/.offline-tmp/` for broader existing offline tests that use tempfile directly.
Tests create fake env text and tiny SQLite databases only. Product/Core QA and
Windows/Linux CI must be reported from actual runs; this document claims no CI
results. The new document carries this scoped feature's state; CURRENT_STATE is
intentionally unchanged because the task permits only three new files.
