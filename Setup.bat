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
echo.
echo +------------------------------------+
echo ^|            FUNPAYFLOW              ^|
echo ^|            v%BOT_VERSION%                  ^|
echo ^|  AUTOMATION / ANALYTICS / CONTROL  ^|
echo +------------------------------------+
echo.
echo Выберите язык / Choose language
echo [1] Русский
echo [2] English
:choose_language
set "LANGUAGE_CHOICE="
set /p "LANGUAGE_CHOICE=> "
if not defined LANGUAGE_CHOICE set "LANGUAGE_CHOICE=1"
if "%LANGUAGE_CHOICE%"=="1" goto :russian
if "%LANGUAGE_CHOICE%"=="2" goto :english
echo Выберите 1 или 2 / Choose 1 or 2.
goto :choose_language

:russian
set "INSTALLER_LANGUAGE=ru"
set "MSG_INITIAL=Первоначальная настройка"
set "MSG_OS=[ERROR] Требуется Windows 10 или новее."
set "MSG_DATA=[ERROR] Не удалось определить каталог данных. Задайте FUNPAY_BOT_DATA_DIR."
set "MSG_STAGE1=[1/3] Подготовка Python и установщика..."
set "MSG_STAGE2=[2/3] Установка зависимостей..."
set "MSG_STAGE3=[3/3] Открытие настройки..."
set "MSG_NO_UV=[ERROR] uv и winget недоступны."
set "MSG_UV_GUIDE=Установите uv по инструкции https://docs.astral.sh/uv/getting-started/installation/ и повторите Setup.bat."
set "MSG_WINGET=Установка uv через официальный пакет winget..."
set "MSG_UV_MISSING=[ERROR] uv установлен, но недоступен в этом окне."
set "MSG_NEW_TERMINAL=Откройте новое окно терминала и повторите Setup.bat."
set "MSG_READY=[OK] Зависимости готовы."
set "MSG_INSTALL_FAILED=[ERROR] Не удалось установить Python или зависимости."
set "MSG_DETAILS=Подробный вывод установщика:"
set "MSG_PRIVATE=Существующие личные данные не удалены."
set "MSG_CONFIG_FAILED=[ERROR] Настройка не завершена."
set "MSG_CANCELLED=[!] Настройка отменена."
set "MSG_CLOSE=Нажмите Enter, чтобы закрыть окно"
goto :language_ready

:english
set "INSTALLER_LANGUAGE=en"
set "MSG_INITIAL=Initial setup"
set "MSG_OS=[ERROR] Windows 10 or newer is required."
set "MSG_DATA=[ERROR] Could not locate the data directory. Set FUNPAY_BOT_DATA_DIR."
set "MSG_STAGE1=[1/3] Preparing Python and installer..."
set "MSG_STAGE2=[2/3] Installing dependencies..."
set "MSG_STAGE3=[3/3] Opening configuration..."
set "MSG_NO_UV=[ERROR] uv and winget are unavailable."
set "MSG_UV_GUIDE=Install uv from https://docs.astral.sh/uv/getting-started/installation/ and rerun Setup.bat."
set "MSG_WINGET=Installing uv via the official winget package..."
set "MSG_UV_MISSING=[ERROR] uv is installed but not available in this terminal."
set "MSG_NEW_TERMINAL=Open a new terminal and rerun Setup.bat."
set "MSG_READY=[OK] Dependencies ready."
set "MSG_INSTALL_FAILED=[ERROR] Python or dependency installation failed."
set "MSG_DETAILS=Detailed installer output:"
set "MSG_PRIVATE=Existing private data was not deleted."
set "MSG_CONFIG_FAILED=[ERROR] Configuration did not complete."
set "MSG_CANCELLED=[!] Setup canceled."
set "MSG_CLOSE=Press Enter to close this window"

:language_ready
echo.
echo %MSG_INITIAL%
echo =========================
if /i not "%OS%"=="Windows_NT" goto :unsupported_os
call powershell -NoProfile -Command "if ([Environment]::OSVersion.Version.Major -ge 10) { exit 0 } else { exit 1 }" >nul 2>nul
if errorlevel 1 goto :unsupported_os
call "%RESOLVER%" >nul 2>nul
if errorlevel 1 (
  echo %MSG_DATA%
  call :wait_for_enter
  popd
  exit /b 1
)
if not defined TEMP set "TEMP=%LOCALAPPDATA%"
if not defined TEMP set "TEMP=%USERPROFILE%"
set "SETUP_LOG=%TEMP%\FunPayFlow-setup.log"
cd /d "%APP_DIR%" || goto :install_failed
echo %MSG_STAGE1%
set "UV_EXE=uv"
where uv >nul 2>nul
if errorlevel 1 (
  where winget >nul 2>nul
  if errorlevel 1 (
    echo %MSG_NO_UV%
    echo %MSG_UV_GUIDE%
    call :wait_for_enter
    popd
    exit /b 1
  )
  echo %MSG_WINGET%
  winget install --id astral-sh.uv --exact --source winget --accept-package-agreements --accept-source-agreements >"%SETUP_LOG%" 2>&1
  if errorlevel 1 goto :install_failed
  if exist "%LOCALAPPDATA%\Microsoft\WinGet\Links\uv.exe" set "UV_EXE=%LOCALAPPDATA%\Microsoft\WinGet\Links\uv.exe"
  if exist "%USERPROFILE%\.local\bin\uv.exe" set "UV_EXE=%USERPROFILE%\.local\bin\uv.exe"
)
call "%UV_EXE%" --version >nul 2>nul
if errorlevel 1 (
  echo %MSG_UV_MISSING%
  echo %MSG_NEW_TERMINAL%
  call :wait_for_enter
  popd
  exit /b 1
)
call "%UV_EXE%" python install 3.13 >"%SETUP_LOG%" 2>&1
if errorlevel 1 goto :install_failed
echo %MSG_STAGE2%
call "%UV_EXE%" sync --locked --no-dev --python 3.13 >>"%SETUP_LOG%" 2>&1
if errorlevel 1 goto :install_failed
echo %MSG_READY%
echo %MSG_STAGE3%
call "%UV_EXE%" run --no-sync python setup_config.py --data-dir "%FUNPAY_BOT_DATA_DIR%" --language "%INSTALLER_LANGUAGE%" --dependencies-ready
if errorlevel 2 goto :canceled
if errorlevel 1 goto :config_failed
call :wait_for_enter
popd
exit /b 0

:unsupported_os
echo %MSG_OS%
call :wait_for_enter
popd
exit /b 1
:install_failed
echo %MSG_INSTALL_FAILED%
echo %MSG_DETAILS% "%SETUP_LOG%"
echo %MSG_PRIVATE%
call :wait_for_enter
popd
exit /b 1
:config_failed
echo %MSG_CONFIG_FAILED% %MSG_PRIVATE%
call :wait_for_enter
popd
exit /b 1
:canceled
echo %MSG_CANCELLED% %MSG_PRIVATE%
call :wait_for_enter
popd
exit /b 2

:wait_for_enter
call powershell -NoProfile -Command "if ([Console]::IsInputRedirected) { exit 1 }" >nul 2>nul
if errorlevel 1 exit /b 0
set "SETUP_ACK="
set /p "SETUP_ACK=%MSG_CLOSE%: "
exit /b 0
