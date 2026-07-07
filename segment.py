"""Segment-first shortlisting: pick the candidate spans worth transcribing
instead of transcribing the whole video.

A coarse per-second RMS energy pass over the already-extracted 16 kHz mono
audio.wav (read directly with the stdlib `wave` module — no extra ffmpeg
pass) is combined with detected scene boundaries to shortlist ~3x the target
number of clip-length windows, which are then merged into contiguous spans.
Only those spans are transcribed downstream, which is the dominant speed win
on long videos.

Short videos skip this entirely (see `should_shortlist`): whole-video
transcription is cheap there and strictly more reliable."""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

from logutil import get_logger

log = get_logger("segment")

OVERSAMPLE = 3          # shortlist ~3x the target clip count
MAX_SPAN_WINDOWS = 30   # hard cap on candidate windows regardless of target


def should_shortlist(duration: float, cfg: dict) -> bool:
    """Segment-first only pays off on long inputs. Below the threshold,
    whole-video transcription is cheap and avoids partial-transcript edge
    cases, so we keep it."""
    max_s = float(cfg["clips"]["max_seconds"])
    return duration > max(300.0, 8.0 * max_s)


def audio_energy(audio_path: str | Path, bin_seconds: float = 1.0
                 ) -> tuple[np.ndarray, float] | None:
    """Per-bin RMS energy of a 16-bit PCM wav. Returns (rms_per_bin,
    bin_seconds) or None if the audio can't be read (caller falls back to
    whole-video transcription)."""
    try:
        with wave.open(str(audio_path), "rb") as wf:
            sr = wf.getframerate()
            ch = wf.getnchannels()
            width = wf.getsampwidth()
            frames = wf.readframes(wf.getnframes())
    except (wave.Error, OSError, EOFError) as e:
        log.warning("audio energy read failed (%s) — whole-video transcription", e)
        return None
    if width != 2 or not frames:
        log.warning("unexpected wav format (width=%d) — whole-video transcription",
                    width)
        return None

    data = np.frombuffer(frames, dtype=np.int16).astype(np.float64)
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1)
    bin_n = max(1, int(sr * bin_seconds))
    n_bins = len(data) // bin_n
    if n_bins < 1:
        return None
    trimmed = data[: n_bins * bin_n].reshape(n_bins, bin_n)
    rms = np.sqrt(np.mean(np.square(trimmed), axis=1))
    return rms, bin_seconds


def _candidate_windows(duration: float, scenes: dict, cfg: dict) -> list[tuple]:
    """Clip-length candidate windows: a sliding grid plus scene-aligned starts.
    De-duplicated by rounded start time."""
    min_s = float(cfg["clips"]["min_seconds"])
    max_s = float(cfg["clips"]["max_seconds"])
    win = (min_s + max_s) / 2.0
    step = max(1.0, win * 0.5)

    starts: list[float] = []
    t = 0.0
    while t + min_s <= duration:
        starts.append(t)
        t += step
    starts += [s["start"] for s in scenes.get("scenes", [])
               if s["start"] + min_s <= duration]

    seen: set[int] = set()
    windows: list[tuple] = []
    for s in sorted(starts):
        key = int(round(s))
        if key in seen:
            continue
        seen.add(key)
        e = min(s + win, duration)
        if e - s >= min_s:
            windows.append((round(s, 3), round(e, 3)))
    return windows


def _window_energy(win: tuple, energy: np.ndarray, bin_seconds: float) -> float:
    lo = int(win[0] / bin_seconds)
    hi = max(lo + 1, int(win[1] / bin_seconds))
    seg = energy[lo:hi]
    return float(seg.mean()) if len(seg) else 0.0


def _overlap(a: tuple, b: tuple) -> float:
    inter = max(0.0, min(a[1], b[1]) - max(a[0], b[0]))
    shorter = max(1e-6, min(a[1] - a[0], b[1] - b[0]))
    return inter / shorter


def _merge_spans(spans: list[tuple], gap: float = 0.5) -> list[tuple]:
    """Union overlapping/adjacent spans so each region is transcribed once."""
    if not spans:
        return []
    spans = sorted(spans)
    merged = [list(spans[0])]
    for s, e in spans[1:]:
        if s <= merged[-1][1] + gap:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(round(a, 3), round(b, 3)) for a, b in merged]


def shortlist_spans(duration: float, scenes: dict, audio_path: str | Path,
                    cfg: dict, target_clips: int) -> list[tuple] | None:
    """Return contiguous spans (start, end) worth transcribing, or None to
    signal "transcribe the whole video" (short input or unreadable audio)."""
    if not should_shortlist(duration, cfg):
        log.info("duration %.0fs below shortlist threshold — whole-video "
                 "transcription", duration)
        return None

    e = audio_energy(audio_path)
    if e is None:
        return None
    energy, bin_seconds = e

    target = max(1, target_clips) * OVERSAMPLE
    target = min(target, MAX_SPAN_WINDOWS)
    windows = _candidate_windows(duration, scenes, cfg)
    if not windows:
        return None

    scored = sorted(windows,
                    key=lambda w: -_window_energy(w, energy, bin_seconds))
    picked: list[tuple] = []
    for w in scored:
        if all(_overlap(w, k) < 0.5 for k in picked):
            picked.append(w)
        if len(picked) >= target:
            break

    spans = _merge_spans(picked)
    covered = sum(b - a for a, b in spans)
    log.info("shortlisted %d spans (%d windows) covering %.0fs of %.0fs "
             "(%.0f%%)", len(spans), len(picked), covered, duration,
             100.0 * covered / max(duration, 1e-6))
    # If the shortlist ends up covering almost everything, the whole-video
    # path is simpler and identical in cost — use it.
    if covered >= 0.85 * duration:
        log.info("shortlist covers most of the video — whole-video transcription")
        return None
    return spans


if __name__ == "__main__":
    import argparse
    import json

    from config import load_config
    from scenes import detect_scenes

    ap = argparse.ArgumentParser(description="smoke: shortlist spans")
    ap.add_argument("video")
    ap.add_argument("audio")
    ap.add_argument("--duration", type=float, required=True)
    ap.add_argument("--clips", type=int, default=10)
    a = ap.parse_args()
    cfg = load_config()
    sc = detect_scenes(a.video, cfg)
    spans = shortlist_spans(a.duration, sc, a.audio, cfg, a.clips)
    print(json.dumps({"spans": spans}, indent=2))
