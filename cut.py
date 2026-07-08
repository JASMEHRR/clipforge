"""Frame-accurate clip cutting. Always re-encodes (never stream-copy) so the
clip starts exactly on the requested time, not the previous keyframe."""
from __future__ import annotations

import argparse
from pathlib import Path

from config import load_config
from errors import CutError
from ffutil import probe, run_ffmpeg, video_encode_args
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
                "-t", f"{end - start:.3f}"]
               + video_encode_args(cfg)
               + ["-c:a", "aac", "-b:a", r["audio_bitrate"],
                  "-movflags", "+faststart", out_path])
    got = probe(out_path)["duration"]
    want = end - start
    if abs(got - want) > 1.5:
        raise CutError(f"cut duration off: wanted {want:.2f}s got {got:.2f}s")
    log.info("cut %.2f–%.2f (%.1fs) -> %s", start, end, got, out_path.name)
    return out_path


def cut_segments(video_path: str | Path, segments: list[list[float]],
                 out_path: str | Path, cfg: dict | None = None) -> Path:
    """Extract an ordered list of source spans and concat them into ONE clip
    (single re-encode via the concat filter). A short audio fade at every
    internal join removes the click of a hard cut WITHOUT overlapping audio, so
    the EditPlan's word-timeline remap stays exact. Single-segment lists take
    the cut_clip fast path unchanged."""
    cfg = cfg or load_config()
    video_path, out_path = Path(video_path), Path(out_path)
    if not segments:
        raise CutError("no segments to cut")
    if len(segments) == 1:
        return cut_clip(video_path, segments[0][0], segments[0][1], out_path, cfg)
    if not video_path.exists():
        raise CutError(f"video not found: {video_path}")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    r = cfg["render"]
    cf = max(0.0, float(cfg["style"].get("crossfade_ms", 30)) / 1000.0)
    parts, labels = [], []
    total = 0.0
    for i, (s, e) in enumerate(segments):
        if e <= s:
            raise CutError(f"invalid segment {i}: start={s} end={e}")
        d = e - s
        total += d
        parts.append(f"[0:v]trim=start={s:.3f}:end={e:.3f},setpts=PTS-STARTPTS[v{i}]")
        af = f"[0:a]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS"
        if cf > 0 and i > 0:                       # fade in at internal joins
            af += f",afade=t=in:st=0:d={cf:.3f}"
        if cf > 0 and i < len(segments) - 1:       # fade out into internal joins
            af += f",afade=t=out:st={max(0.0, d - cf):.3f}:d={cf:.3f}"
        af += f"[a{i}]"
        parts.append(af)
        labels.append(f"[v{i}][a{i}]")            # concat wants v,a interleaved
    n = len(segments)
    parts.append("".join(labels) + f"concat=n={n}:v=1:a=1[vout][aout]")
    filtergraph = ";".join(parts)

    run_ffmpeg(["-i", video_path, "-filter_complex", filtergraph,
                "-map", "[vout]", "-map", "[aout]"]
               + video_encode_args(cfg)
               + ["-c:a", "aac", "-b:a", r["audio_bitrate"],
                  "-movflags", "+faststart", out_path])
    got = probe(out_path)["duration"]
    if abs(got - total) > 1.5:
        raise CutError(f"segment concat off: wanted {total:.2f}s got {got:.2f}s")
    log.info("cut %d segments (%.1fs) -> %s", n, got, out_path.name)
    return out_path


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="smoke: cut a clip")
    ap.add_argument("video")
    ap.add_argument("start", type=float)
    ap.add_argument("end", type=float)
    ap.add_argument("--out", default="output/_smoke_cut.mp4")
    a = ap.parse_args()
    print(cut_clip(a.video, a.start, a.end, a.out))
