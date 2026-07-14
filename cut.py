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

_EPS = 0.05  # clamp buffer so a trim never lands exactly on/past EOF


def _clamp_to_source(video_path: Path, start: float, end: float) -> tuple[float, float]:
    """Clamp [start, end) to the source's real probed duration. Callers
    (style_refiner's extend_forward, EditPlan bounds) don't verify against
    actual container duration — this is the one place both cut paths share,
    so clamping here catches every producer instead of patching each one."""
    duration = probe(video_path)["duration"]
    end = min(end, duration - _EPS)
    return start, end


def cut_clip(video_path: str | Path, start: float, end: float,
             out_path: str | Path, cfg: dict | None = None) -> Path:
    cfg = cfg or load_config()
    video_path, out_path = Path(video_path), Path(out_path)
    if not video_path.exists():
        raise CutError(f"video not found: {video_path}")
    if end <= start:
        raise CutError(f"invalid range: start={start} end={end}")
    start, end = _clamp_to_source(video_path, start, end)
    if end <= start:
        raise CutError(f"range entirely past source duration: start={start} end={end}")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    r = cfg["render"]
    # -ss before -i: fast keyframe seek, then decode — frame-accurate because
    # we re-encode. -t is the clip duration.
    run_ffmpeg(["-ss", f"{start:.3f}", "-i", video_path,
                "-t", f"{end - start:.3f}"]
               + video_encode_args(cfg)
               + ["-c:a", "aac", "-b:a", r["audio_bitrate"],
                  "-movflags", "+faststart", out_path])
    info = probe(out_path)  # already raises CutError-equivalent FFmpegError if no video stream
    if not info.get("has_audio"):
        raise CutError(f"cut produced no audio stream: {out_path}")
    got = info["duration"]
    want = end - start
    if abs(got - want) > 1.5:
        raise CutError(f"cut duration off: wanted {want:.2f}s got {got:.2f}s")
    log.info("cut %.2f–%.2f (%.1fs) -> %s", start, end, got, out_path.name)
    return out_path


def expand_ramps(segments: list, speed_ramps: list[dict] | None) -> list[tuple]:
    """Split segments at speed-ramp boundaries → ordered (start, end, rate)
    subsegments (rate 1.0 = normal). Pure; ramps outside any segment are
    ignored, zero-length slivers dropped."""
    ramps = sorted(speed_ramps or [], key=lambda r: r["start"])
    out: list[tuple] = []
    for s, e in segments:
        pos = s
        for rp in ramps:
            rs, re_ = max(rp["start"], s), min(rp["end"], e)
            if re_ - rs < 0.01 or rs < pos - 1e-6:
                continue
            if rs - pos > 0.01:
                out.append((pos, rs, 1.0))
            out.append((rs, re_, float(rp["rate"])))
            pos = re_
        if e - pos > 0.01:
            out.append((pos, e, 1.0))
    return out


def cut_segments(video_path: str | Path, segments: list[list[float]],
                 out_path: str | Path, cfg: dict | None = None,
                 speed_ramps: list[dict] | None = None) -> Path:
    """Extract an ordered list of source spans and concat them into ONE clip
    (single re-encode via the concat filter). A short audio fade at every
    internal join removes the click of a hard cut WITHOUT overlapping audio, so
    the EditPlan's word-timeline remap stays exact. Single-segment lists take
    the cut_clip fast path unchanged.

    `speed_ramps` ({start, end, rate} in source time, word-free gaps only)
    play those sub-spans faster: video setpts/rate + audio atempo. The
    EditPlan's word remap already accounts for the time saved."""
    cfg = cfg or load_config()
    video_path, out_path = Path(video_path), Path(out_path)
    if not segments:
        raise CutError("no segments to cut")
    if len(segments) == 1 and not speed_ramps:
        return cut_clip(video_path, segments[0][0], segments[0][1], out_path, cfg)
    if not video_path.exists():
        raise CutError(f"video not found: {video_path}")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    duration = probe(video_path)["duration"]
    clamped = []
    for s, e in segments:
        e = min(e, duration - _EPS)
        if e > s:
            clamped.append((s, e))
    if not clamped:
        raise CutError("all segments out of source bounds "
                       f"(source duration {duration:.2f}s)")

    subs = expand_ramps(clamped, speed_ramps)
    if not subs:
        raise CutError("no renderable subsegments")

    r = cfg["render"]
    cf = max(0.0, float(cfg["style"].get("crossfade_ms", 30)) / 1000.0)
    parts, labels = [], []
    total = 0.0
    for i, (s, e, rate) in enumerate(subs):
        if e <= s:
            raise CutError(f"invalid segment {i}: start={s} end={e}")
        d = (e - s) / rate
        total += d
        vf = f"[0:v]trim=start={s:.3f}:end={e:.3f},setpts=PTS-STARTPTS"
        if rate != 1.0:
            vf += f",setpts=PTS/{rate:.3f}"
        parts.append(vf + f"[v{i}]")
        af = f"[0:a]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS"
        if rate != 1.0:
            af += f",atempo={rate:.3f}"
        if cf > 0 and i > 0:                       # fade in at internal joins
            af += f",afade=t=in:st=0:d={cf:.3f}"
        if cf > 0 and i < len(subs) - 1:           # fade out into internal joins
            af += f",afade=t=out:st={max(0.0, d - cf):.3f}:d={cf:.3f}"
        af += f"[a{i}]"
        parts.append(af)
        labels.append(f"[v{i}][a{i}]")            # concat wants v,a interleaved
    n = len(subs)
    parts.append("".join(labels) + f"concat=n={n}:v=1:a=1[vout][aout]")
    filtergraph = ";".join(parts)

    run_ffmpeg(["-i", video_path, "-filter_complex", filtergraph,
                "-map", "[vout]", "-map", "[aout]"]
               + video_encode_args(cfg)
               + ["-c:a", "aac", "-b:a", r["audio_bitrate"],
                  "-movflags", "+faststart", out_path])
    info = probe(out_path)
    if not info.get("has_audio"):
        raise CutError(f"cut produced no audio stream: {out_path}")
    got = info["duration"]
    if abs(got - total) > 1.5:
        raise CutError(f"segment concat off: wanted {total:.2f}s got {got:.2f}s")
    log.info("cut %d segments (%.1fs) -> %s", n, got, out_path.name)
    return out_path


