@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul
if /i not "%OS%"=="Windows_NT" (
  echo This installer requires Windows 10 or newer.
  exit /b 1
)
powershell -NoProfile -Command "if ([Environment]::OSVersion.Version.Major -ge 10) { exit 0 } else { exit 1 }" >nul 2>nul
if errorlevel 1 (
  echo Windows 10 or newer is required.
  exit /b 1
)
pushd "%~dp0" || exit /b 1
call "%~dp0ResolveDataDir.bat"
if errorlevel 1 (
  popd
  exit /b 1
)
set "UV_EXE=uv"
where uv >nul 2>nul
if errorlevel 1 (
  where winget >nul 2>nul
  if errorlevel 1 (
    echo Install uv from https://docs.astral.sh/uv/getting-started/installation/ and rerun Setup.bat.
    popd
    exit /b 1
  )
  echo Installing uv via the official winget package...
  winget install --id astral-sh.uv --exact --source winget --accept-package-agreements --accept-source-agreements
  if errorlevel 1 (
    echo uv installation failed. Rerun Setup.bat after resolving winget.
    popd
    exit /b 1
  )
  if exist "%LOCALAPPDATA%\Microsoft\WinGet\Links\uv.exe" set "UV_EXE=%LOCALAPPDATA%\Microsoft\WinGet\Links\uv.exe"
  if exist "%USERPROFILE%\.local\bin\uv.exe" set "UV_EXE=%USERPROFILE%\.local\bin\uv.exe"
)
"%UV_EXE%" --version >nul 2>nul
if errorlevel 1 (
  echo uv is installed but not available in this terminal. Open a new terminal and rerun Setup.bat.
  popd
  exit /b 1
)
"%UV_EXE%" python install 3.13
if errorlevel 1 goto :failed
"%UV_EXE%" sync --locked --no-dev --python 3.13
if errorlevel 1 goto :failed
"%UV_EXE%" run --no-sync python setup_config.py --data-dir "%FUNPAY_BOT_DATA_DIR%"
if errorlevel 1 goto :failed
echo Application data: "%FUNPAY_BOT_DATA_DIR%"
echo Setup complete. Run Start.bat, then send /start to your Telegram bot.
popd
exit /b 0
:failed
echo Setup did not complete. Existing private data was not deleted.
popd
exit /b 1
