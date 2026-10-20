@echo off
setlocal
set "UI_PYTHON=%LOCALAPPDATA%\FunPayFlowCodexWorker\control-center-venv\Scripts\python.exe"
if not exist "%UI_PYTHON%" (
    echo Optional Control Center UI environment is missing.
    echo Run explicitly: powershell -NoProfile -File "%~dp0setup_control_center_ui.ps1"
    pause
    exit /b 1
)
"%UI_PYTHON%" "%~dp0control_center.py"
if errorlevel 1 pause
