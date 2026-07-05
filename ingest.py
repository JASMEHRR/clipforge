"""Ingest: local file or URL → normalized video.mp4 + 16 kHz mono audio.wav.

URLs go through yt-dlp; everything AV goes through FFmpeg. If the source is
already h264+aac mp4 we remux instead of re-encoding (fast path)."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from config import load_config
from errors import IngestError
from ffutil import probe, run_ffmpeg
from logutil import get_logger
from schemas import validate

log = get_logger("ingest")


def is_url(source: str) -> bool:
    return source.lower().startswith(("http://", "https://"))


def normalize_plan(info: dict, suffix: str) -> str:
    """Decide how to normalize a probed input (pure; unit-tested):
    - 'remux'      — h264 + yuv420p + aac in an mp4-family container:
                     stream-copy both (fast, no quality loss)
    - 'audio_only' — video already fine but audio missing/non-aac:
                     copy video, transcode/generate audio only
    - 'reencode'   — anything else (vp9/av1/hevc, exotic pixel formats,
                     non-mp4 containers whose remux can be unreliable)
    """
    video_ok = (info.get("vcodec") == "h264"
                and info.get("pix_fmt", "") in ("yuv420p", "yuvj420p"))
    container_ok = suffix.lower() in (".mp4", ".m4v", ".mov")
    if not (video_ok and container_ok):
        return "reencode"
    if info.get("has_audio") and info.get("acodec") == "aac":
        return "remux"
    return "audio_only"


def ffmpeg_timeout(cfg: dict, duration: float) -> int:
    """Timeout scaled to input length: max(base, duration * per_second)."""
    f = cfg.get("ffmpeg", {})
    base = int(f.get("timeout_base_seconds", 1800))
    per = float(f.get("timeout_per_input_second", 3.0))
    return int(max(base, duration * per))


def demo_cap_seconds() -> float | None:
    """CLIPFORGE_DEMO=1 caps ingest length (hosted demo Spaces)."""
    if os.environ.get("CLIPFORGE_DEMO") == "1":
        return float(os.environ.get("CLIPFORGE_DEMO_MAX_SECONDS", "300"))
    return None


def ingest(source: str, job_dir: str | Path, cfg: dict | None = None) -> dict:
    """Returns IngestInfo dict (schema-validated); writes ingest_info.json."""
    cfg = cfg or load_config()
    job_dir = Path(job_dir)
    job_dir.mkdir(parents=True, exist_ok=True)
    video = job_dir / "video.mp4"
    audio = job_dir / "audio.wav"

    if is_url(source):
        src_file = _download_url(source, job_dir)
        source_type = "url"
    else:
        src_file = Path(source)
        if not src_file.exists():
            raise IngestError(f"input file not found: {source}")
        source_type = "file"

    try:
        info = probe(src_file)
    except Exception as e:
        raise IngestError(f"could not probe input: {src_file}", detail=str(e)) from e

    log.info("input: %s %dx%d %.2ffps %.1fs v=%s(%s) a=%s",
             src_file.name, info["width"], info["height"], info["fps"],
             info["duration"], info["vcodec"], info.get("pix_fmt", "?"),
             info["acodec"] or "-")

    timeout = ffmpeg_timeout(cfg, info["duration"])
    cap = demo_cap_seconds()
    limit = ["-t", f"{cap:.0f}"] if cap and info["duration"] > cap else []
    if limit:
        log.warning("DEMO mode: input capped to %.0fs (of %.0fs)",
                    cap, info["duration"])

    plan = normalize_plan(info, src_file.suffix)
    r = cfg["render"]
    if plan == "remux":
        run_ffmpeg(["-i", src_file] + limit
                   + ["-c", "copy", "-movflags", "+faststart", video],
                   timeout=timeout)
        log.info("normalize: remux fast path (h264/yuv420p/aac, no re-encode)")
    elif plan == "audio_only":
        args = ["-i", src_file] + limit + ["-c:v", "copy"]
        args += (["-c:a", "aac", "-b:a", r["audio_bitrate"]]
                 if info["has_audio"] else ["-an"])
        run_ffmpeg(args + ["-movflags", "+faststart", video], timeout=timeout)
        log.info("normalize: video copied, audio %s",
                 "transcoded to aac" if info["has_audio"] else "absent")
    else:
        args = ["-i", src_file] + limit + [
            "-c:v", "libx264", "-preset", "veryfast",
            "-crf", str(r["crf"]), "-pix_fmt", "yuv420p",
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2"]
        args += (["-c:a", "aac", "-b:a", r["audio_bitrate"]]
                 if info["has_audio"] else ["-an"])
        run_ffmpeg(args + ["-movflags", "+faststart", video], timeout=timeout,
                   progress_label=f"normalize {src_file.name}")
        log.info("normalize: re-encoded to h264/aac (source was %s/%s)",
                 info["vcodec"], info.get("pix_fmt") or "?")

    if info["has_audio"]:
        run_ffmpeg(["-i", video, "-vn", "-ac", "1", "-ar", "16000",
                    "-c:a", "pcm_s16le", audio], timeout=timeout)
    else:
        # Silent track keeps downstream contracts intact (empty transcript).
        run_ffmpeg(["-f", "lavfi", "-i",
                    f"anullsrc=r=16000:cl=mono:d={max(info['duration'], 1)}",
                    "-c:a", "pcm_s16le", audio])
        log.warning("no audio stream — generated silent track")

    out_info = probe(video)
    result = {
        "source": str(source),
        "source_type": source_type,
        "duration": out_info["duration"],
        "width": out_info["width"],
        "height": out_info["height"],
        "fps": out_info["fps"],
        "video_path": str(video),
        "audio_path": str(audio),
    }
    validate(result, "ingest_info")
    (job_dir / "ingest_info.json").write_text(json.dumps(result, indent=2),
                                              encoding="utf-8")
    return result


def _download_url(url: str, job_dir: Path) -> Path:
    try:
        import yt_dlp
    except ImportError as e:
        raise IngestError("yt-dlp not installed", detail=str(e)) from e
    out_tmpl = str(job_dir / "source.%(ext)s")
    opts = {
        # Prefer H.264 (avc1) so normalization is a remux, not a re-encode;
        # fall back progressively. 1080p cap keeps CPU paths sane.
        "format": ("bv*[vcodec^=avc1][height<=1080]+ba[ext=m4a]"
                   "/b[vcodec^=avc1][height<=1080][ext=mp4]"
                   "/bv*[height<=1080][ext=mp4]+ba"
                   "/b[height<=1080]/b"),
        "outtmpl": out_tmpl,
        "merge_output_format": "mp4",
        "quiet": True,
        "noprogress": True,
        "noplaylist": True,
        "retries": 3,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as e:  # noqa: BLE001 — yt-dlp raises many types
        raise IngestError(f"download failed: {url}", detail=str(e)[:500]) from e
    files = sorted(job_dir.glob("source.*"), key=lambda p: -p.stat().st_size)
    if not files:
        raise IngestError(f"yt-dlp produced no file for {url}")
    return files[0]


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="smoke: ingest a file or URL")
    ap.add_argument("source")
    ap.add_argument("--job-dir", default="output/_smoke_ingest")
    a = ap.parse_args()
    print(json.dumps(ingest(a.source, a.job_dir), indent=2))
