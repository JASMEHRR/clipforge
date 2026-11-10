@echo off
rem ClipForge nightly run: one kids video (Create, "kids" workspace) then one
rem money explainer (Explainer, "money" workspace), each with its Shorts.
rem Nothing is published: every clip waits in that workspace's approvals.
rem Registered in Windows Task Scheduler as "ClipForge Nightly".
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if not exist output mkdir output
echo ==== %DATE% %TIME% kids ==== >> output\nightly.log
.venv\Scripts\python.exe create.py --count 1 >> output\nightly.log 2>&1
echo ==== %DATE% %TIME% money ==== >> output\nightly.log
.venv\Scripts\python.exe explain.py --count 1 >> output\nightly.log 2>&1
echo ==== %DATE% %TIME% done ==== >> output\nightly.log
