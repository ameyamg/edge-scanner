@echo off
setlocal
title Edge Scanner Setup
cd /d "%~dp0"

echo Edge Scanner setup
echo.

rem Python 3.11 to 3.14. A brand-new Python (3.15 in October 2026) has no Windows wheels yet for
rem some packages (pyarrow, pyyaml), so pip tries to compile them and fails. The py launcher can
rem pick a supported version when several are installed, whichever one is first on PATH.
set "VERSION_OK=import sys; sys.exit(0 if (3, 11) <= sys.version_info[:2] <= (3, 14) else 1)"
set "PY="
where py >nul 2>nul
if not errorlevel 1 (
    for %%V in (3.13 3.12 3.14 3.11) do if not defined PY (
        py -%%V -c "" >nul 2>nul && set "PY=py -%%V"
    )
)
if not defined PY (
    where python >nul 2>nul
    if not errorlevel 1 (
        python -c "%VERSION_OK%" && set "PY=python"
    )
)
if defined PY goto :python_found

rem None found: offer to install 3.13 for this Windows user. winget ships with Windows 10 and 11.
where winget >nul 2>nul
if errorlevel 1 goto :python_missing
echo No Python 3.11 to 3.14 was found. Setup can install Python 3.13 for you, for this Windows
echo user only. A newer Python already on this machine stays as it is.
choice /C YN /M "Install Python 3.13 now"
if errorlevel 2 goto :python_missing
rem InstallLauncherAllUsers=0 keeps the py launcher per-user too, so no admin prompt is needed.
winget install --exact --id Python.Python.3.13 --scope user --silent --accept-package-agreements --accept-source-agreements --custom "InstallLauncherAllUsers=0"
rem PATH in this window is not refreshed, so use the installed python.exe by its path.
set "PY313=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if exist "%PY313%" set PY="%PY313%"
if defined PY goto :python_found

:python_missing
echo Python 3.11 to 3.14 is needed. Python 3.13 is recommended:
echo https://www.python.org/downloads/windows/
echo Tick "Add python.exe to PATH" in the installer, then run setup.bat again.
echo A newer Python can stay installed; setup picks 3.13 when both are there.
where python >nul 2>nul && (
    echo This machine has:
    python --version
)
goto :fail

:python_found
echo Using:
%PY% --version

where npm >nul 2>nul
if errorlevel 1 (
    echo Node.js was not found. Install the LTS version from https://nodejs.org/ and run setup.bat again.
    goto :fail
)

echo [1/5] Creating the Python environment in .venv
if exist ".venv\Scripts\python.exe" (
    rem A .venv left by an earlier run on an unsupported Python is rebuilt.
    ".venv\Scripts\python.exe" -c "%VERSION_OK%" >nul 2>nul
    if errorlevel 1 (
        echo       Rebuilding .venv, it was made with an unsupported Python
        rmdir /s /q .venv
    )
)
if not exist ".venv\Scripts\python.exe" (
    %PY% -m venv .venv
    if errorlevel 1 goto :fail
)

echo [2/5] Installing the Python packages
".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
if errorlevel 1 goto :fail

echo [3/5] Installing the dashboard packages (a few minutes the first time)
pushd dashboard-v2
call npm ci --no-audit --no-fund --loglevel=error
if errorlevel 1 (
    rem An older npm can reject a lock file written by a newer one; npm install reconciles it.
    echo       Retrying with npm install
    call npm install --no-audit --no-fund --loglevel=error
    if errorlevel 1 (
        popd
        goto :fail
    )
)

echo [4/5] Building the dashboard
call npm run build
if errorlevel 1 (
    popd
    goto :fail
)
popd

echo [5/5] Creating .env for your market-data keys
if exist ".env" (
    echo       .env already exists, left as it is
) else (
    copy ".env.example" ".env" >nul
    echo       Created. Open .env and add your Alpaca keys, or set DATA_PROVIDER=schwab.
)

echo.
echo Setup finished. Add your keys to .env, then start it with start_scanner.bat
echo and open http://localhost:7777
echo.
echo Every install starts empty: no watchlists, no saved screens of your own and no alert
echo history. The first start downloads market history and can take 10 to 20 minutes.
echo Optional sample setups: .venv\Scripts\python scripts\install_setup_library.py
echo.
pause
exit /b 0

:fail
echo.
echo Setup stopped. Fix the problem above and run setup.bat again; finished steps are skipped or repeated safely.
pause
exit /b 1
