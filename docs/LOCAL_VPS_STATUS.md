# Local VPS status controller — phase 1

This is an independent, opt-in Windows process for the owner. It only automates
status. Deployment and an approval controller require a separately reviewed phase 2.
It is not integrated with the Codex worker, FunPayFlow runtime, CI or startup tasks.
No action runs without `--configure`, `--once` or `--watch`. Development tests never
connect to a VPS, GitHub, FunPay or Telegram.

## Local setup (owner operation, outside the Codex sandbox)

Use a separately reviewed, trusted source checkout with Python 3.13+, Windows
OpenSSH `ssh.exe` and GitHub CLI `gh.exe` on a trusted PATH. Authenticate `gh`
locally for `Vetal12350/FunPayFlow`; do not put credentials in this repository or
issues. Independently verify and enroll the VPS host key in the Windows user's
default OpenSSH known_hosts before running. Unknown or changed host keys fail closed.

Run locally from that trusted checkout:

```powershell
python -B scripts/vps_status_controller.py --configure
```

The local prompts accept only a canonical public IPv4 and an absolute, drive-based
Windows SSH identity filename. Enter the filename, never key contents. The controller
does not open the key; OpenSSH uses it. The SSH account is fixed to `root`, matching
the existing owner-specific preflight paths. There is no account, port, command,
repository, URL, identity or host override from an issue. Other installation layouts
need separate review; this is not a general VPS tool.

Configuration is stored only at
`%LOCALAPPDATA%\FunPayFlowVpsStatus\config.json`, with exactly `host` and `identity`
fields. LOCALAPPDATA must be an absolute Windows path outside the source checkout.
Protect this directory, the checkout, PATH and SSH identity using Windows account
permissions; do not use a shared or attacker-writable location. Configuration creation
is exclusive. To change it, manually edit that local JSON after stopping the process;
validation applies again at startup. Never copy it into an issue or source checkout.

## Explicit operation

```powershell
# One poll, sanitized terminal output only:
python -B scripts/vps_status_controller.py --once
# Foreground polling every 60 seconds, opt in to sanitized GitHub comments:
python -B scripts/vps_status_controller.py --watch --post-results
```

Stop watch with Ctrl+C. There is no automatic background launch or autostart.
Starting this process is an explicit owner decision to permit the documented SSH
status checks and, with `--post-results`, GitHub comments. No development command
in this document grants production access to a coding agent.

Only open issues in `Vetal12350/FunPayFlow` authored by login `Vetal12350`, labelled
`vps-local`, with **exact title** `[VPS] status` and **exact body** `status` qualify.
Trailing whitespace, extra instructions, code and other actions are rejected.
Issue numbers must be integers in 1..2147483647. Each poll examines at most 50
matching open issues; the JSON response is capped at 256 KiB before parsing, labels
at 100 entries of 50 characters. No issue content is executed or used as a filename,
host, identity, account or program; only the validated integer identifies a local claim
and GitHub comment target.

Before SSH, the controller exclusively creates and fsyncs
`%LOCALAPPDATA%\FunPayFlowVpsStatus\processed\<number>.done`.
Markers persist across restarts and concurrent processes. They are never automatically
removed, even on timeout, crash or comment failure. This deliberately favors at-most-once
attempts over guaranteed delivery. An interrupted attempt can remain unreported.
Request another check with a new qualifying issue, rather than retrying an uncertain
attempt by deleting a marker. Claims accumulate locally; keep them while issues can
still be polled. Disk/state failures prevent SSH. Claims are also consumed in terminal-only
mode; adding `--post-results` later will not replay them.

## Fixed read-only boundary

SSH receives a list of arguments with `shell=False`, no pseudo-terminal, batch mode,
strict host-key checking, no agent/port forwarding, no proxy and no user SSH config
(`-F NUL`). Password and interactive authentication are disabled; only the configured
identity is selected. The sole remote command is exactly `python3 -`. Its stdin is
the existing `scripts/vps_preflight.py` source from beside the controller, checked
against a reviewed SHA-256 pin (LF/CRLF checkout differences allowed). A changed
preflight fails closed until its changes and pin are reviewed together. No issue
body can supply source bytes. All subprocess calls have a 45-second timeout, with
SSH connection timeout 10 seconds and one connection attempt; failures are not retried.
A local timeout cannot guarantee remote process termination; the remote source only
performs bounded read-only checks. No backup, restart, staging, deploy, installation,
lot publication or runtime account operations are supported.

The preflight checks existing service states, process count, worktree state, config
file existence, SQLite read-only quick_check and disk availability. Its fixed historical
layout is described in [PRODUCTION.md](PRODUCTION.md). The controller requires the
exact preflight JSON schema, enums, boolean safety flags and valid commit format.
Unknown fields, duplicate keys, malformed/oversized JSON, and arbitrary strings fail
closed. Successful reports expose only service/legacy state, runtime count category,
worktree category, SQLite category and disk category. No commit IDs, IP, identity paths,
environment, raw stdout/stderr, logs or exception text are printed or posted.

Failures use only fixed categories: `local`, `github`, `ssh`, `timeout`, `schema`,
`source`. A failure during an accepted status attempt can produce a fixed category
comment when posting is enabled; polling/persistence/delivery failures stop the process.
No issue is automatically closed, including after a successful report. There are no
automatic retries of failed or uncertain comments. GH comments are the only optional
external write; there are no production write commands.

## Offline regression verification

```powershell
python -B tests/test_vps_status_controller.py
```

The stdlib unittest suite fakes all controller filesystem operations and subprocesses;
it requires no installed runtime dependencies or private data and writes no test
artifacts. Pytest can also collect it with `PYTHONDONTWRITEBYTECODE=1` and
`pytest -q -p no:cacheprovider tests/test_vps_status_controller.py`; this suite needs
no temporary directory. For existing tests that require fixtures, pre-create a trusted
ignored `.pytest_cache` directory and use it for cache/base-temp output. Full
Windows/Linux CI and Product/Core QA
remain review checks for the trusted PR workflow; this controller does not open PRs.
Keep phase 1 limited to this document, its script and its test file. `CURRENT_STATE`
is intentionally unchanged to respect that three-file scope.
