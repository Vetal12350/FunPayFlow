# Unprivileged staging isolation contract — offline planning only

`scripts/vps_unprivileged_stage.py` implements the permitted **read-only planning
fallback**, not a working staging executor. `stage(sha)` accepts only an exact
lowercase 40-hex string, checks Linux real/effective/saved UID/GID, rejects root,
saved root, root groups and set-id contexts, then returns
`NOT_STAGED_ISOLATION_REQUIRED`. Every CLI invocation returns `REJECTED_COMMAND`
with nonzero exit. No subprocess, filesystem lookup, network, application import,
service operation or cleanup is implemented. In particular no production path is
read, even for metadata. The existing staging and deploy executors stay DISABLED.

`validate_plan` is a pure contract validator for explicitly mocked observations.
Its positive result is `PLAN_VALIDATED_NOT_STAGED`, never successful staging.
There is no observation collector, input-file parser, environment switch, or
connection from this validator to execution. **Caller-supplied facts cannot prove
confinement.** Mock success tests prove contract consistency only. Permission,
timeout and unknown command outcomes are modeled rejections, not tests of a live
subprocess runner. Missing confinement is a real execution blocker.

## Executable and non-executable boundaries

| Component | Present behavior | Future trusted responsibility |
| --- | --- | --- |
| Source checkout worker | Identity check, refusal, pure command/environment proposals and plan validation | Must remain unprivileged; never service/data authority |
| Privileged forced-command gate | **Not implemented or installed** | Root-owned code outside checkout; exact request grammar and origin/main verification; deployment lock; runtime identity snapshots; no repository/backend execution as root |
| Sandbox launcher and verifier | **Not implemented** | Establish and authenticate the kernel-enforced boundary below before any Git/uv invocation |
| Command runner | **Not implemented** | Execute only reviewed arrays using `shell=False`, exact-output checks, bounded output/time/process-tree resources and interleaved rechecks |
| Activation, backup, health and rollback | **Not implemented** | Separate authorized work and security review |

The checkout, Git objects, project configuration, wheel/build backends and their
children are untrusted code. `uv sync --locked` can execute such code. Environment
sanitization, a non-root UID, a path prefix, namespace ID, mount-table snapshot or
an `ISOLATED=1` flag alone does not demonstrate confinement. This scope cannot
authenticate the origin and lifetime of a sandbox, deny every inherited handle,
or prove there are no writable aliases/private-data mounts. Consequently it does
not run proposed commands. Running the source file itself as root is not a safe
bootstrap: even Python imports occur before `stage()` can refuse root.

## Required bootstrap before execution can be implemented

A future independently reviewed root-owned launcher must use a trusted isolated
interpreter (`-I`) and trusted installed worker, not import code from a writable
checkout. It must create a dedicated restricted `funpayflow-staging` account with
no sudo, production groups, credentials, login agent, service socket or data
access, then drop all real/effective/saved IDs, capabilities and supplementary
groups before executing any repository/dependency code. UID/GID and Unix ownership
must be verified from the kernel/account database, not inferred from usernames.
The worker's current UID/GID equality check is only a prerequisite, not this proof.

The launcher must establish a read-only filesystem namespace with **only the new
full-SHA stage writable**, including private temp/home/cache. It must hide private
data, host process information, host sockets, home directories and alternate mount
aliases entirely; read-only private data is insufficient. Close inherited file
descriptors, deny privilege gain, set restrictive umask, limit descendants and
resources, and prevent mount/namespace escape. Network policy must deny
FunPay/Telegram and arbitrary outbound traffic; only the fixed public GitHub
fetch needs future controlled egress. Build/sync are proposed offline. HTTPS Git
transport must be audited to prohibit push/POST; these proposals are not a network
policy. No network operations are performed in this issue.

The verifier must authenticate an externally established, per-invocation boundary
(for example a trusted launcher channel with peer identity and kernel checks tied
to the process, namespace and exclusive stage inode), revalidate its continued
enforcement, and reject unknown evidence. A user-editable manifest, boolean,
environment variable or mock fact dictionary is never sufficient. This verifier,
its protocol and OS-specific integration tests are **incomplete gates**.

## Fixed proposed layout and command plan

```text
/var/lib/funpayflow-staging/                 root:root 0755, no worker writes
  <exact-full-40-hex-sha>/                  staging account 0700, exclusive
    repo/                                 detached source and private .venv
    home/                                 private HOME and config
    tmp/                                  private temporary files
    cache/uv/                             private, preseeded offline cache
    artifacts/                            wheel output; never activated
```