_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


def concat_bumpers(clip_path: str | Path, out_path: str | Path,
                   cfg: dict | None = None, intro: str | Path | None = None,
                   outro: str | Path | None = None) -> Path | None:
    """Concat an intro and/or outro bumper (video or still image) around a
    finished clip, matched to its resolution/fps. Returns None when neither
    bumper resolves — bumpers are branding, so a missing file logs a warning
    and is skipped rather than failing the clip."""
    cfg = cfg or load_config()
    clip_path, out_path = Path(clip_path), Path(out_path)
    image_s = float(cfg.get("style", {}).get("bumper_image_s", 2.0))

    def _resolve(p) -> Path | None:
        if not p:
            return None
        path = Path(p)
        if not path.is_absolute():
            from config import ROOT
            path = ROOT / path
        if not path.is_file():
            log.warning("bumper missing, skipped: %s", p)
            return None
        return path

    bumpers = [("intro", _resolve(intro)), ("outro", _resolve(outro))]
    bumpers = [(k, p) for k, p in bumpers if p is not None]
    if not bumpers:
        return None

    info = probe(clip_path)
    w, h, fps = info["width"], info["height"], max(1.0, info["fps"])
    inputs: list = ["-i", clip_path]
    parts: list[str] = [
        f"[0:v]scale={w}:{h},setsar=1,fps={fps:.3f},format=yuv420p[clipv]",
        "[0:a]aresample=44100,"
        "aformat=sample_fmts=fltp:channel_layouts=stereo[clipa]"]
    seq: dict[str, str] = {}
    n_inputs = 1
    for k, p in bumpers:
        idx = n_inputs
        n_inputs += 1
        is_img = p.suffix.lower() in _IMAGE_EXTS
        if is_img:
            inputs += ["-loop", "1", "-t", f"{image_s:.3f}", "-i", p]
        else:
            inputs += ["-i", p]
        dur = image_s if is_img else probe(p)["duration"]
        parts.append(f"[{idx}:v]scale={w}:{h}:force_original_aspect_ratio="
                     f"decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
                     f"fps={fps:.3f},format=yuv420p[{k}v]")
        if not is_img and probe(p)["has_audio"]:
            parts.append(f"[{idx}:a]aresample=44100,aformat=sample_fmts=fltp:"
                         f"channel_layouts=stereo[{k}a]")
        else:
            parts.append(f"anullsrc=r=44100:cl=stereo,atrim=0:{dur:.3f},"
                         "aformat=sample_fmts=fltp:"
                         f"channel_layouts=stereo[{k}a]")
        seq[k] = k
    order = (["[introv][introa]"] if "intro" in seq else []) + \
        ["[clipv][clipa]"] + (["[outrov][outroa]"] if "outro" in seq else [])
    parts.append("".join(order) + f"concat=n={len(order)}:v=1:a=1[vout][aout]")

    run_ffmpeg([*inputs, "-filter_complex", ";".join(parts),
                "-map", "[vout]", "-map", "[aout]"]
               + video_encode_args(cfg, final=True)
               + ["-c:a", "aac", "-b:a", cfg["render"]["audio_bitrate"],
                  "-movflags", "+faststart", out_path])
    log.info("bumpers %s -> %s", "+".join(k for k, _ in bumpers), out_path.name)
    return out_path


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="smoke: cut a clip")
    ap.add_argument("video")
    ap.add_argument("start", type=float)
    ap.add_argument("end", type=float)
    ap.add_argument("--out", default="output/_smoke_cut.mp4")
    a = ap.parse_args()
    print(cut_clip(a.video, a.start, a.end, a.out))
