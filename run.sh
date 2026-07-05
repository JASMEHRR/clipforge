#!/usr/bin/env bash
# ClipForge bare-metal launcher (Linux/macOS). Requires python3.11 + ffmpeg.
set -euo pipefail
cd "$(dirname "$0")"

PY=python3.11
command -v "$PY" >/dev/null 2>&1 || { echo "python3.11 is required (MediaPipe wheels)"; exit 1; }
command -v ffmpeg >/dev/null 2>&1 || { echo "ffmpeg is required on PATH"; exit 1; }

if [ ! -d .venv ]; then
  "$PY" -m venv .venv
  ./.venv/bin/pip install --no-input --upgrade pip
  ./.venv/bin/pip install --no-input -r requirements.txt
fi

exec ./.venv/bin/python app.py
