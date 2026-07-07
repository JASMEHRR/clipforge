@echo off
rem ClipForge zero-setup launcher (Windows).
rem Finds or installs everything automatically: Python 3.11, the virtual
rem environment, all Python packages, and FFmpeg. Just double-click.
setlocal enabledelayedexpansion
cd /d "%~dp0"

rem --- find ANY Python to run the self-installer (stdlib only) ------------
set "BOOT="
py -3.11 -c "1" >nul 2>&1 && set "BOOT=py -3.11"
if not defined BOOT py -3 -c "1" >nul 2>&1 && set "BOOT=py -3"
if not defined BOOT python -c "1" >nul 2>&1 && set "BOOT=python"

if not defined BOOT (
    echo No Python found. Attempting automatic install via winget...
    winget install -e --id Python.Python.3.11 --accept-source-agreements --accept-package-agreements --silent
    py -3.11 -c "1" >nul 2>&1 && set "BOOT=py -3.11"
)
if not defined BOOT (
    echo.
    echo Python could not be installed automatically.
    echo Please install Python 3.11 from:
    echo   https://www.python.org/downloads/release/python-3119/
    echo ^(tick "Add python.exe to PATH"^), then run this file again.
    pause
    exit /b 1
)

rem --- self-install / repair / validate everything -------------------------
%BOOT% setup_env.py
if errorlevel 1 (
    echo.
    echo Setup could not finish. Read the message above for the exact fix,
    echo then run this file again - downloads resume where they stopped.
    pause
    exit /b 1
)

rem --- launch (bundled ffmpeg is found automatically by the app) -----------
.venv\Scripts\python.exe app.py
if errorlevel 1 pause
