# Explicit opt-in only. Never invoked by the launcher or by tests.
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
if (-not $env:LOCALAPPDATA) { throw 'LOCALAPPDATA is required on Windows.' }
$uiVenv = Join-Path $env:LOCALAPPDATA 'FunPayFlowCodexWorker\control-center-venv'
$uiPython = Join-Path $uiVenv 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $uiPython)) {
    & py -3.13 -m venv $uiVenv
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.13 with venv is required. No global install was attempted.' }
}
# Refuse an unrelated interpreter or venv with global site packages enabled.
& $uiPython -I -c "import pathlib, sys; p = pathlib.Path(sys.prefix); assert sys.version_info[:2] == (3, 13); assert sys.prefix != sys.base_prefix; assert p.resolve() == pathlib.Path(sys.argv[1]).resolve(); assert 'include-system-site-packages = false' in (p / 'pyvenv.cfg').read_text().lower()" $uiVenv
if ($LASTEXITCODE -ne 0) { throw 'Expected an isolated Python 3.13 UI venv.' }
# --isolated ignores pip environment variables and user config; use official PyPI only.
$previousPipConfig = $env:PIP_CONFIG_FILE
try {
    # Windows null device disables global/site pip config (including alternate indexes).
    $env:PIP_CONFIG_FILE = 'NUL'
    & $uiPython -I -m pip --isolated install --index-url https://pypi.org/simple --only-binary=:all: --no-cache-dir 'CustomTkinter==6.0.0'
    if ($LASTEXITCODE -ne 0) { throw 'Optional UI setup failed. The launcher will not install or repair packages.' }
} finally {
    $env:PIP_CONFIG_FILE = $previousPipConfig
}
Write-Host 'Optional UI ready. Launch scripts\start_control_center.bat after approval.'
