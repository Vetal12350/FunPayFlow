# Local queue reader only. Does not start the FunPay runtime.
$ErrorActionPreference = "Stop"
$homeDir = Join-Path $env:LOCALAPPDATA "FunPayFlowCodexWorker"
$source = Join-Path $homeDir "source"
$worker = Join-Path $source "scripts\codex_issue_worker.py"

if (-not (Test-Path $worker)) {
    throw "Worker source not installed; see docs/LOCAL_CODEX_WORKER.md."
}
& git -C $source pull --ff-only --quiet
if ($LASTEXITCODE -ne 0) {
    throw "Launcher checkout cannot fast-forward. Refusing to run."
}
& py -3.13 $worker --watch --interval 90
if ($LASTEXITCODE -ne 0) {
    throw "Local Codex worker stopped with an error."
}
