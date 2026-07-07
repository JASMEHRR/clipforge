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


def run_ffmpeg(args: list[str], timeout: int = 1800,
               progress_label: str | None = None) -> None:
    """Run ffmpeg -y -v error <args>; raise FFmpegError with stderr tail.

    With `progress_label`, ffmpeg's -progress stream is read live and an
    out_time line is logged every ~30s so long re-encodes look alive
    instead of hung."""
    cmd = ["ffmpeg", "-y", "-v", "error"] + [str(a) for a in args]
    log.info("ffmpeg %s", " ".join(cmd[3:6]) + (" ..." if len(cmd) > 6 else ""))
    try:
        if progress_label:
            _run_with_progress(cmd, timeout, progress_label)
            return
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise FFmpegError(f"ffmpeg timed out after {timeout}s") from e
    except FileNotFoundError as e:
        raise FFmpegError("ffmpeg not found on PATH") from e
    if r.returncode != 0:
        raise FFmpegError(f"ffmpeg exited {r.returncode}",
                          detail=(r.stderr or "")[-1000:])


def _run_with_progress(cmd: list[str], timeout: int, label: str) -> None:
    import time
    # -progress pipe:1 emits key=value blocks; -stats_period throttles them.
    cmd = cmd[:3] + ["-stats_period", "30", "-progress", "pipe:1"] + cmd[3:]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + timeout
    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            if time.monotonic() > deadline:
                proc.kill()
                raise FFmpegError(f"ffmpeg timed out after {timeout}s ({label})")
            if line.startswith("out_time="):
                log.info("%s: encoded up to %s", label, line.strip()[9:])
        proc.wait(timeout=max(30, deadline - time.monotonic()))
    except subprocess.TimeoutExpired as e:
        proc.kill()
        raise FFmpegError(f"ffmpeg timed out after {timeout}s ({label})") from e
    if proc.returncode != 0:
        stderr = proc.stderr.read() if proc.stderr else ""
        raise FFmpegError(f"ffmpeg exited {proc.returncode}",
                          detail=stderr[-1000:])


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
        "pix_fmt": v.get("pix_fmt", ""),
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


_nvenc: bool | None = None


def nvenc_available() -> bool:
    """NVIDIA GPU present, ffmpeg built with h264_nvenc, AND the driver can
    actually initialize it (cached). The compiled-in encoder list says
    nothing about whether the installed driver meets NVENC's own minimum
    version requirement — that only surfaces as a runtime "minimum required
    Nvidia driver" error from ffmpeg, so a real 1-frame smoke encode is the
    only reliable check. Falls back to libx264 when it can't."""
    global _nvenc
    if _nvenc is None:
        try:
            gpu = subprocess.run(["nvidia-smi", "-L"], capture_output=True,
                                 timeout=15).returncode == 0
            enc = gpu and "h264_nvenc" in subprocess.run(
                ["ffmpeg", "-hide_banner", "-encoders"], capture_output=True,
                text=True, timeout=30).stdout
            _nvenc = bool(enc) and subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                 "-i", "color=black:s=64x64", "-frames:v", "1",
                 "-c:v", "h264_nvenc", "-f", "null", "-"],
                capture_output=True, timeout=30).returncode == 0
        except (OSError, subprocess.SubprocessError):
            _nvenc = False
        log.info("encoder: %s", "h264_nvenc (GPU)" if _nvenc else "libx264 (CPU)")
    return _nvenc


def video_encode_args(cfg: dict, final: bool = False) -> list[str]:
    """Encoder argument set: NVENC when config allows and hardware exists,
    otherwise libx264 with the configured preset/crf."""
    r = cfg["render"]
    force_cpu = r.get("compute", "auto") == "cpu"
    if (not force_cpu and r.get("use_nvenc", "auto") == "auto"
            and nvenc_available()):
        return ["-c:v", "h264_nvenc", "-preset", "p5" if final else "p3",
                "-cq", str(r["crf"]), "-pix_fmt", "yuv420p"]
    preset = r["preset_final"] if final else r["preset_intermediate"]
    return ["-c:v", "libx264", "-preset", preset, "-crf", str(r["crf"]),
            "-pix_fmt", "yuv420p"]


def filter_path(p: str | Path) -> str:
    """Escape a filesystem path for use inside an ffmpeg filter argument
    (subtitles/ass/sendcmd filenames) — Windows drive colons and backslashes."""
    s = str(Path(p).resolve()).replace("\\", "/")
    return s.replace(":", "\\:")
