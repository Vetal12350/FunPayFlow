@echo off
rem Shared by Setup.bat and Start.bat. Never derive private data from %~dp0.
if defined FUNPAY_BOT_DATA_DIR goto :resolved
if defined LOCALAPPDATA set "FUNPAY_BOT_DATA_DIR=%LOCALAPPDATA%\FunPayFlow"
if defined FUNPAY_BOT_DATA_DIR goto :resolved
if defined USERPROFILE set "FUNPAY_BOT_DATA_DIR=%USERPROFILE%\AppData\Local\FunPayFlow"
if not defined FUNPAY_BOT_DATA_DIR (
  echo Cannot locate per-user application data. Set FUNPAY_BOT_DATA_DIR to an absolute path. 1>&2
  exit /b 1
)
:resolved
if /i "%~1"=="--print" set FUNPAY_BOT_DATA_DIR
exit /b 0
