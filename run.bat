@echo off
rem ClipForge bare-metal launcher (Windows). Requires Python 3.11 + ffmpeg.
cd /d "%~dp0"

where ffmpeg >nul 2>&1 || (echo ffmpeg is required on PATH & exit /b 1)
py -3.11 --version >nul 2>&1 || (echo Python 3.11 is required ^(winget install Python.Python.3.11^) & exit /b 1)

if not exist .venv (
  py -3.11 -m venv .venv
  .venv\Scripts\python.exe -m pip install --no-input --upgrade pip
  .venv\Scripts\pip install --no-input -r requirements.txt
)

rem The UI auto-opens its window on start (config ui.auto_open / ui.window_mode);
rem this console stays open for logs. No further terminal interaction needed.
rem "run.bat new" launches the new UI (FastAPI, port 7861) while it is being
rem built; the default stays the current UI until the new one reaches parity.
if "%1"=="new" (
  .venv\Scripts\python.exe -m server.main
) else (
  .venv\Scripts\python.exe app.py
)
