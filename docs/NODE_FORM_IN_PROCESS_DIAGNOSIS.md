# In-process NodeForm diagnosis: design only, disabled

Reviewed 2026-10-18 against source HEAD
`3feecadcda221b09382bd3f70f52a170879941dd` and the locally installed
FunPayAPI 1.1.0 source. No runtime trigger, environment switch, Telegram action,
worker, or boot request is implemented. Production remains unchanged. This is
the task's safety fallback, not a live diagnosis or a fix for nodes 976/986.

## Why the hook is not safe to add under the requested constraints

`main.main()` initializes the single Account, validates its identity, initializes
SQLite and recovers autobump progress, then enters `_supervise_tasks()`.
Supervision treats unexpected background task completion as critical and stops
the runtime. `_application_loop()` drains the executor before replacing a
runtime. An optional finite probe must therefore never become a critical task.

`NodeFormService.get(node, force=True)` takes the client's existing RLock and
holds it through HTTP and parsing. The same lock serializes Runner updates,
session refresh and bump operations. Running the probe before supervision
delays startup; running it in `asyncio.to_thread` keeps the event loop responsive
but still delays other Account operations. Lock acquisition itself is unbounded.
`asyncio.wait_for` cannot terminate that thread or release its lock; calling it
a total timeout would be incorrect and could also delay shutdown/restart.

The installed `Account.method()` uses `requests.get` with the Account's
`requests_timeout` (default 10 seconds). There is no per-call timeout or
`allow_redirects` argument. Requests follows GET redirects by default, so a
single `get()` invocation does **not** guarantee one HTTP request to only the
selected endpoint. Final URL validation happens after redirects. The timeout
bounds transport inactivity, not total duration (including DNS, redirects,
slow response bodies, lock wait or parsing). Changing the shared Account timeout
or monkeypatching requests would change runtime behavior beyond this task.

Force refresh also invalidates an existing schema generation and populates the
ordinary memory cache with visible technical metadata on success. A concurrent
wizard could be affected. The schema is not logged or written to SQLite by the
adapter, but cache behavior must be explicitly reviewed before adding a probe.

These are concrete lifecycle/heartbeat and request-budget blockers. No parser
rules, safety gates, settings, claims, schedules or recovery behavior were changed.
There is no supported way here to attach a probe to an already running process;
a separate Python command, Account or bot is not an alternative.

## Proposed opt-in contract, pending resolution of those blockers

This section specifies future behavior; the named switch does not exist yet.

1. Use an ephemeral process-only opt-in, for example the exact value
   `FUNPAYFLOW_NODE_FORM_DIAGNOSIS=once-976-986`. Absent, empty, misspelled and
   other values mean OFF and produce zero diagnostic requests and no log line.
   Do not store it in user settings or a credential file. Do not expose arbitrary
   node, endpoint, account, owner, locale or output-path input.
2. Consume the opt-in once per OS process, before dispatch. An internal runtime
   restart must not re-arm it. A failed run is consumed too. Never start a second
   process to trigger it. Removing the opt-in disables subsequent starts; already
   dispatched HTTP cannot be recalled. No Telegram interaction is required.
3. Only after normal Account initialization/identity validation, reuse the exact
   client, Account and RLock. Use `NodeFormService.get(976, force=True)` and
   `get(986, force=True)` once each, without retry. No account refresh, discovery,
   fallback fetch, POST, save, delete or publication is part of the probe.
4. Resolve strict redirect suppression and a supported finite transport budget
   first, without changing dependencies or the poller/bump client globally.
   If the existing API cannot provide this, remain disabled. Define a bounded
   lock admission policy and a reviewed heartbeat budget; skip when busy or
   restarting. Do not abandon a thread holding the Account lock. If zero impact
   on lifecycle/heartbeat remains mandatory, retain this design-only outcome.
5. Treat probe completion/failure as optional, outside critical task supervision.
   Keep normal fatal Runner supervision intact. Cancellation before dispatch
   sends nothing; after dispatch, drain safely before Account replacement. Never
   turn a diagnostic failure into a bot stop, Telegram notice or persistent write.
6. Emit at most one fixed report per selected node, using locally chosen keys
   `976` and `986` only. Success is the fixed reason `OK`; failure uses
   `NodeFormError.diagnostic`, re-sanitized at the output boundary. The only facts
   permitted are the existing bounded integer HTTP/editor/select counts and
   fixed URL category. Unexpected exceptions map to `UNEXPECTED`, without text,
   type names or traceback. Account/owner IDs, URLs, HTML/body/headers, secrets,
   schema fingerprints/generations, technical names/values and chat text must
   never be emitted or saved. Review the existing per-failure warning to avoid
   duplicate reporting; never capture raw journal lines as the result.
7. Resolve cache effects explicitly: no stale-generation invalidation of active
   wizard work and no diagnostic schema persistence. Do not silently change
   normal `force=True` semantics to achieve this.

