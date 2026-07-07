"""ClipForge bootstrap / self-installer.

Runs with ANY system Python 3.8+ using ONLY the standard library (it executes
before pip dependencies exist). It makes a fresh Windows/Linux/macOS machine
ready to run ClipForge with zero manual setup:

  1.  Find (or guide installation of) Python 3.11.
  2.  Create/repair the .venv and install pinned requirements (with retries).
  3.  Find FFmpeg — or download a portable static build into tools/ffmpeg/
      (resumable, cached, integrity-checked).
  4.  Detect NVIDIA GPU / CUDA and report the acceleration mode (the app
      falls back to CPU automatically; this just tells the user up front).
  5.  Optionally prefetch the Whisper model (--prefetch-models) so the first
      job doesn't stall on a silent download (Hugging Face downloads resume
      natively).
  6.  Validate everything (imports, ffmpeg run, fonts, config) before launch.

Every failure produces a plain-language explanation and the minimum manual
action required. Exit code 0 = ready to launch.

Usage:  python setup_env.py [--prefetch-models] [--check-only] [--json]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TOOLS = ROOT / "tools"
DL_CACHE = ROOT / "cache" / "downloads"
IS_WIN = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"
VENV = ROOT / ".venv"
VENV_PY = VENV / ("Scripts/python.exe" if IS_WIN else "bin/python")

# Portable FFmpeg builds (Windows only; Linux/macOS use the package manager,
# with clear guidance if absent). BtbN builds are the de-facto standard
# portable distribution; "latest" is a stable redirect.
FFMPEG_WIN_URL = ("https://github.com/BtbN/FFmpeg-Builds/releases/latest/"
                  "download/ffmpeg-master-latest-win64-gpl.zip")

PYTHON_DL_PAGE = "https://www.python.org/downloads/release/python-3119/"


def log(msg: str) -> None:
    print(f"[setup] {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"[setup] WARNING: {msg}", flush=True)


def fail(msg: str, action: str) -> None:
    """Explain the problem and the minimum manual fix, then exit non-zero."""
    print(f"\n[setup] PROBLEM: {msg}", flush=True)
    print(f"[setup] WHAT TO DO: {action}\n", flush=True)
    sys.exit(1)


def _run(cmd: list[str], timeout: int = 900, **kw):
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout, **kw)


# --------------------------------------------------------------- downloads

def download(url: str, dest: Path, expected_sha256: str = "",
             label: str = "", max_attempts: int = 3) -> Path:
    """Resumable, cached download.

    - Completed files are cached in place and reused (never re-downloaded).
    - Interrupted downloads resume from the .part file via HTTP Range.
    - Optional sha256 integrity verification.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        if not expected_sha256 or _sha256(dest) == expected_sha256:
            log(f"{label or dest.name}: already downloaded (cached)")
            return dest
        warn(f"{dest.name}: cached file failed integrity check — redownloading")
        dest.unlink()

    part = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(1, max_attempts + 1):
        try:
            _fetch(url, part, label or dest.name)
            if expected_sha256 and _sha256(part) != expected_sha256:
                part.unlink(missing_ok=True)
                raise IOError("integrity check failed (sha256 mismatch)")
            part.replace(dest)
            return dest
        except (urllib.error.URLError, IOError, TimeoutError) as e:
            warn(f"download attempt {attempt}/{max_attempts} failed: {e}")
            time.sleep(min(30, 2 ** attempt))
    fail(f"could not download {label or url} after {max_attempts} attempts.",
         "Check your internet connection and run this again — the download "
         "will resume where it stopped.")
    raise AssertionError  # unreachable


