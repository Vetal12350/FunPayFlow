# Windows Control Center · UX 2.0, phase 1

The primary workflow is **ChatGPT → GitHub Issue → existing local Codex Worker →
Windows monitor**. Tell ChatGPT the requested source change. Its separately authorized
GitHub connector must create an owner-authored `[Codex] ...` issue with `codex-local`
in `Vetal12350/FunPayFlow`. Until GitHub actually contains the issue, it is **not queued**.
The monitor neither creates tasks nor receives a private socket/event feed from ChatGPT.
It cannot see ChatGPT thinking, tools, or edits before the issue reaches GitHub.

## One-click Windows launch

Prerequisites: Windows desktop, Python 3.13 with Tkinter, and the existing Git/gh/Codex/uv
worker installation described in [LOCAL_CODEX_WORKER.md](LOCAL_CODEX_WORKER.md).
Use existing local authentication; no API key or bot token is requested by this app.
After the source change has been reviewed and installed through the owner's normal
source update process, double-click `scripts/start_control_center.bat`.
This source implementation does not update an installation or start anything during tests.

Phase 1 provides a Russian dashboard with a compact health strip, a current/recent task
card, separate Windows CI, Linux CI, Product QA and Core QA indicators, and a scrollable
history of at most 100 tasks. Select a row to inspect its checks and click **Открыть GitHub**.
An empty dashboard explains that tasks originate in ChatGPT. The UI uses stdlib Tk/ttk,
11 pt text, a neutral light palette and a restrained accent; no theme/font assets are installed.
Heartbeat age and the last phase are separate. Only the freshly observed current task
is described as executing; historical task phases explicitly say execution is unconfirmed.
A merged/closed PR takes precedence over the worker's historical phase.

While heartbeat is fresh and active, **Worker запущен** is disabled. A fresh heartbeat
also relegates any earlier launch notice to history. When health is unknown/stale,
**Запустить Worker** still performs the existing fail-closed lock/process inventory checks.
Ambiguous safety results say **Сначала проверьте уже запущенный Worker**; stale telemetry
never proves that no process exists. Opening the window never starts a worker.
The start action launches only the
fixed `%LOCALAPPDATA%\FunPayFlowCodexWorker\source\scripts\codex_issue_worker.py`
with `--watch --interval 90`, using the current Python 3.13 interpreter and `shell=False`.
It performs no pull or installation. The installed worker must exactly match the worker
source shipped beside this GUI; an older/different installation is refused.
The existing `start_codex_worker.ps1` remains available for separately authorized use;
it performs its existing source pull and uses the same new worker lock after updating.

Starting the worker enables its existing issue → isolated Codex → branch/PR workflow,
including its existing GitHub writes. The monitor's own GitHub queries are read-only.
Installing/reviewing this code is separate from authorization to run that workflow.
Closing the monitor leaves an already running worker alone. There is no stop/kill button.
A worker launched elsewhere is observed through exactly the same status file.

## Observed phases and health

The trusted worker atomically replaces `live_status.json` in its fixed private worker
folder. This is a separate public telemetry protocol, not bot runtime data. The GUI
never reads `state.json`, reports, prompts, Codex output, `.env`, SQLite, or bot data.
Existing `state.json` format, mark-before-execution deduplication, job isolation,
allowed paths, approval/sandbox flags, and environment filtering remain in effect.

Phases are `waiting`, `claimed`, `clone`, `dependencies`, `codex_running`, `branch_pr`,
`pr_created`, `blocked`, `unavailable`, `stopped`. A phase marks entry to a fixed step,
not successful completion of that step. A PR URL appears only after PR creation returns
an allowlisted repository URL. A subsequent issue comment/close failure can mark the
worker blocked while retaining the already-created PR link. No automatic retry is added.

Telemetry contains protocol version, numeric issue ID, enum phase, phase timestamp,
phase start time, heartbeat, up to 100 jobs, and the last 30 sanitized phase events.
Failures expose only the fixed category `task_failed`, never raw exception text.
Heartbeat is written every five seconds, including during the bounded Codex call.
Local observations refresh once per second independently of GitHub requests. More than
30 seconds without a heartbeat, or a future heartbeat, means **stale / health unknown**.
The UI shows the *last recorded phase* for stale workers, not an ever-growing
"phase elapsed" timer that could be mistaken for active execution.
A fresh heartbeat proves a recent observation, not that the worker cannot subsequently crash.
The job's last observed phase remains visible after a crash; it is not proof of continued work.
Missing/incompatible status means unknown, never proof that an external worker is dead.

