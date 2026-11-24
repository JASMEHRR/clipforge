@echo off
rem ClipForge nightly run. Nothing is published: every clip waits in approvals.
rem   1. one money explainer + its reels   ("money" workspace)
rem   2. the content loop: learn from last week, two reels from today's top stories, post packages for Muse (loop.py)
rem Registered in Windows Task Scheduler as "ClipForge Nightly".
rem Kids channel paused 2026-11-10 (PJ): delete "rem " on the create.py line to bring it back.
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if not exist output mkdir output
rem .venv\Scripts\python.exe create.py --count 1 >> output\nightly.log 2>&1
echo ==== %DATE% %TIME% money ==== >> output\nightly.log
.venv\Scripts\python.exe explain.py --count 1 >> output\nightly.log 2>&1
echo ==== %DATE% %TIME% trending ==== >> output\nightly.log
.venv\Scripts\python.exe loop.py cycle --make 2 >> output\nightly.log 2>&1
echo ==== %DATE% %TIME% done ==== >> output\nightly.log