def _fetch(url: str, part: Path, label: str) -> None:
    """One (resuming) download pass with progress + speed output."""
    have = part.stat().st_size if part.exists() else 0
    req = urllib.request.Request(url, headers={"User-Agent": "ClipForge-setup"})
    if have:
        req.add_header("Range", f"bytes={have}-")
    with urllib.request.urlopen(req, timeout=60) as r:
        if have and r.status == 200:      # server ignored Range: restart
            have = 0
        total = have + int(r.headers.get("Content-Length") or 0)
        mode = "ab" if have and r.status == 206 else "wb"
        t0, done = time.monotonic(), have
        last = 0.0
        with open(part, mode) as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                now = time.monotonic()
                if now - last > 1.0:
                    last = now
                    speed = (done - have) / max(1e-6, now - t0) / 1e6
                    pct = f"{done / total * 100:5.1f}%" if total else f"{done >> 20} MB"
                    print(f"\r[setup] {label}: {pct}  {speed:5.1f} MB/s   ",
                          end="", flush=True)
    print(flush=True)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 23):
            h.update(chunk)
    return h.hexdigest()


# ----------------------------------------------------------------- python

def find_python311() -> str | None:
    """Locate a Python 3.11 interpreter (returns command path) or None."""
    candidates = []
    if IS_WIN:
        candidates += [["py", "-3.11"], ["python3.11"], ["python"]]
    else:
        candidates += [["python3.11"], ["python3"], ["python"]]
    for cand in candidates:
        exe = shutil.which(cand[0])
        if not exe:
            continue
        try:
            r = _run(cand + ["-c", "import sys;print(sys.version_info[:2])"],
                     timeout=30)
            if r.returncode == 0 and "(3, 11)" in r.stdout:
                return " ".join(cand)
        except (OSError, subprocess.SubprocessError):
            continue
    return None


def ensure_python311() -> str:
    py = find_python311()
    if py:
        log(f"Python 3.11 found: {py}")
        return py
    if IS_WIN and shutil.which("winget"):
        log("Python 3.11 not found — installing via winget "
            "(this can take a few minutes)…")
        r = _run(["winget", "install", "-e", "--id", "Python.Python.3.11",
                  "--accept-source-agreements", "--accept-package-agreements",
                  "--silent"], timeout=1800)
        if r.returncode == 0:
            # winget updates PATH for new shells; probe common install dirs too
            for p in (Path(os.environ.get("LOCALAPPDATA", "")) / "Programs"
                      / "Python" / "Python311" / "python.exe",):
                if p.exists():
                    return str(p)
            py = find_python311()
            if py:
                return py
        warn(f"winget install did not complete cleanly: "
             f"{(r.stderr or r.stdout)[-300:]}")
    fail("Python 3.11 is required (MediaPipe wheels do not support other "
         "versions yet) and could not be installed automatically.",
         f"Install it from {PYTHON_DL_PAGE} (tick 'Add python.exe to PATH'), "
         "then run this setup again.")
    raise AssertionError


# ------------------------------------------------------------------- venv

def ensure_venv(py_cmd: str) -> None:
    healthy = VENV_PY.exists() and _run(
        [str(VENV_PY), "-c", "import sys"], timeout=30).returncode == 0
    if not healthy:
        if VENV.exists():
            warn("existing .venv is broken — recreating it")
            shutil.rmtree(VENV, ignore_errors=True)
        log("creating virtual environment (.venv)…")
        r = _run(py_cmd.split() + ["-m", "venv", str(VENV)], timeout=300)
        if r.returncode != 0:
            fail(f"could not create the virtual environment: {r.stderr[-300:]}",
                 "Delete the .venv folder if it exists and run setup again.")

    marker = VENV / ".requirements.sha256"
    req_hash = _sha256(ROOT / "requirements.txt")
    if marker.exists() and marker.read_text().strip() == req_hash:
        log("Python dependencies already installed (requirements unchanged)")
        return
    log("installing Python dependencies (first run downloads ~2 GB; "
        "pip caches everything, so re-runs are fast)…")
    for attempt in range(1, 4):
        r = subprocess.run([str(VENV_PY), "-m", "pip", "install", "--no-input",
                            "-r", str(ROOT / "requirements.txt")],
                           cwd=ROOT)
        if r.returncode == 0:
            marker.write_text(req_hash)
            return
        warn(f"pip install failed (attempt {attempt}/3) — retrying; already "
             "downloaded packages are cached and will not re-download")
        time.sleep(5)
    fail("Python dependencies could not be installed after 3 attempts.",
         "Check your internet connection and disk space, then run setup "
         "again. Progress is cached — it resumes where it stopped.")


