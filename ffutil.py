"""FFmpeg helpers: run with captured errors, probe, filter-path escaping.
ALL AV operations in the project go through ffmpeg/ffprobe via this module."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from errors import ClipForgeError
from logutil import get_logger

log = get_logger("ffmpeg")


class FFmpegError(ClipForgeError):
    stage = "ffmpeg"


def run_ffmpeg(args: list[str], timeout: int = 1800) -> None:
    """Run ffmpeg -y -v error <args>; raise FFmpegError with stderr tail."""
    cmd = ["ffmpeg", "-y", "-v", "error"] + [str(a) for a in args]
    log.info("ffmpeg %s", " ".join(cmd[3:6]) + (" ..." if len(cmd) > 6 else ""))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise FFmpegError(f"ffmpeg timed out after {timeout}s") from e
    except FileNotFoundError as e:
        raise FFmpegError("ffmpeg not found on PATH") from e
    if r.returncode != 0:
        raise FFmpegError(f"ffmpeg exited {r.returncode}",
                          detail=(r.stderr or "")[-1000:])


def probe(path: str | Path) -> dict:
    """ffprobe → {duration, width, height, fps, vcodec, acodec, has_audio}."""
    cmd = ["ffprobe", "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", str(path)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise FFmpegError(f"ffprobe failed for {path}",
                          detail=(r.stderr or "")[-500:])
    data = json.loads(r.stdout)
    v = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in data["streams"] if s["codec_type"] == "audio"), None)
    if v is None:
        raise FFmpegError(f"no video stream in {path}")
    num, _, den = (v.get("avg_frame_rate") or "30/1").partition("/")
    fps = (float(num) / float(den)) if den and float(den) else 30.0
    if fps <= 0 or fps > 240:
        fps = 30.0
    return {
        "duration": float(data["format"].get("duration", 0.0)),
        "width": int(v["width"]),
        "height": int(v["height"]),
        "fps": fps,
        "vcodec": v.get("codec_name", ""),
        "acodec": (a or {}).get("codec_name", ""),
        "has_audio": a is not None,
    }


def ffmpeg_version() -> str:
    r = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True,
                       timeout=30)
    first = (r.stdout or "").splitlines()[0] if r.stdout else ""
    return first.split(" ")[2] if len(first.split(" ")) > 2 else "unknown"


def verify_ffmpeg(cfg: dict) -> str:
    """Verify ffmpeg exists and matches the pinned version (warn on drift
    unless ffmpeg.require_exact)."""
    ver = ffmpeg_version()
    pinned = cfg.get("ffmpeg", {}).get("pinned_version", "")
    if pinned and not ver.startswith(pinned):
        msg = f"ffmpeg version drift: found {ver}, pinned {pinned}"
        if cfg.get("ffmpeg", {}).get("require_exact"):
            raise FFmpegError(msg)
        log.warning(msg)
    else:
        log.info("ffmpeg %s OK (pinned %s)", ver, pinned or "none")
    return ver


def filter_path(p: str | Path) -> str:
    """Escape a filesystem path for use inside an ffmpeg filter argument
    (subtitles/ass/sendcmd filenames) — Windows drive colons and backslashes."""
    s = str(Path(p).resolve()).replace("\\", "/")
    return s.replace(":", "\\:")
