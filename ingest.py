"""Ingest: local file or URL → normalized video.mp4 + 16 kHz mono audio.wav.

URLs go through yt-dlp; everything AV goes through FFmpeg. If the source is
already h264+aac mp4 we remux instead of re-encoding (fast path)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from config import load_config
from errors import IngestError
from ffutil import probe, run_ffmpeg
from logutil import get_logger
from schemas import validate

log = get_logger("ingest")


def is_url(source: str) -> bool:
    return source.lower().startswith(("http://", "https://"))


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

    log.info("input: %s %dx%d %.2ffps %.1fs v=%s a=%s",
             src_file.name, info["width"], info["height"], info["fps"],
             info["duration"], info["vcodec"], info["acodec"] or "-")

    if (info["vcodec"] == "h264" and info["acodec"] == "aac"
            and src_file.suffix.lower() == ".mp4"):
        run_ffmpeg(["-i", src_file, "-c", "copy", "-movflags", "+faststart", video])
        log.info("normalize: remux fast path (already h264+aac mp4)")
    else:
        r = cfg["render"]
        args = ["-i", src_file,
                "-c:v", "libx264", "-preset", r["preset_intermediate"],
                "-crf", str(r["crf"]), "-pix_fmt", "yuv420p",
                "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2"]
        args += (["-c:a", "aac", "-b:a", r["audio_bitrate"]]
                 if info["has_audio"] else ["-an"])
        run_ffmpeg(args + ["-movflags", "+faststart", video])
        log.info("normalize: re-encoded to h264/aac")

    if info["has_audio"]:
        run_ffmpeg(["-i", video, "-vn", "-ac", "1", "-ar", "16000",
                    "-c:a", "pcm_s16le", audio])
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
        "format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080]/b",
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
