# Windows Control Center 3.0 · optional CustomTkinter UI

## CI-internal pinned FunPayAPI wheel helper

`scripts/build_pinned_funpayapi_wheel.py` is a source-only preparation helper,
not an updater, installer or VPS tool. Workflow integration is a separate change;
the GUI `Подготовить обновление` remains disabled until a secure installer exists.
Only a disposable **Linux GitHub-hosted runner, non-root**, with **no secrets**,
a least-privilege read-only GitHub token and checkout credential persistence disabled
may run it. CI markers are refusal guards, not a sandbox or proof of those conditions;
the job owner must provision that environment. Third-party build hooks execute code
with the runner user's privileges. Never run this helper on production or a persistent
self-hosted runner. No workflow is added by this change.

Provision `pip`, `setuptools` and `wheel` in the CI Python environment beforehand
(these are CI tools, not new project dependencies). Run
`python scripts/build_pinned_funpayapi_wheel.py` there. The sole optional positional
argument is an existing empty absolute output directory under `RUNNER_TEMP`; without
it, the helper creates `RUNNER_TEMP/funpayapi-wheel` and refuses nonempty output.
It checks the fixed URL/size/SHA-256 against checkout `uv.lock`, performs one bounded
HTTPS GET without auth, proxies or redirects, verifies bytes before safe extraction,
then uses pip's conventional setuptools backend without indexes, dependency installs
or build isolation. Unsupported backends or missing build tools fail closed. Hooks
receive a minimal environment and temporary HOME; their raw logs are suppressed.

Only one validated wheel and `manifest.json` are published to that output directory.
The manifest records the exact source and wheel SHA-256, built filename, package,
version and validated WHEEL tags matching the filename. It is neither an attestation
nor a claim of bit-for-bit reproducibility. Archive, metadata and write-boundary
regressions use synthetic inputs and mocked HTTPS/subprocess only, in
`tests/test_build_pinned_funpayapi_wheel.py`. No real source download or wheel build
is performed by these tests; the future disposable CI job must establish real build
compatibility before anyone relies on its artifact.

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
The existing installed Tkinter interface stays in place until the owner explicitly
upgrades through the normal reviewed source update process. This checkout does not
update that installation. Do not copy a new launcher over it before approval.

After approval, from the reviewed source checkout run the optional setup explicitly:

```powershell
powershell -NoProfile -File .\scripts\setup_control_center_ui.ps1
```

