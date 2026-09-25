@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul
pushd "%~dp0" || exit /b 1
call "%~dp0ResolveDataDir.bat"
if errorlevel 1 (
  popd
  pause
  exit /b 1
)
if not exist "%FUNPAY_BOT_DATA_DIR%\.env" (
  echo Private configuration is missing. Run Setup.bat again.
  popd
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  echo Python environment is missing. Run Setup.bat first.
  popd
  pause
  exit /b 1
)
".venv\Scripts\python.exe" main.py
set "BOT_EXIT=%ERRORLEVEL%"
if "%BOT_EXIT%"=="3" echo Another bot instance already holds the process lock.
if not "%BOT_EXIT%"=="0" if not "%BOT_EXIT%"=="3" echo Bot stopped with an error. Check the message above and the private logs folder.
echo Bot stopped. Press a key to close this window.
pause >nul
popd
exit /b %BOT_EXIT%
