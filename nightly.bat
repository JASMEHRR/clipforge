@echo off
rem ClipForge nightly run. Nothing is published: every clip waits in approvals.
rem   1. one money explainer + its reels   ("money" workspace)
rem   2. the content loop: learn from last week, two reels from today's top stories, post packages for Muse (loop.py)
rem Registered in Windows Task Scheduler as "ClipForge Nightly".
rem Kids channel paused 2026-11-10 (PJ): delete "rem " on the create.py line to bring it back.
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if not exist output mkdir output
if not exist cache mkdir cache
rem One render at a time: wait (up to 2 hours) while the laptop bridge is rendering,
rem and tell the bridge the nightly run is on (cache\nightly.running).
set /a WAITED=0
:waitbridge
if exist cache\bridge-render.lock if %WAITED% LSS 120 (
  echo ==== %DATE% %TIME% waiting for the laptop bridge render ==== >> output\nightly.log
  timeout /t 60 /nobreak > nul
  set /a WAITED+=1
  goto waitbridge
)
echo %DATE% %TIME% > cache\nightly.running
rem .venv\Scripts\python.exe create.py --count 1 >> output\nightly.log 2>&1
echo ==== %DATE% %TIME% money ==== >> output\nightly.log
.venv\Scripts\python.exe explain.py --count 1 >> output\nightly.log 2>&1
echo ==== %DATE% %TIME% trending ==== >> output\nightly.log
.venv\Scripts\python.exe loop.py cycle --make 2 >> output\nightly.log 2>&1
echo ==== %DATE% %TIME% done ==== >> output\nightly.log
del cache\nightly.running 2> nul
