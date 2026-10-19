# Production deploy design — PLAN VALIDATION ONLY

**Deployment is NOT implemented. Do not install this as a working deploy agent.**
This change takes the task's explicitly permitted read-only plan-validation
fallback. It adds a pure policy validator and a command entrypoint that always
returns nonzero. No GitHub Action, environment, SSH key, forced command, VPS
installation or live deployment has been configured or tested.

See [production boundaries](PRODUCTION.md). Source review does not authorize
access to production. No CI configuration or dependencies change here.

## Owner-reported baseline, not independently verified

As of 2026-10-19 the owner reports exactly one `funpayflow.service`, running
`/root/FunPayFlow-stage-5e605a90` at commit
`5e605a90d64bb9ed977032077d97ac75c154778c`; legacy `funpay-bot.service` is
inactive. Private data is separate at `/root/FunPayFlow-data`, and the unit is
`/etc/systemd/system/funpayflow.service`. Existing backups are
`/root/funpayflow-backup-*`, containing `private-data.tar.gz` and `unit.service`.
The stage has a root-owned `.venv`; uv is `/root/.local/bin/uv`, Python is 3.13.
These statements are not runtime observations collected by this task.

## What the source actually does

`scripts/vps_deploy_executor.py` imports only the standard library. It never
opens files, runs subprocesses, imports the app, reads credentials, connects to a
network, writes data, acquires a lock, stops a service or creates an Account.

The entrypoint accepts no standalone arguments, including `--selfcheck`. It
checks the exact ASCII `SSH_ORIGINAL_COMMAND` grammar `deploy <40 lowercase hex>`
with no leading/trailing whitespace, extra commands or arguments. It rejects
TTYs and additional environment variables; its proposed minimal sshd environment
requires root identity, `/bin/bash`, `/usr/bin:/bin`, and SSH connection metadata.
These environment strings are not authentication or approval evidence. A valid
request prints only `DEPLOY_NOT_IMPLEMENTED` and exits 2. Invalid requests and
handled exceptions/interrupts print only `REQUEST_REJECTED` and exit 2. No input,
environment values, exceptions, private paths, logs or identifiers are echoed.

The library function `validate_plan(command, facts)` checks a strict observation
schema. It rejects wrong upstream/main, forks, unsafe stage names, reused stages,
wrong cwd, duplicate runners, active legacy, health under 20 seconds, restart
increases and unrecognized unit text. The proposed unit must match the narrow
template derived from the tracked systemd template with root as its user; the old
stage must appear exactly twice. Unknown directives, overrides or extra path
instances reject. This template is not asserted to match the live unit.

Required boolean observations include root ownership, absence of symlinks in
every path component, clean verified previous stage, sufficient disk, verified
prerequisites and prior backup, schema compatibility, locked dependencies,
compilation and a held deployment lock. Missing, unknown, incorrectly typed or
unsafe facts reject. **These are caller assertions, not collected evidence.**
There is intentionally no JSON, file, issue-body or CLI interface to supply them.
Even a valid synthetic plan returns only `PLAN_VALIDATED_DEPLOY_DISABLED`.
It cannot authorize an operation or establish that human approval occurred.

## Required work before any operational bootstrap

The full executor needs a separately reviewed implementation and offline failure
tests. Do not enable Step 2 against this placeholder. In particular:

1. Build a trusted Linux collector with fixed paths and absolute tool paths,
   sanitized subprocess environments, timeouts and captured/redacted output.
   Verify its installed root-owned file and every parent against symlinks and
   writable permissions. Reject unit drop-ins, unexpected unit directives,
   unrecognized processes, wrong PID/cwd, legacy activity and dirty previous code.
   Process inspection must cover alternate launch forms and fail closed on
   inaccessible or ambiguous state. Repository code is not an installed agent.
2. Acquire a nonblocking exclusive local lock in a trusted root-owned directory;
   hold it through health checks or rollback. Reject concurrent requests. Resolve
   main afresh from only `https://github.com/Vetal12350/FunPayFlow.git`; require
   the requested exact commit to equal main, rechecking immediately before
   stopping if preparation took time. Reject forks and Git configuration/hook
   injection. Create a new root-owned stage with detached verified HEAD; refuse
   existing stages and symlinks. No caller-selected paths or shell commands.
3. Verify a known-good previous stage, recognized unit with exactly two code path
   instances, baseline PID/restarts/health, free space and prerequisites before
   stopping. Prepare locked dependencies and compile only in the new stage. Root
   package build hooks execute repository/dependency code: their trust and
   isolation need explicit review. Never execute new app code against private
   data during preparation. Establish a reviewed, fail-closed migration policy;
   guessing schema compatibility from filenames or a boolean is insufficient.
4. Snapshot the exact old unit. Stop only `funpayflow.service`, ensure all writers
   exited, run SQLite read-only quick_check with WAL consistency considered, then
   archive the ENTIRE private data directory including hidden files and WAL files.
   Never print its contents or extract into production. Use a fresh backup
   directory mode 0700, archive and unit files mode 0600; validate member names,
   types, traversal/link safety, completeness and archive integrity. Refuse missing
   or corrupted backups. Preserve existing scripts, stages and backups.
