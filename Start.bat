@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul
pushd "%~dp0" || exit /b 1
set "APP_DIR=%CD%"
if exist "app\pyproject.toml" set "APP_DIR=%CD%\app"
set "RESOLVER=%~dp0ResolveDataDir.bat"
if exist "%~dp0app\ResolveDataDir.bat" set "RESOLVER=%~dp0app\ResolveDataDir.bat"
set "BOT_VERSION=unknown"
for /f "tokens=3" %%V in ('findstr /b /c:"version = " "%APP_DIR%\pyproject.toml"') do set "BOT_VERSION=%%~V"
set "INSTALLER_LANGUAGE=ru"
call "%RESOLVER%" >nul 2>nul
if errorlevel 1 goto :data_error
cd /d "%APP_DIR%" || goto :data_error
if exist "%FUNPAY_BOT_DATA_DIR%\installer_language.txt" (
  findstr /x /c:"en" "%FUNPAY_BOT_DATA_DIR%\installer_language.txt" >nul 2>nul
  if not errorlevel 1 set "INSTALLER_LANGUAGE=en"
)
if "%INSTALLER_LANGUAGE%"=="en" goto :english

:russian
set "MSG_CONFIG_OK=[OK] Конфигурация найдена."
set "MSG_DATA_OK=[OK] Каталог данных готов."
set "MSG_CONFIG_MISSING=[ERROR] Конфигурация не найдена. Запустите Setup.bat."
set "MSG_PYTHON_MISSING=[ERROR] Среда Python не найдена. Запустите Setup.bat."
set "MSG_BOT_START=Запуск бота..."
set "MSG_STOP=Бот остановлен. Нажмите любую клавишу, чтобы закрыть окно."
goto :language_ready

:english
set "MSG_CONFIG_OK=[OK] Configuration found."
set "MSG_DATA_OK=[OK] Data directory ready."
set "MSG_CONFIG_MISSING=[ERROR] Private configuration is missing. Run Setup.bat first."
set "MSG_PYTHON_MISSING=[ERROR] Python environment is missing. Run Setup.bat first."
set "MSG_BOT_START=Starting bot..."
set "MSG_STOP=Bot stopped. Press a key to close this window."

:language_ready
if not exist "%FUNPAY_BOT_DATA_DIR%\.env" (
  if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" console_ui.py start-error --language "%INSTALLER_LANGUAGE%" --kind missing_config
  ) else (
    echo FunPayFlow v%BOT_VERSION%
    echo %MSG_CONFIG_MISSING%
  )
  popd
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  echo FunPayFlow v%BOT_VERSION%
  echo %MSG_PYTHON_MISSING%
  popd
  pause
  exit /b 1
)
".venv\Scripts\python.exe" console_ui.py start-ready --language "%INSTALLER_LANGUAGE%" --data-dir "%FUNPAY_BOT_DATA_DIR%"
if errorlevel 1 (
  echo FunPayFlow v%BOT_VERSION%
  echo %MSG_CONFIG_OK%
  echo %MSG_DATA_OK%
  echo %MSG_BOT_START%
)
".venv\Scripts\python.exe" main.py
set "BOT_EXIT=%ERRORLEVEL%"
if "%BOT_EXIT%"=="3" (
  ".venv\Scripts\python.exe" console_ui.py start-error --language "%INSTALLER_LANGUAGE%" --kind lock --no-banner
)
if not "%BOT_EXIT%"=="0" if not "%BOT_EXIT%"=="3" (
  ".venv\Scripts\python.exe" console_ui.py start-error --language "%INSTALLER_LANGUAGE%" --kind runtime_error --no-banner
)
echo %MSG_STOP%
pause >nul
popd
exit /b %BOT_EXIT%

:data_error
echo.
echo FunPayFlow v%BOT_VERSION%
echo [ERROR] Не удалось определить каталог данных. Запустите Setup.bat.
pause
popd
exit /b 1