# ------------------------------------------------------------------ ffmpeg

def bundled_ffmpeg_dir() -> Path | None:
    """tools/ffmpeg/**/bin containing ffmpeg + ffprobe, if present."""
    base = TOOLS / "ffmpeg"
    if not base.exists():
        return None
    exe = "ffmpeg.exe" if IS_WIN else "ffmpeg"
    for p in base.rglob(exe):
        if (p.parent / ("ffprobe.exe" if IS_WIN else "ffprobe")).exists():
            return p.parent
    return None


def ensure_ffmpeg() -> str:
    """Return a directory/PATH note. Order: system PATH → bundled → download
    (Windows) → package-manager guidance (Linux/macOS)."""
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        log("FFmpeg found on PATH")
        return "system"
    bundle = bundled_ffmpeg_dir()
    if bundle:
        log(f"using bundled FFmpeg: {bundle}")
        return str(bundle)
    if IS_WIN:
        log("FFmpeg not found — downloading a portable build (~90 MB, "
            "one time only)…")
        zip_path = download(FFMPEG_WIN_URL, DL_CACHE / "ffmpeg-win64.zip",
                            label="FFmpeg")
        dest = TOOLS / "ffmpeg"
        dest.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(zip_path) as z:
                z.testzip()
                z.extractall(dest)
        except zipfile.BadZipFile:
            zip_path.unlink(missing_ok=True)
            fail("the FFmpeg download was corrupted.",
                 "Run setup again — it will download a fresh copy.")
        bundle = bundled_ffmpeg_dir()
        if bundle:
            log(f"FFmpeg installed to {bundle}")
            return str(bundle)
        fail("the FFmpeg archive did not contain the expected files.",
             "Delete the tools/ffmpeg folder and run setup again.")
    hint = ("brew install ffmpeg" if IS_MAC
            else "sudo apt-get install -y ffmpeg   (or your distro's equivalent)")
    fail("FFmpeg is required and was not found.",
         f"Install it with:  {hint}   then run setup again.")
    raise AssertionError


# --------------------------------------------------------------------- gpu

def detect_gpu() -> dict:
    """Best-effort GPU/CUDA report. Never fails — the app runs on CPU."""
    info = {"nvidia_gpu": False, "gpu_name": "", "cuda_runtime": False,
            "mode": "CPU"}
    try:
        r = _run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                 timeout=20)
        if r.returncode == 0 and r.stdout.strip():
            info["nvidia_gpu"] = True
            info["gpu_name"] = r.stdout.strip().splitlines()[0]
    except (OSError, subprocess.SubprocessError):
        pass
    if info["nvidia_gpu"] and VENV_PY.exists():
        r = _run([str(VENV_PY), "-c",
                  "import ctranslate2;print(ctranslate2.get_cuda_device_count())"],
                 timeout=60)
        info["cuda_runtime"] = r.returncode == 0 and r.stdout.strip().isdigit() \
            and int(r.stdout.strip()) > 0
    if info["cuda_runtime"]:
        info["mode"] = f"GPU ({info['gpu_name']})"
        log(f"acceleration: GPU — {info['gpu_name']} (Whisper large-v3, NVENC "
            "if available)")
    elif info["nvidia_gpu"]:
        info["mode"] = "CPU (GPU present, CUDA runtime unavailable)"
        log(f"NVIDIA GPU detected ({info['gpu_name']}) but the CUDA runtime "
            "is not usable — running on CPU. Everything works; it is just "
            "slower. Installing the latest NVIDIA driver usually enables GPU "
            "mode.")
    else:
        log("no NVIDIA GPU detected — running on CPU (Whisper small/int8). "
            "Everything works; long videos just take longer.")
    return info


# ----------------------------------------------------------------- models