`/`, `/var`, `/var/lib` and the staging base are proposed root-owned 0755
directories. The root-owned launcher must exclusively reserve each stage under a
lock using descriptor-relative no-follow operations and verified ownership/modes
for every component. Refuse any pre-existing directory, partial stage or symlink;
never resolve/follow symlinks to reuse paths. No automatic removal or retry into a
partial stage is permitted. Symlinks created by repository/build code also require
no-follow checks before subsequent work; the sandbox must contain races and
symlink escapes regardless of path checks. Output is untrusted, never privileged
code to import or activate. Minimum modeled capacity is 2 GiB and 10,000 free
inodes; the launcher must impose quotas and check capacity again during work.

`proposed_commands(sha)` describes fixed executable paths `/usr/bin/git`,
`/usr/bin/uv`, `/usr/bin/python3.13`, argument arrays, 30–600 second timeouts and
exact stdout predicates. Tool versions, ownership, executable integrity and absence
of writable ancestors must be verified by the future bootstrap. These paths are
proposed prerequisites, not claims about tools installed on any VPS.

The proposed order is fresh Git init with empty template and disabled hooks,
fixed public origin `https://github.com/Vetal12350/FunPayFlow.git` main lookup,
fetch only main without tags/submodules, exact fetched commit check, detached
checkout of requested SHA, exact HEAD and clean-status checks, production-only
`uv sync --locked --no-dev --no-default-groups --no-editable --offline`, isolated
Python compileall, offline wheel build, then another exact main lookup. Empty,
duplicate, mismatched or ambiguous main output must fail closed. There is no
caller-selected ref, origin, executable, path, flag or command. No runtime module
is imported or started for the compile/package checks.

`proposed_environment` builds a fresh allowlist without reading or merging the
parent environment. All incoming Git/Python/uv/loader/SSH/proxy overrides and
application data settings are discarded. Only fixed Git config controls and
stage-local uv/temp/cache values are supplied. This does not undo loader/Python
injection that happened before startup: the trusted launcher must also sanitize
the environment before starting the interpreter. No shell is used.

The future runner must enforce each predicate immediately, including detached
HEAD (not just matching commit), inspect the checkout for unsafe links before
builds, validate expected package contents without importing runtime code, and
check unchanged tracked source/lock and identity again after untrusted builds.
`command_results` in the validator represents those completed checks, not merely
zero exit codes. It is deliberately synthetic. The sync lock does not by itself
pin every PEP 517 build dependency. A separately reviewed cache-seeding and build
dependency provenance policy is needed; absent dependencies must fail offline,
never trigger an online fallback or lock update. No shared writable cache.

## Runtime identity, errors and future production work

The future trusted root gate, outside the worker's namespace, owns main/PID
rechecks under a deployment lock. It must freshly verify upstream main before and
after staging; snapshot both units, exactly one runtime, PID plus start ticks
(PID reuse), code cwd/inode, commit, WorkingDirectory and restart count before and
after, **including failure, timeout and interruption**. The mock runtime tuple
models PID/start ticks/code inode/runner count/restarts; it does not collect these
or prove a real service is unchanged. Missing, ambiguous or changed identity means
failure, without retrying, activating code or restarting a service. The worker
must never receive runtime secrets or inspect production processes/data itself.

All externally visible results must be fixed allowlisted statuses. Do not output
exception bodies, subprocess stdout/stderr, original commands, environment, paths
supplied by an untrusted party or private logs. A future timeout handler must kill
and reap the entire sandbox process tree; simply timing out the parent process
is insufficient. Keep partial stages for separately authorized investigation.

[PRODUCTION.md](PRODUCTION.md) records the historical owner-specific layout:
code `/root/FunPayFlow-next`, private data `/root/FunPayFlow-data`, old installation
`/root/SecureBot`, and old/new systemd units. None is accessed or changed here.
Moving code/data out of that legacy root layout, establishing distinct ownership
and safely migrating service paths is **future work**, requiring explicit approval,
backups and compatibility review. This document does not prescribe a live move.

Backup consistency, atomic activation, health probing, one-runtime enforcement and
rollback/schema compatibility require separate implementation and authorization.
No SSH, VPS setup, GitHub Environment/secrets/variables, deployment or rollback is
included. Windows and Linux CI, Product/Core QA and human/ChatGPT security review
are required before merge; local mock results do not claim those approvals.
`CURRENT_STATE` is intentionally unchanged under this task's three-file scope.

Offline regression command (existing environment, no dependency sync):

```text
python -m pytest -q tests/test_vps_unprivileged_stage.py
```