This is the only install step. It creates an isolated Python 3.13 venv at
`%LOCALAPPDATA%\FunPayFlowCodexWorker\control-center-venv` and installs
[CustomTkinter 6.0.0](https://pypi.org/project/customtkinter/6.0.0/) from official PyPI.
It needs network access but no administrator rights, global install, repository dependency
change or execution-policy bypass. If local policy blocks scripts, use the owner's normal
approved script process. Setup does not launch the GUI, Worker or bot.

Double-click `scripts/start_control_center.bat`. It runs only that venv's
`Scripts\python.exe`; if missing, it prints the explicit setup command and stops.
It never installs anything automatically. Missing/wrong CustomTkinter fails before
worker state access or polling. Neither setup nor a real GUI was run during Codex tests.

The Russian dashboard uses genuine CTk rounded cards, blue buttons, four separate
Windows CI, Linux CI, Product QA and Core QA indicators, and selectable, scrollable
history capped at 100 tasks. Click a task (or use Up/Down keys) to inspect its full
status, observed phase timeline and event indicator; click **Открыть GitHub** for its
validated link. Long history summaries are shortened; the selected card shows full text.
Choose **Системная / Светлая / Тёмная** in the header. Appearance follows CTk themes,
with no acrylic/glass blur claim, extra fonts, icons or media assets. The detail card
scrolls independently on smaller screens. No phase percentage is inferred.
An empty dashboard explains that tasks originate in ChatGPT.
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

The monitor deliberately displays **Codex работает**, with heartbeat and freshly observed phase elapsed time.
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

Notifications are **sound-only**: a single short `app.bell()` on the UI main thread
for each new PR-created, CI/QA-failure, all-four-green or worker-blocked event. No popup,
Toplevel, modal dialog, focus steal, automatic browser opening, toast, tray or background
service exists. An unavailable audio device fails quietly; audibility depends on desktop
sound settings. Closing the GUI stops notification delivery; an already running Worker
remains independent. Reopening can announce an event first observed since the last session,
but cannot replay a previously claimed event.

Events remain visible in the selected task card and as a small dot on its history button.
The in-memory event list is bounded to 400 fixed messages for retained tasks; phase history
comes only from the worker's sanitized retained events. Notification dedup storage contains
IDs, not a message archive; no raw logs or task text are stored by the GUI.

`monitor_notifications.json` persists event IDs before sounding, including the PR
head SHA for failure/green events. Refresh/restart cannot replay an already recorded event.
A crash between persistence and sounding may lose one notification. Corrupt/unwritable
storage disables notifications visibly instead of replaying them. At 10,000 IDs the store
fails closed (no eviction/replay). Run one monitor window per worker folder; cross-window
notification coordination is outside this MVP. No task text, raw output, or credentials
are persisted in the notification file.

There is no web server, paid API, alternate assistant, task-entry UI, mobile notifier,
automatic merge, deployment, owner approval bypass, FunPay/Telegram call or bot token use.
Phone Telegram notifications are future work through a separately authorized dedicated
notifier; existing bot tokens must never be reused. Live ChatGPT internals remain unavailable.

## VPS: pinned-source prerequisite; environment preparation disabled

The integrated **Проверить VPS** button starts one bounded background check. Opening
the window and passive refresh never read VPS configuration or start SSH. Repeated
clicks are disabled while checking; closing discards late results. Worker controls
and their existing polling remain independent. No deploy button exists. The visible
**Подготовить обновление** button is disabled with the exact backend outcome
`NOT_PREPARED_ISOLATION_REQUIRED` and `DEPLOY_READY=NO`; checking cannot enable it.

One click observes current main separately and checks the newest **historical push
CI for the fixed target `0affd6784f2f903fa69a38ceb783a30b0816594e`** in
`Vetal12350/FunPayFlow/.github/workflows/ci.yml`, requiring exact SHA, main branch,
push event, both repository identities and completed success. A later UI-only merge
does not invalidate that target. Its CI cannot substitute for target CI. The UI
reports current main as `TARGET` or `DIFFERENT`; it never selects a new target.
The running bot is checked against the owner-confirmed
`/root/FunPayFlow-stage-b567b561` path (not the historical `FunPayFlow-next`)
and the matching systemd `WorkingDirectory`, PID/cwd and **exact** pinned commit.
The unit must be root-owned, with root/default User, the exact
`/etc/systemd/system/funpayflow.service` fragment and no drop-ins; its ancestors and
the production directory must be owned by root, without symlinks or group/world writes.
An unexpected service layout or a change of production version is not silently accepted. It then sends only a fixed stdlib
read-only collector over SSH: service/legacy state, one matching PID and cwd,
current commit, source-file presence, absence of stage dependency directories,
disk threshold (512 MiB), SQLite quick_check and backup-directory presence.
It also compares **all source file bytes and directory entries** in the existing
`/root/.FunPayFlow-source-0affd678` with a freshly fetched exact-target HTTPS archive
from the fixed GitHub codeload URL. Archives stay in bounded memory, are never
extracted, and redirects, links, special files, duplicate/ambiguous paths, unexpected
prefixes and missing parents are refused. No source download/extraction occurs on
the VPS. The stage is traversed through directory descriptors with `O_NOFOLLOW`;
root ownership, no group/world writes, single-link regular files, size/count/time
limits and mutation checks are required. `.env`, private directories and dependency
environments are refused before reading them. No remote filenames or content reach
the GUI. Extra or altered source files cannot count as verified. If the older
source-only staging tool omitted archive entries, this check refuses that stage;
it does not silently repair or repeat staging.
No application/staged code is imported or run. No installs, restaging, backups,
service changes, POST, issue comments or automatic retries occur.

Results contain only enums and the reviewed `b567b561` / `0affd678` prefixes.
`OBSERVED` means these limited observations succeeded, never deploy-ready.
Failures/incomplete evidence show `UNKNOWN` or `REFUSED`. A hash disagreement is
`source_integrity=MISMATCH` / `REFUSED`; inaccessible/unverifiable source is `UNKNOWN`.
Only an exact comparison yields `source_integrity=VERIFIED`. Source evidence is tied
only to `/root/.FunPayFlow-source-0affd678`, never the deployable stage pathname.
`source_staged=YES` alone still means presence, never verified bytes or an environment.
Missing `.venv`, `venv`, `site-packages` means no local dependency environment
observed; it does not attest installed packages or dependency compatibility.

SQLite opens with `mode=ro&immutable=1` plus `query_only`, bounded size (256 MiB)
and progress deadline. WAL/journal presence skips the check as `UNKNOWN`, avoiding
SHM/journal creation and a misleading main-file-only check. This observation of a
live file is **not** a consistent-copy/schema preflight. Backup-directory presence
does not verify any backup. These limitations never enable deployment.

Owner setup after review (not performed by this source task): retain the existing
`%LOCALAPPDATA%/FunPayFlowVpsStatus/config.json` setup described in
[LOCAL_VPS_STATUS.md](LOCAL_VPS_STATUS.md). In that same protected local directory,
manually provide `update_known_hosts`: exactly one ASCII line containing the
configured IPv4, a space, `ssh-ed25519`, a space, and its independently verified
base64 public host key. No wildcards, aliases, extra entries or automatic key
enrollment are accepted. Missing/changed pins fail closed. Neither configuration
nor key contents belong in issues or this checkout. A trusted Windows PATH must
provide OpenSSH and authenticated GitHub CLI; no new dependencies are installed.

The installed Center does not gain this UI automatically. A separately approved
**one-time Center update** must deliver reviewed `scripts/control_center.py`,
`scripts/vps_update_status.py` and `scripts/vps_update_prepare.py` together, retaining sibling
`vps_status_controller.py` and the matching worker. Restart the Center through its
existing `scripts/start_control_center.bat`; launch path and installer are unchanged.
If the optional CTk venv is absent, the existing explicit setup above is still needed.
No installed Windows files were updated here. No fresh ZIP, CMD launcher or per-update
Python script is introduced. After that one-time trusted Center update, supported
routine operations belong in this existing GUI.

**One-click environment preparation is not yet available.** The small real prerequisite
here is historical target CI plus source integrity, not a completed execution workflow.
The existing root SSH identity and root-only source directory do not establish an
isolated build identity with denied access to production/credentials. A venv, clean
environment or pip binary-only flag cannot provide that boundary. No dedicated
identity, sandbox policy or root filesystem/service change is installed by this code.
`prepare_once(approved=True)` therefore still refuses, without I/O, retries or cleanup;
it never reports `ENV_PREPARED`. Declined/missing approval also performs no I/O.
Startup reads only the pure capability, never calls `prepare_once` or confirms anything.

The reusable Linux backend below implements the isolation probe prerequisite.
Environment preparation still needs a reviewed locked-wheel resolver, artifact and
packaged metadata verification and sandbox-only compilation. Until those exist,
no active prepare/confirmation path is shipped. Once supported, the GUI must
ask once for the exact target and stage-only scope before dispatch. Production
cutover remains a separate explicit approval with backup/rollback and health gates.
Hosted Windows/Linux CI and Product/Core QA remain external PR gates.

### Isolated builder protocol v1: actual probe, mandatory owner bootstrap

`scripts/vps_isolated_wheel_builder.py` is a local Linux trusted-broker component.
It contains no SSH transport, installer, package manager, downloads or application
imports. It does not enable or change the Center. This is the request's bounded
**isolation probe/bootstrap deliverable**, not a completed wheel/environment builder.
No host provisioning or live Linux sandbox execution was performed in this checkout.

Its API is passive on import. `check_configuration(trusted_digest=...)` validates
the provisioned filesystem without dispatch; even success returns
`ISOLATION_MISSING / LIVE_PROBE_REQUIRED`. Explicit `probe_once(trusted_digest=...)`
launches the actual fixed `systemd-run` service and can return `ISOLATION_READY`
only after the reviewed probe succeeds and inputs are reattested. Missing bootstrap
returns `ISOLATION_MISSING / BOOTSTRAP_REQUIRED`; unsafe or unverifiable evidence
returns `ISOLATION_REFUSED` with a fixed reason. All results include the fixed target
and `deploy_ready=NO`. There is no `ENV_PREPARED` execution path or environment artifact.
Readiness describes one completed probe, not a cached authorization for later work.

Mandatory **one-time, separately owner-authorized provisioning contract** (no
automatic installation, sudo policy or systemd host configuration is shipped):

1. Review and install this broker and sibling `vps_update_prepare.py` in an
   administrator-controlled code directory, outside the staged application.
   A future authenticated transport may expose only this fixed protocol, never
   caller-selected commands, properties, unit names, paths or Python code. The broker
   needs host root solely for namespace setup; the sandbox Python must be non-root.
   Existing root SSH is not a package build identity and is not used by this module.
2. Provide `/var/lib/funpayflow-wheel-builder`, root:root, no group/other writes,
   no symlink ancestors, and `jobs` beneath it with mode `0700`. The validator
   rejects every mount at or beneath this base, including same-device bind mounts.
   These directories must be ordinary directories on a trusted host filesystem.
3. Provide a minimal `rootfs` beneath that base. It contains only real directories
   `usr`, `usr/bin`, `usr/lib`, optional `lib`/`lib64`, the actual trusted executables
   `usr/bin/env` and `usr/bin/python3.13`, and reviewed Python 3.13 stdlib/ELF runtime
   libraries under `usr/lib`, `lib`, `lib64`. Copy needed loader libraries as regular
   files at their required paths, without symlinks/hardlinks. Provide empty `proc`,
   `sys`, `dev`, `run`, `tmp`, `work`, `input` directories. All entries are root:root, no
   group/other writes, set-id/sticky bits, links, special files or nested mounts;
   ordinary readable `0755` directories/executables and `0644` data are suitable.
   Do not copy the host root filesystem, `/etc`, home directories, credentials,
   logs, package caches or installed third-party packages. File capabilities must
   not be added; bounding/ambient capabilities are empty and NoNewPrivileges is on.
   The image is bounded to 256 MiB, 16,000 entries, 32 MiB per file and a 15-second scan.
4. Independently audit and seal that image using `bootstrap.json` beneath the base,
   a root:root regular single-link file, no group/other writes, at most 4096 bytes,
   with exactly `{"protocol":1,"rootfs_digest":"<64 lowercase hex>"}`. The digest
   uses PR #133's canonical entry format: relative path to `["dir"]` or
   `["file", byte_count, sha256_hex]`, SHA-256 of UTF-8 JSON with sorted keys and
   separators `(',', ':')`. Permissions are checked separately. This seal is a trust
   assertion by the owner after auditing the image, not evidence the probe can mint
   for an arbitrary host tree. Replacing the interpreter/image requires renewed review.
5. Supply a pristine public `source-0affd678` beneath the base, with exactly the
   fixed-target archive entries, root:root and the same source restrictions as
   PR #133 (no links, private/runtime/environment entries or extra files). Do not
   bind `/root`, copy runtime data, relax production permissions or give DynamicUser
   access to the existing root-only stage. Source transfer must be a separately
   reviewed trusted exporter of public source bytes. The future Center must obtain
   `trusted_source_digest()` through the existing fixed HTTPS archive attestation
   and pass it as `trusted_digest`; an issue-supplied or self-calculated stage digest
   is not provenance. This backend never changes the source or fetches an archive.
6. On an authorized disposable Linux host, verify systemd supports all requested
   properties, mount/network namespaces, seccomp and resource controllers. No
   property may be silently dropped, no generic/per-unit drop-in may override the
   policy, and no fallback is permitted. Run the explicit probe and review its
   result before approving transport integration. The CLI defaults to configuration
   checking; `--probe` explicitly dispatches, both require `--trusted-source-digest`.

The transient unit uses `DynamicUser=yes`, `NoNewPrivileges=yes`,
`ProtectSystem=strict`, `ProtectHome=yes`, an independently sealed `RootDirectory`,
only a read-only source bind at `/input`, private devices/tmp/network, hidden
foreign processes, no capabilities and restricted syscalls/address families.
The Python process receives only `PATH=/usr/bin` and `LC_ALL=C` through trusted
`env -i`, uses `-I -S`, and executes only the fixed probe. No shell is involved.
The probe checks Python 3.13, UID/GID/groups, clean environment, kernel capabilities,
NoNewPrivs/seccomp, distinct mount/network namespaces, effective read-only root/input,
and the actual input bind's device/inode identity against the attested host snapshot,
and a bounded noexec/nodev/nosuid tmpfs output at `/work`. It tests denied/absent
root/home/log/credential and foreign `/proc/1` access without reading any bytes there;
it tests socket creation denial without connecting to any endpoint. It verifies its
own eight-byte output's size, type, single link and non-root owner inside the sandbox.
It never compiles or executes project/package code. `/work` is ephemeral per unit;
its probe marker is not an environment artifact and is not exported.

Each dispatch reserves a unique root-only `jobs/<32 hex>` directory before launch.
An existing reservation is refused. At 64 reservations further probes are refused
until explicit owner reconciliation; no automatic deletion or retry exists.
The service has a 20-second runtime limit, 3-second stop timeout, process-group kill,
128 MiB memory, no swap, eight tasks, 50% CPU and a 16 MiB output tmpfs with 1 MiB
file limit. `/tmp` is a separate bounded 4 MiB tmpfs inside the rootfs; host-backed
`PrivateTmp` is disabled, so no host `/tmp` or `/var/tmp` bind is required.
A 28-second client timeout attempts a bounded stop of only that unique probe unit,
retains its reservation and returns `DISPATCH_UNKNOWN` even when stop succeeds.
The caller must not blindly retry unknown results. No production unit is addressed.

`binary_lock_blocker(bytes)` is a separate pure static preflight for the supported
v1/revision-3 Python `>=3.13` lock runtime graph. Ambiguous versions, conditional/extra
edges and non-PyPI sources are refused as `LOCK_UNSUPPORTED`. An sdist-only runtime
dependency yields `BINARY_WHEELS_MISSING`; the current checkout's `funpayapi==1.1.0`
has this blocker. An all-wheel graph still yields `LOCK_RESOLVER_REQUIRED`: wheel
availability alone does not attest compatible tags, pinned hashes or packaged metadata.
No pip/uv fallback, project build, root installation or `ENV_PREPARED` is possible.
Resolving this blocker requires a separate reviewed dependency decision, outside
this task's prohibition on lock/dependency changes.

## Offline verification

`tests/test_control_center.py` imports headlessly without creating Tk windows. Tests fake
all gh responses and process startup; `tests/test_codex_issue_worker.py` covers safe phase
snapshots, heartbeat, locking, timeout cleanup, mark-before-execution and restart dedup.
`tests/test_vps_update_status.py` fakes GitHub, SSH, systemd, proc and SQLite responses,
and `tests/test_vps_update_prepare.py` verifies synthetic archives and a descriptor-only
fake source tree, plus the actual disabled GUI capability gate. They cover source
mismatch, corrupt archives, unsafe ownership/links/paths, extra stage/environment
entries, changed main, unvetted target CI and sanitized timeout/partial responses.
`tests/test_vps_isolated_wheel_builder.py` exercises the real fixed probe code against
synthetic kernel observations and the broker against a descriptor-only fake host:
non-root credentials, denied production/credential access, clean environment,
hashes, links/ownership/mounts, output ownership/limits, collisions, timeouts and
missing binary wheels. No test launches real systemd or proves live Linux isolation.
Wheel installation/metadata/compilation remain unimplemented and cannot be enabled.
Run with the existing development environment:

```powershell
New-Item -ItemType Directory -Force tests/.offline-tmp | Out-Null
$env:TEMP = Join-Path (Get-Location) 'tests/.offline-tmp'
$env:TMP = $env:TEMP
$env:PYTHONDONTWRITEBYTECODE = '1'
uv run --no-sync pytest -q -p no:cacheprovider tests/test_control_center.py tests/test_codex_issue_worker.py tests/test_vps_update_status.py tests/test_vps_update_prepare.py tests/test_vps_isolated_wheel_builder.py tests/test_vps_status_controller.py --basetemp=tests/.offline-tmp/control-center
```

Keep every generated fixture under `tests/.offline-tmp/`; do not use other scratch paths.
Tests fake CTk, gh responses and process startup. They do not install packages, create real
windows, call FunPay/Telegram, perform POST, or read runtime credentials. Windows/Linux CI,
Product/Core QA and ChatGPT audit must all pass before any separately authorized merge.
Headless tests do not prove real desktop rendering or audible Windows sound.
`CURRENT_STATE.md` is intentionally unchanged: this task's explicit path allowlist
excludes it. The current backend scope and remaining blockers are recorded here.

## Rollback after an explicit upgrade

Close the monitor (the Worker stays independent). Restore the previously approved
`scripts/control_center.py` and `scripts/start_control_center.bat` from the owner's retained
source copy through the normal source update process. The old launcher uses Python 3.13
with stdlib Tkinter. Leave the optional venv unused; no deletion or reinstall is needed.
Do not reset notification IDs: both presentations use the same dedup store. Do not change
worker state, authentication or the running Worker to roll back the presentation.

## Windows manual UI smoke (after review)

This is a presentation/integration check for the owner in the normal authorized local
installation, not an instruction to change worker state or run production. Do not press
**Запустить Worker** when an existing worker is active or uncertain.

1. After approval and explicit setup, open the monitor with the new launcher. Confirm no worker starts on opening.
   Keep commands/tasks in ChatGPT; there is no task-entry field in this application.
2. Inspect at 1150×700 and 1366×768, then Windows display scaling 100%, 125% and 150%.
   Confirm readable text, wrapping in the summary/status strip, four individual check
   indicators, visible bottom buttons, Up/Down task selection and history/detail scrolling. Switch all three themes.
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
6. With synthetic fixture events (not live task execution), verify PR creation,
   CI/QA failure, four-green and blocked each produce exactly one short sound and a small
   in-app event indicator. No separate window appears, focus stays in the app, no browser
   opens. Refresh and reopen: already claimed events stay silent. With unavailable audio,
   refresh still works quietly. Corrupt/unwritable notification storage disables sound
   visibly without replay. Do not edit real worker telemetry to manufacture fixtures.
7. On a copy without the optional venv, double-click the launcher: expect a clear setup
   message and no automatic install or Worker startup. Reopen after setup; only the GUI
   starts. Closing it must not terminate an existing Worker.

Native Windows toasts, tray mode, auto-start and app packaging are future **separate
scoped tasks**, after this phase's UI is checked. None is implemented or enabled here.
Windows/Linux CI, Product/Core QA, and ChatGPT security/UI review are subsequent gates;
offline tests alone do not claim those external reviews or a desktop smoke PASS.