def prefetch_models() -> None:
    """Download the Whisper model matching this machine now, instead of
    silently during the first job. Hugging Face downloads resume natively."""
    code = (
        "import sys; sys.path.insert(0, r'%s')\n"
        "from config import load_config\n"
        "import transcribe\n"
        "cfg = load_config(check_python=False)\n"
        "if transcribe.model_cached(cfg):\n"
        "    print('model already cached')\n"
        "else:\n"
        "    name, device, compute = transcribe.model_config(cfg)\n"
        "    print(f'downloading Whisper {name} ({device}/{compute}) — "
        "resumable')\n"
        "    from faster_whisper import WhisperModel\n"
        "    from pathlib import Path\n"
        "    WhisperModel(name, device='cpu', compute_type='int8',\n"
        "                 download_root=str(Path(r'%s') / "
        "cfg['whisper']['model_dir']))\n"
        "    print('model ready')\n" % (ROOT, ROOT))
    r = subprocess.run([str(VENV_PY), "-c", code], cwd=ROOT)
    if r.returncode != 0:
        warn("model prefetch failed — the model will download automatically "
             "on the first job instead (the job shows download progress).")


# -------------------------------------------------------------- validation

def validate(ffmpeg_where: str) -> list[str]:
    """Preflight: everything a job needs must work before we say 'ready'."""
    problems: list[str] = []
    env = os.environ.copy()
    if ffmpeg_where not in ("system",):
        env["PATH"] = ffmpeg_where + os.pathsep + env.get("PATH", "")

    r = _run(["ffmpeg", "-version"], timeout=30, env=env) \
        if shutil.which("ffmpeg", path=env["PATH"]) else None
    if not r or r.returncode != 0:
        problems.append("FFmpeg does not run")
    imports = ("yaml, jsonschema, numpy, cv2, mediapipe, faster_whisper, "
               "gradio, yt_dlp, scenedetect")
    r = _run([str(VENV_PY), "-c", f"import {imports}"], timeout=180)
    if r.returncode != 0:
        problems.append(f"a Python dependency fails to import: "
                        f"{r.stderr.strip().splitlines()[-1][:200]}")
    for font in ("Montserrat-Regular.ttf", "Montserrat-ExtraBold.ttf"):
        if not (ROOT / "assets" / "fonts" / font).exists():
            problems.append(f"bundled font missing: {font}")
    r = _run([str(VENV_PY), "-c",
              "from config import load_config; load_config()"], cwd=ROOT,
             timeout=60)
    if r.returncode != 0:
        problems.append(f"config.yaml failed to load: "
                        f"{r.stderr.strip().splitlines()[-1][:200]}")
    return problems


# ------------------------------------------------------------------- main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="ClipForge self-installer")
    ap.add_argument("--prefetch-models", action="store_true",
                    help="download the Whisper model now (resumable)")
    ap.add_argument("--check-only", action="store_true",
                    help="report status without installing anything")
    ap.add_argument("--json", action="store_true",
                    help="emit a machine-readable status report")
    a = ap.parse_args(argv)

    if a.check_only:
        report = {
            "python311": bool(find_python311()),
            "venv": VENV_PY.exists(),
            "ffmpeg": bool(shutil.which("ffmpeg") or bundled_ffmpeg_dir()),
            "gpu": detect_gpu(),
        }
        print(json.dumps(report, indent=2) if a.json else report)
        return 0

    log("── ClipForge setup ──────────────────────────────────")
    py = ensure_python311()
    ensure_venv(py)
    ffmpeg_where = ensure_ffmpeg()
    detect_gpu()
    if a.prefetch_models:
        prefetch_models()

    problems = validate(ffmpeg_where)
    if problems:
        fail("setup finished but validation found issues:\n  - "
             + "\n  - ".join(problems),
             "Run setup again; if the problem persists, open an issue with "
             "the message above.")
    log("everything is ready ✔  — launching is safe")
    return 0


if __name__ == "__main__":
    sys.exit(main())
