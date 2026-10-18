# Runtime/recovery audit — phase 1

Date: 2026-10-18. Source baseline: `453acee9995549d6ba5489ea3613ecab1097e6ed`
(`codex/issue-46`, equal to the checkout's `origin/main`). Remote freshness is
UNVERIFIED: no fetch or remote operations were performed. Initial worktree clean.

## Risk matrix and deterministic scenarios

This matrix was selected before adding scenarios. All data is synthetic; clocks,
accounts, polling and Telegram are fake/mocked. SQLite files are temporary.
Priority reflects consequence, not a claim that a defect exists.

| Risk / priority | Existing evidence | Additional scenario / invariant |
| --- | --- | --- |
| Crash during snapshot/claim/result persistence — critical | Interrupted committed claim and result-save failure already tested | Abort the second snapshot insert inside SQLite; reopen and verify rollback, then resume successfully |
| Timeout after dispatch / UNKNOWN — critical | ReadTimeout, TLS, connection failures; no immediate retry; independent polling recovery | Reopen after UNKNOWN and repeat recovery; deadline must remain stable, with no send before it |
| Concurrent workers / stale claims — critical | Atomic conditional claim exists; little direct contention coverage | Two store instances contend using a barrier, one winner; expired claim cannot be stolen; old attempt cannot release/finish a newer claim |
| Restart loses progress/cooldown — high | Partial snapshot reorder and cooldown restart tests | Preserve 121 confirmed synthetic targets, recover an interrupted next target and continue only remaining pending work |
| Overnight 07:00–04:00 Europe/Moscow — high | Pure policy and daytime boundary integration tests | Persist/reload policy, test UTC instants around midnight, 04:00 and 07:00; closing boundary after claim must release without dispatch |
| Manual OFF / SAFE_MODE / network backoff — critical | Saved OFF and late cancellation already tested | Exercise worker outer and late gates for each blocker without changing saved preference/progress |
| Missing tokens — high | Startup validation precedes Account creation | AST-extracted startup with each required variable absent from a synthetic environment must exit before any client/Bot/store factory is called |
| Polling/supervision recovery — critical | Capped indefinite backoff, fatal/auth/certificate stop, queue durability, executor drain | Run the six requested regression modules; preserve their assertions |

## Results

Baseline requested modules: **PASS**, 65 tests and 40 subtests. Final run of those
six modules plus `test_autobump_schedule.py`: **PASS**, 115 tests and 58 subtests
(6.14 seconds, no skips). Nine new test methods add 18 subtests; existing
assertions remain unchanged. Source compilation of the seven modules: **PASS**.
Final diff review, `git diff --check`, and separate whitespace/local-link checks
for this untracked report: **PASS**. Deliverables: three test files and this report.

| New deterministic scenario | Test method | Result |
| --- | --- | --- |
| Partial snapshot insert rolls back, reopen/retry succeeds | `ResumeTests.test_sqlite_snapshot_abort_rolls_back_before_retry` | PASS |
| Real SQLite claim failure blocks simulated dispatch, reopen retains pending row | `ResumeTests.test_sqlite_claim_abort_blocks_dispatch_and_survives_reopen` | PASS |
| Two SQLite workers share one cycle and one winning claim; stale attempts cannot mutate replacement | `ResumeTests.test_competing_store_claims_and_stale_attempts_are_fenced` | PASS |
| 121 saved successes remain byte-for-field equivalent except cleared cycle/position; interrupted next target deferred | `ResumeTests.test_121_saved_successes_survive_interrupted_next_target` | PASS |
| Repeated UNKNOWN recovery does not extend an already ambiguous deadline; only normal interval permits another attempt | `ResumeTests.test_unknown_repeated_recovery_preserves_deadline_until_exact_boundary` | PASS |
| SAFE_MODE, OFF, restart and backoff block worker entry and late callback without writing settings | `LoopPolicyTests.test_outer_and_late_safety_gates_preserve_user_preference` | PASS |
| Saved overnight window survives reload; midnight, exclusive stop and inclusive start agree with MSK | `ScheduleIntegrationTests.test_saved_0700_0400_window_across_midnight_and_restart` | PASS |
| Closing overnight boundary after durable claim releases to pending before simulated HTTP | `ScheduleIntegrationTests.test_overnight_close_after_claim_releases_before_dispatch` | PASS |
| Each missing mandatory variable prevents runtime construction | `AutobumpStartupTests.test_missing_configuration_exits_before_constructing_runtime` | PASS |

No application defect was reproduced by these scenarios. No runtime fix,
migration, retry-policy change or safety-gate change is justified by this run.
Only tests and this report change. `CURRENT_STATE` was reviewed and does not need
an entry: capabilities and runtime boundaries have not changed.

The default Windows sandbox test invocation failed because directories created
with mode `0700` became inaccessible (WinError 5), including temporary SQLite
directories. A synthetic probe reproduced this for `0700`, while `0777` passed.
The successful baseline used the existing `.venv/Scripts/python.exe`, pytest
without its cache provider, a temporary directory under the checkout, and a
process-local `os.mkdir` adapter using `0777`. No application permissions or
dependencies were changed. Default `uv` also could not access its global cache.
These default invocations are **FAIL (environment)**, not evidence of a runtime
regression. The final run additionally blocked real HTTP at both requests and
aiohttp transport entry points; neither guard fired.

Final test invocation (PowerShell, existing environment; no dependency sync):

```powershell
$env:TEMP = Join-Path (Get-Location) 'tests/.runtime-audit-tmp'
$env:TMP = $env:TEMP
# Create this scratch directory first if absent.
@'
import os
from unittest.mock import patch
import aiohttp
import pytest
import requests
mkdir = os.mkdir
def sandbox_mkdir(path, mode=0o777, *, dir_fd=None):
    return mkdir(path, 0o777, dir_fd=dir_fd)
async def deny_async(*args, **kwargs):
    raise AssertionError('Real HTTP forbidden in runtime audit')
modules = ['autobump_resume', 'autobump_startup', 'transient_funpay_recovery',
           'runtime_core', 'runtime_restart', 'autobump_schedule_integration', 'autobump_schedule']
with patch('os.mkdir', sandbox_mkdir), \
     patch.object(requests.Session, 'send', side_effect=AssertionError('Real HTTP forbidden')), \
     patch.object(aiohttp.ClientSession, '_request', deny_async):
    result = pytest.main(['-q', '-p', 'no:cacheprovider', '--tb=short'] +
                        [f'tests/test_{name}.py' for name in modules])
raise SystemExit(result)
'@ | .venv/Scripts/python.exe -
```

Sandbox ACLs prevented cleanup of empty directories created by failed default
runs: `tests/.runtime-audit-tmp/{probe448,tmp9054z7s2,funpay-offline-tests-yjpuhmyy}`
and root `pytest-cache-files-{4p_44nrd,7uilcxej,u_uydk9l,vao1roq6}`. Automatic
approval review rejected recursive cleanup (`blocked by policy`); nonrecursive
removal and permission reset failed for these directories with access denied.
They contain no application/runtime data and are not deliverable source files.

## Residual risks / handoff

- **UNVERIFIED:** latest remote main, Linux execution, full suite and CI QA.
  Trusted worker: when preparing the PR, request full/QA checks from
  [DEVELOPMENT](DEVELOPMENT.md), including `uv run --no-sync pytest -q tests`,
  compilation and existing packaging/install smoke checks. No PR or CI config
  was created/changed here.
- **UNVERIFIED:** actual process kill, physical power loss, disk-full/fsync and
  SQLite corruption. SQL aborts and independent connections exercise transaction
  rollback and durable state boundaries, not hardware durability.
- Store contention does not certify multiple live runtimes/accounts. Production
  remains single-runner; startup recovery must follow shutdown/executor drain.
  A claimed target cannot be stolen merely because its deadline elapsed.
- The 121 successes are synthetic category records, not inspection or proof of
  any private historical dataset. No production data was accessed.
- Cooldowns use wall clock; system-time jumps remain a limitation. UNKNOWN
  deferral prevents blind retry inside the safe interval, not remote exactly-once
  execution. Recovered claims can conservatively skip an unsent target.
- Live FunPay/Telegram behavior and production health are **UNVERIFIED** and
  deliberately outside this audit. No real GET/POST, runner startup, second
  account, deployment, credentials, GitHub changes or publication occurred.
