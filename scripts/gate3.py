"""Gate 3 / Final QA driver — run with the CLEAN venv's python, keyless:

1. pytest green
2. ffmpeg verified against the pin
3. Docker artifacts statically validated (no daemon on host → noted)
4. caches cleared (except whisper models), then FULL gate-1 verification
   from scratch: pipeline --sample --provider mock --debug + scripts/gate1.py
5. FULL gate-2 verification: scripts/gate2.py

Prints GATE 3 PASSED/FAILED. Intended to run detached; see output/gate3.log.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.pop("GEMINI_API_KEY", None)

from config import ROOT, load_config  # noqa: E402

failures: list[str] = []
cfg = load_config()
PY = sys.executable
print(f"gate3 running under: {PY} (python {sys.version.split()[0]})")


def step(name: str, args: list[str], timeout: int = 3600) -> bool:
    print(f"\n=== {name} ===", flush=True)
    r = subprocess.run(args, cwd=ROOT, timeout=timeout)
    if r.returncode != 0:
        failures.append(f"{name} failed (exit {r.returncode})")
        return False
    return True


# 1. pytest
step("pytest", [PY, "-m", "pytest", "tests/", "-q"])

# 2. ffmpeg pin
from ffutil import verify_ffmpeg  # noqa: E402
try:
    print("ffmpeg:", verify_ffmpeg(cfg))
except Exception as e:  # noqa: BLE001
    failures.append(f"ffmpeg verification failed: {e}")

# 3. docker static validation
import yaml  # noqa: E402
try:
    comp = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    assert "clipforge" in comp["services"]
    df = (ROOT / "Dockerfile").read_text()
    assert df.startswith("# ClipForge") and "FROM python:3.11" in df
    assert "ffmpeg" in df and "CMD" in df
    print("docker: static validation OK (no daemon on host — container run "
          "pending manual verification, PROGRESS.md Known Issues)")
except Exception as e:  # noqa: BLE001
    failures.append(f"docker static validation failed: {e}")

# 4. fresh keyless gate-1: clear caches (keep whisper models), full run
for sub in ("transcripts", "scenes"):
    shutil.rmtree(ROOT / "cache" / sub, ignore_errors=True)
print("caches cleared (transcripts, scenes) — models kept")

ok = step("pipeline --sample --provider mock --debug (from scratch)",
          [PY, "pipeline.py", "--sample", "--provider", "mock", "--debug"])
if ok:
    step("gate1 verification", [PY, "scripts/gate1.py"])

# 5. full gate-2
step("gate2 verification", [PY, "scripts/gate2.py"])

print()
if failures:
    print("GATE 3 FAILED:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("GATE 3 PASSED")
