"""Frame-accurate clip cutting. Always re-encodes (never stream-copy) so the
clip starts exactly on the requested time, not the previous keyframe."""
from __future__ import annotations

import argparse
from pathlib import Path

from config import load_config
from errors import CutError
from ffutil import probe, run_ffmpeg
from logutil import get_logger

log = get_logger("cut")


def cut_clip(video_path: str | Path, start: float, end: float,
             out_path: str | Path, cfg: dict | None = None) -> Path:
    cfg = cfg or load_config()
    video_path, out_path = Path(video_path), Path(out_path)
    if not video_path.exists():
        raise CutError(f"video not found: {video_path}")
    if end <= start:
        raise CutError(f"invalid range: start={start} end={end}")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    r = cfg["render"]
    # -ss before -i: fast keyframe seek, then decode — frame-accurate because
    # we re-encode. -t is the clip duration.
    run_ffmpeg(["-ss", f"{start:.3f}", "-i", video_path,
                "-t", f"{end - start:.3f}",
                "-c:v", "libx264", "-preset", r["preset_intermediate"],
                "-crf", str(r["crf"]), "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", r["audio_bitrate"],
                "-movflags", "+faststart", out_path])
    got = probe(out_path)["duration"]
    want = end - start
    if abs(got - want) > 1.5:
        raise CutError(f"cut duration off: wanted {want:.2f}s got {got:.2f}s")
    log.info("cut %.2f–%.2f (%.1fs) -> %s", start, end, got, out_path.name)
    return out_path


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="smoke: cut a clip")
    ap.add_argument("video")
    ap.add_argument("start", type=float)
    ap.add_argument("end", type=float)
    ap.add_argument("--out", default="output/_smoke_cut.mp4")
    a = ap.parse_args()
    print(cut_clip(a.video, a.start, a.end, a.out))