5. Only after validated backup, replace exactly the two code paths in the unit
   with an atomic root-owned mode-0644 replacement, daemon-reload and start the
   single service. Check active state, MainPID, cwd, WorkingDirectory, one runner,
   inactive legacy and no restart increase for at least 20 seconds.
6. On every failure after stop (including ambiguous stop/start outcome), restore
   the saved unit, daemon-reload, start OLD and verify active/identity. Preserve
   the validated backup byte-for-byte. Never restore data automatically. Handle
   SIGINT/SIGTERM through rollback where feasible; SIGKILL/power loss needs a
   durable recovery design. Return fixed statuses for rollback success and
   manual-intervention-required failure. Never blindly retry unknown mutations.
   Never delete old stages/backups, use `rm -rf`, or bypass app safety gates.

Until these are implemented, no success/rollback/deployment claims are valid.
Operational git/uv/systemd failures, SQLite/backup corruption, real filesystem
ownership/symlink attacks, flock contention and signal rollback remain untested
execution branches because no execution branches exist in this change.

## Future manual bootstrap and one human approval

Outside this task, the owner must review the completed executor and install an
immutable, root-owned copy outside all checkouts at
`/usr/local/libexec/funpayflow-deploy.py`. Pin its reviewed contents independently
of repository updates. Forced-command invocation must use an absolute trusted
Python interpreter with isolated mode (`-I`) to avoid Python startup injection.
An interpreter and script writable by the service or deploy caller invalidate
the privilege boundary. Root execution is specific to the reported installation,
not a change to public installer defaults.

Step 2 must configure a GitHub production Environment with required OWNER
reviewers, prevent self-review and administrator bypass as appropriate, restrict
deployable branches to protected upstream main and protect workflow changes.
The single human approval happens there before the dedicated key is available.
The server cannot prove GitHub approval from `SSH_ORIGINAL_COMMAND` alone; access
to that key and the reviewed workflow are part of the trust boundary.

Generate a NEW dedicated restricted key outside chat. Never reuse personal
`id_ed25519`, copy private keys into documentation or request secrets in chat.
The authorized_keys entry must enforce the fixed command and disable PTY,
agent/X11/port forwarding, user rc and user-controlled environment (`restrict`
plus applicable explicit restrictions). Disable AcceptEnv/PermitUserEnvironment
and review sshd/PAM environment behavior, forced command, forwarding and SFTP/SCP
rejection end-to-end. Pin the VPS host key in the future workflow. A source file
or an authorized_keys example alone does not establish these protections.

## Offline verification and review handoff

Run from the source checkout, with already available dependencies:

```text
python -m unittest discover -s tests -p test_vps_deploy_executor.py -v
uv run --offline --no-sync pytest -q tests
uv run --offline --no-sync python scripts/qa_smoke.py --suite product
uv run --offline --no-sync python scripts/qa_smoke.py --suite core
git diff --check
git status --short
```

The new tests need only the standard library and run on Windows and Linux; they
use synthetic observations and mock IO/process/network entrypoints to fail on
side effects. They cover valid plan validation, exact command rejection, wrong
SHA/main/origin, missing and unsafe facts (including symlink, backup and lock
assertions), unit path counts, runtime/health checks, CLI/environment rejection,
repeated disabled requests, interruption and fixed output without secrets. They
do not test Linux deployment by pretending mocked facts are real observations.
Existing CI already runs the test suite on both OSes; this task changes no CI.
Local Windows results cannot establish a Linux CI pass.

Local verification for this change (Windows, 2026-10-19):

- Standard-library unittest: 10 passed. Targeted pytest for the new validator,
  staging controller and status controller: 47 passed, 293 subtests passed.
  The existing checkout `.venv` was used without dependency installation.
- Compilation of the two new Python files passed; whitespace checks passed.
- Preflight tests requiring temporary files fail with Windows sandbox
  `PermissionError` on temporary directories. An explicit alternative TEMP
  basetemp also failed. Full-suite execution was interrupted after the same
  widespread failures; it did not pass. Product and core QA were each attempted
  with fail-fast and both failed on temporary-directory access/SQLite setup.
- Linux CI, live systemd/SSH, upstream lookup, backup and rollback were not run.
- Initial pytest cache creation also left two inaccessible generated
  `pytest-cache-files-*` directories in the checkout. Cleanup was blocked by
  policy/access restrictions. They are test artifacts, not deliverable files;
  the trusted worker should inspect and remove them if present.

Worker review description must state: plan-only fallback; deployment disabled;
all operational branches listed above remain unimplemented; owner baseline and
live unit/schema compatibility are unverified; no VPS/GitHub/key configuration.
No PR or issue is created by this task. The three-file limit intentionally leaves
CURRENT_STATE unchanged; this document records the limitation and handoff.