Phase 1 deliberately displays **Codex работает**, with heartbeat and freshly observed phase elapsed time.
It does not parse JSONL, infer editing/test activity, expose reasoning or raw logs, or show
percentages. Codex stdout/stderr go to the null device; its existing last-message report
stays outside the GUI. Prompt stdin uses an automatically deleted temporary file
in the isolated checkout, avoiding a potentially blocking Windows pipe write. Codex
has a 1200-second deadline. Timeout/cancellation terminates
the owned child (Windows tree termination via taskkill; POSIX isolated process group for
offline portability) and blocks the task without retry. No arbitrary OS process is killed.

Worker exclusion uses a kernel-held byte-range lock on Windows (`msvcrt.locking`),
with `flock` for offline POSIX tests. Crash releases the lock; leftover lock-file existence
has no meaning. Both GUI launch and worker startup also inspect legacy Python/py worker
processes through fixed, read-only PowerShell/CIM code; only a count is returned.
An existing worker, inaccessible inventory, or lock failure refuses a new launch.
An older worker does not cooperate with the lock: stop/update it through the owner's
normal local procedure before starting this version. The GUI never stops it or treats
stale status as authorization to replace it. A legacy worker launched later by another
program cannot be controlled by this protocol; do not run old and new launchers together.

## GitHub, CI and audit

Background GitHub polling repeats five seconds after the previous bounded refresh.
Every subprocess uses fixed array arguments, `shell=False`, and a timeout. The GUI
allowlists only issue/PR listing and GET Actions endpoints. Issue titles are used only
to identify the `[Codex] ` prefix; bodies and raw logs are never requested/displayed.
The queue display is a candidate queue; the worker still checks the complete owner,
label, prefix, nonempty-body and size policy before claiming an issue.

PR discovery requires both the worker's sanitized PR URL and the matching issue branch.
The fixed `gh pr list -R Vetal12350/FunPayFlow --state all` read-only request includes
`state,mergedAt` alongside number, URL, branch and head SHA. Lifecycle metadata is used
only after the exact URL, branch and SHA shape pass validation. Consistent GitHub
`MERGED` plus a valid `mergedAt` shows **Слито в main**; `CLOSED` with null `mergedAt`
shows **Закрыто без merge**. Missing, malformed or inconsistent lifecycle fields show
**Неизвестно**. An open PR with all four exact checks successful shows
**Ожидает проверки кода**; queued/running checks show **Проверки выполняются**.
Merged status does not assert review, release or deployment. Check indicators remain
independent, so merged PRs can retain four verified green checks. Cached GitHub data
is explicitly marked when refresh is unavailable. No unrestricted gh API was added.
Actions queries bind to the PR's current head SHA, `pull_request` event and exact
workflow path. If the run has a nonempty `pull_requests` association list, it must
name the exact PR. GitHub can return an empty association list after a PR is merged:
only then the monitor requires the exact PR head SHA, matching branch, and both
run repository identities matching this repository. Missing association data or any
mismatch cannot count as success. The newest run/latest attempt is used, with these exact
job names from the current source workflows:

| Workflow path | Required job |
| --- | --- |
| `.github/workflows/ci.yml` | `offline-tests (ubuntu-latest)` |
| `.github/workflows/ci.yml` | `offline-tests (windows-latest)` |
| `.github/workflows/product-qa.yml` | `Owner journeys and regression QA` |
| `.github/workflows/product-qa.yml` | `Runtime and order reliability QA` |

Only all four completed `success` conclusions produce **four checks green**.
Skipped, neutral, absent, renamed, duplicate or incomplete results never count as success.
Failures/cancellation block the check summary. Missing jobs remain waiting (for example,
Product QA path filtering can leave a documentation-only PR without those jobs).
Workflow names are not fuzzy matched. If workflow job names change, update this map
with offline regression tests rather than assuming a new name means success.
GitHub/network/malformed-page errors show unavailable and preserve visibly cached data.

Requests are bounded: listings show at most 100 issues/PRs; each refresh inspects up to
the 10 newest PRs authenticated by local telemetry. Older rows remain waiting/unknown.
Actions pages with more than 100 results or incomplete data fail closed. A slow refresh
can take longer than five seconds; local heartbeat and the start button remain independent.
PRs created by pre-telemetry workers cannot be authenticated by this MVP and remain unknown.
Historical jobs are limited to the 100 retained safe records. Previously observed unfinished
jobs become blocked on worker restart; they are not automatically resumed or requeued
(existing processed-state safety applies).

