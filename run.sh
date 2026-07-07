#!/usr/bin/env bash
# ClipForge zero-setup launcher (Linux/macOS). setup_env.py finds or installs
# everything it can and explains the one command needed when it can't
# (e.g. `sudo apt-get install -y ffmpeg`).
set -euo pipefail
cd "$(dirname "$0")"

BOOT=""
for c in python3.11 python3 python; do
  command -v "$c" >/dev/null 2>&1 && { BOOT="$c"; break; }
done
if [ -z "$BOOT" ]; then
  echo "Python 3 is required. Install it (e.g. sudo apt-get install -y python3.11 python3.11-venv) and run again."
  exit 1
fi

"$BOOT" setup_env.py

exec ./.venv/bin/python app.py
