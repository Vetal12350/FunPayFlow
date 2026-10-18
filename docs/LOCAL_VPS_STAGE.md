# Phase 2: STAGING ONLY — plan-only safety fallback

This independent, owner-initiated local Windows CLI previews an immutable source
stage. **Remote staging is not implemented or enabled.** Acknowledging a plan never
executes SSH, installs dependencies, creates directories or launches a bot. It does
not modify the phase 1 status controller, preflight or issue worker and has no issue,
watch, background, autostart, posting or deployment interface.

## Why execution is disabled

Locked dependency installation can execute package build backends, including the
project's backend. A fixed SSH script and `uv --locked` alone cannot confine that
code's writes to a new stage directory when running as root. The existing owner
layout supplies no reviewed sandbox that enforces this boundary. Current main plus
successful CI also does not independently establish human review of every source
and dependency. This implementation therefore uses the task's plan-only fallback;
it does not claim to have solved immutable staging or verified a live service.

An executable implementation needs separate review of isolation, locked installation
and trust evidence. Do not turn this preview into execution by adding shell commands
to its output or interpreting its acknowledgment as permission for a later action.

## Local operation

Use a trusted source checkout, trusted PATH, Windows and Python 3.13+. GitHub CLI
`gh.exe` must already be locally authenticated for the public repository. Reuse the
owner's existing phase 1 configuration, documented in
[LOCAL_VPS_STATUS.md](LOCAL_VPS_STATUS.md):
`%LOCALAPPDATA%\FunPayFlowVpsStatus\config.json`, with exactly `host` and `identity`.
The same sibling controller validates canonical public IPv4, absolute drive-based
Windows identity filename and LOCALAPPDATA outside the source checkout. This CLI
reads only that bounded config file, never the identity's key contents. It does not
create or modify local config or claims. Do not place private config in the checkout.

From an interactive local owner terminal:

```powershell
python -B scripts/vps_stage_controller.py --plan <exact-40-character-lowercase-main-SHA>
```

The angle-bracket text is a placeholder; replace it with the reviewed commit SHA.
No arguments means disabled. Noninteractive stdin automatically declines before
config or GitHub access. There are no host, path, account, command or issue overrides.
The CLI accepts only an exact lowercase SHA, never a branch, shell text or issue body.

Before showing a plan it uses explicit GitHub CLI GET requests to `github.com` for
the fixed public repository. The SHA must equal the current `main` commit exactly.
The latest returned push run for that SHA and branch from the vetted workflow path
`.github/workflows/ci.yml` must be completed/success, named `CI`, and belong to the
same source and head repository. Missing, inaccessible, malformed, mismatching,
incomplete or failed evidence fails closed. All subprocesses use argument lists,
`shell=False`, 45-second timeouts and no retries. Raw stdout, stderr, exceptions,
identity paths, account/environment values and logs are never printed.

The terminal plan displays the exact SHA, configured public host, fixed public origin
and proposed `/root/FunPayFlow-stage-<12-character-sha-prefix>` directory. It requests
the exact typed phrase `PLAN <full-SHA> <public-host>` with no alternate approval or
default. Treat this display as local-only; do not paste host details into reports.
After acknowledgment, main and CI are checked again. Success means **plan
acknowledged, nothing staged**. It is not proof of human review, branch protection,
remote availability, filesystem state, service health or future HEAD freshness.

## Required boundary for a future staging executor

Execution remains blocked until a reviewed design can enforce all of these:

- Trusted OpenSSH executable and independently enrolled host key; strict host-key
  checking, batch mode, no forwarding/proxy, explicit timeouts and `shell=False`.
  A fixed reviewed stdin script accepts only the validated SHA.
- Fresh interactive owner approval for the exact current reviewed main SHA and
  configured public host, with vetted CI evidence. No issue grants authorization.
- Exclusive creation of the fixed new stage directory; any existing file, directory
  or symlink refuses execution. Clone only the fixed public origin, detach at the
  exact SHA, install locked dependencies using existing `/root/.local/bin/uv` with
  enforced stage-only writes, compile source without importing runtime, verify HEAD.
- Before and after checks establish active main service and unchanged original PID
  and WorkingDirectory. No stop/start/restart, second bot, production credentials,
  settings/data/unit writes, backups, archives, database operations or runtime imports.
- Failures and timeouts report only fixed limited categories, never raw remote
  output. UNKNOWN is not retried. Partial stages stay untouched for manual review;
  no recursive cleanup. Immutability and safe isolation need enforcement, not labels.

No SSH executable is needed for this preview because it never attempts SSH. Existing
stage refusal, SSH failure/timeout handling, remote status and success verification
are **unimplemented execution gates**, not checks passed by this preview. In
particular an acknowledged plan permits no mutation anywhere on the VPS.

Production deployment is a separate **Phase 3**, requiring a new interactive owner
approval, reviewed rollback and production checks in [PRODUCTION.md](PRODUCTION.md).
Neither phase 1 issues nor a phase 2 plan authorize it.

## Offline verification

```powershell
python -B tests/test_vps_stage_controller.py
python -B tests/test_vps_status_controller.py
# If pytest is already installed:
python -B -m pytest -q -p no:cacheprovider tests/test_vps_stage_controller.py tests/test_vps_status_controller.py
```

Tests fake subprocesses, config IO, terminal and approval input; no network, SSH,
credentials or runtime imports are used and no test files are created. They exercise
input/config injection, missing/stale main or CI evidence, noninteractive decline,
exact acknowledgment, post-acknowledgment changes, sanitized errors/timeouts and the
absence of any SSH/execution path. Full Windows/Linux CI and Product/Core QA remain
required PR checks for the trusted worker. No test command here authorizes VPS access.
`CURRENT_STATE` stays unchanged to preserve the explicit three-new-file scope.