For an open PR, four green checks means **Ожидает проверки кода**, not reviewed, merged,
released or deployed. A merged/closed PR instead shows its verified GitHub lifecycle state. There is no trusted automated audit-completion signal in the current
pipeline, so this MVP never invents one. Review the diff in GitHub before any separately
authorized merge. This task does not run or alter production workflow #72; it must remain
DISABLED. No workflow configuration is changed by this feature.

## Notifications and boundaries

Non-modal local Windows popups (with a bell and GitHub button) appear on PR creation,
CI/QA failure, four green checks, and worker blocking. They contain only issue/PR numbers,
fixed trusted status text and repository links. Popups are bounded to four visible windows
and expire after 20 seconds; the monitor must be open to receive them. These are Tk desktop
popups, not Windows notification-center/toast history or a background service.

`monitor_notifications.json` persists event IDs before presentation, including the PR
head SHA for failure/green events. Refresh/restart cannot replay an already recorded event.
A crash between persistence and presentation may lose one notification. Corrupt/unwritable
storage disables notifications visibly instead of replaying them. At 10,000 IDs the store
fails closed (no eviction/replay). Run one monitor window per worker folder; cross-window
notification coordination is outside this MVP. No task text, raw output, or credentials
are persisted in the notification file.

There is no web server, paid API, alternate assistant, task-entry UI, mobile notifier,
automatic merge, deployment, owner approval bypass, FunPay/Telegram call or bot token use.
Phone Telegram notifications are future work through a separately authorized dedicated
notifier; existing bot tokens must never be reused. Live ChatGPT internals remain unavailable.

## Offline verification

`tests/test_control_center.py` imports headlessly without creating Tk windows. Tests fake
all gh responses and process startup; `tests/test_codex_issue_worker.py` covers safe phase
snapshots, heartbeat, locking, timeout cleanup, mark-before-execution and restart dedup.
Run with the existing development environment:

```text
uv run --no-sync pytest -q tests/test_control_center.py tests/test_codex_issue_worker.py
```

No real gh issue creation, live Codex, POST, production data or desktop session is needed.
Live Windows presentation, installed-worker launch, authenticated GitHub polling and
actual Windows/Linux CI remain separately verified integration steps, not claims made
by these offline tests. `CURRENT_STATE.md` is intentionally not changed because this
request restricts modifications to `scripts/control_center.py`, `tests/test_control_center.py`
and this document.


## Windows manual UI smoke (after review)

This is a presentation/integration check for the owner in the normal authorized local
installation, not an instruction to change worker state or run production. Do not press
**Запустить Worker** when an existing worker is active or uncertain.

1. Open the monitor with the existing launcher. Confirm no worker starts on opening.
   Keep commands/tasks in ChatGPT; there is no task-entry field in this application.
2. Inspect at 1150×700 and 1366×768, then Windows display scaling 100%, 125% and 150%.
   Confirm readable text, wrapping in the summary/status strip, four individual check
   indicators, visible bottom buttons, keyboard row selection and history scrolling.
   Scroll the history and wait for refresh: the view should keep its position.
3. With a fresh active heartbeat, confirm **Worker запущен** is disabled, heartbeat age
   refreshes, and any previous launch failure is only a historical notice. Closing the
   monitor must leave the worker running. Reopening must not launch another worker.
4. Inspect issue #79 / PR #80 when the trusted GitHub response actually reports merged:
   expect **Слито в main** and each verified successful check green. An open four-green
   PR instead says **Ожидает проверки кода**. A closed unmerged PR says
   **Закрыто без merge**. Selecting an older task must not imply current execution.
5. Use synthetic offline fixtures for stale/future/missing heartbeat, unreadable PR fields,
   GitHub outage and an empty history; do not interrupt the live worker to manufacture
   these cases. Expect unknown health, last recorded phase without elapsed timer,
   explicit cached/unknown GitHub data, and explanatory empty-state text.
6. Verify existing local **Tk popups** for PR creation, CI/QA failure, four-green and blocked
   events using fixtures. They remain bounded, expire and deduplicate across restarts.
   They are not native Windows notification-center/toast notifications.

Native Windows toasts, tray mode, auto-start and app packaging are future **separate
scoped tasks**, after this phase's UI is checked. None is implemented or enabled here.
Windows/Linux CI, Product/Core QA, and ChatGPT security/UI review are subsequent gates;
offline tests alone do not claim those external reviews or a desktop smoke PASS.
