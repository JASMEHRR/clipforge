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

.venv\Scripts\python.exe app.py