## Offline evidence and remaining acceptance tests

`tests/test_node_form_probe_feasibility.py` is a **test-only rehearsal**, not an
implementation of the opt-in. It verifies two fixed GET method calls with the
synthetic forms, exact Account/RLock identity and exclusion of a competing lock
user during dispatch. It also verifies transport failures produce only fixed
reports/warnings, do not retry, permit a later ordinary GET, and leave a temporary
SQLite sentinel unchanged when reopened. It inspects the installed transport
contract using a fake requests GET and checks no probe is wired into main.
The sentinel test is adapter-level evidence, not a claim that a running bot
survives a deployed probe. Existing NodeForm diagnostics tests cover hostile
response text, malformed forms, account context and allowlist filtering.

Local validation of this design-only change: targeted NodeForm/Product/Core/
restart/recovery suites passed **222 tests and 16 subtests**. The full `tests`
suite finished with **722 passed, 155 subtests passed, 3 failed**. The failures
were `test_linux_syntax_and_permission_strategy` (Git Bash signal-pipe access
denied), `test_start_reports_second_instance_without_running_production_bot`
and `test_extracted_runtime_windows_and_linux_install_files` (uv cache access
denied). Packaging smoke with a writable cache was blocked by missing offline
`hatchling`; no dependency was downloaded or changed. Compileall and
`git diff --check` passed. Hosted CI and live QA were not run.

Tests ran with the existing `.venv/Scripts/python.exe`, using `pytest.main`
and temporary directories under ignored `.pytest_cache`. As in the preceding
offline audit, this Windows sandbox denied mode-0700 test directories. Only
the test runner process substituted mode 0777 for `os.mkdir(..., 0o700)`;
repository/application permissions code was unchanged. The targeted arguments
were `-q`, a fresh `--basetemp`, and `tests/test_node_form_probe_feasibility.py`,
`tests/test_node_form.py`, `tests/test_node_form_diagnostics.py`,
`tests/test_node_form_owner_diagnostics.py`, `tests/test_node_products.py`,
`tests/test_runtime_core.py`, `tests/test_runtime_restart.py`,
`tests/test_transient_funpay_recovery.py`. The full run used the same wrapper
with `-q`, a separate fresh `--basetemp`, and `tests`.

Before enabling any implementation, add deterministic tests for:

| Area | Required evidence |
| --- | --- |
| Default OFF | Execute extracted main/supervision with fakes; absent/invalid opt-in makes zero diagnostic requests and creates no probe task. |
| One shot | Explicit opt-in yields exactly two GETs to the fixed endpoints, including cache-hit setup; internal restart, re-entry and both failures do not re-arm or retry. |
| Transport | Mock at HTTP boundary, including redirect chains, auth/error responses, timeout, slow-body and unsupported client API; no redirect dispatch, POST or alternate endpoint; budget failure skips safely. |
| Identity/concurrency | Assert Account/RLock object identity at dispatch, busy-lock skip, cancellation before/after dispatch, no second Account/session/Runner and safe executor drain. Use barriers/events, not timing sleeps. |
| Bot continuity | With fake polling, heartbeat, refresh and bump tasks, fail both probes; polling remains alive, no critical notice/restart is requested, gates remain effective and no critical error is masked. |
| Persistence | Seed temporary real stores/settings with manual OFF, schedule, claims, ambiguous/cooldown progress and drafts; compare through reopened stores after success, failure and cancellation. No settings or persistent rows may change. |
| Sanitization | Inject synthetic private markers in body, headers, URL, options, IDs and exception text; capture all application logs and Telegram fakes. Reports contain only fixed local keys/reasons/facts; Telegram receives nothing. |
| Cache | Existing active wizard generations and unrelated scopes survive success/failure; diagnostic metadata is neither persisted nor exposed. |

## Separately authorized rollout, after implementation passes review

No VPS access, backup, deployment, runtime start or live request was performed
for this work. A future operator needs separate authorization for deployment,
consistent backups, restart and the two authenticated GETs. Follow
[PRODUCTION](PRODUCTION.md): verify the actual code/data paths and one active
runtime, preserve settings and SQLite using stopped writers or SQLite backup API,
check rollback compatibility, and keep the old runtime stopped throughout.
Do not recreate `v1.0.0` or replace its assets.

First deploy with opt-in absent and verify normal health. Only after all blockers
above are resolved and a finite run is separately authorized, arrange the
ephemeral opt-in for that same runtime's next approved start, without a parallel
bot/session. Remove the opt-in from the launch configuration immediately after
consumption so a later OS restart cannot repeat it. Collect only the allowlisted
reports; verify heartbeat and unchanged persistent preferences/progress. Disable
by removing the opt-in, or roll back under the approved plan. No owner Telegram
clicks should be necessary. No actual live outcome or root cause is claimed.
