"""Burned-in subtitle detector (OpenCV only — detection, not reading).

Decides whether a SOURCE video already carries hardcoded subtitles inside a
given clip range, and where the band sits, so style_refiner can choose
replace/keep/ignore. This is DETECTION only: no OCR, no text reading, no
erasure (all out of scope). Output is a SUBTITLE_DETECT_RESULT dict, cached
per source-hash + range + detector config.

Method: sample ~1 frame/sec across the range; in the lower `search_band_ratio`
of each frame find text-like horizontal lines (morphological gradient →
threshold → horizontal close → wide/short contours); a vertical zone that
carries text in >= `persistence_ratio` of frames is reported as a burned band.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np

from config import ROOT, config_hash, file_hash, load_config
from errors import StyleError
from ffutil import probe, run_ffmpeg
from logutil import get_logger
from schemas import validate

log = get_logger("subtitle_detect")


def _text_lines_in_region(region: np.ndarray) -> list[tuple[int, int]]:
    """Return [(top_row, bottom_row), ...] of text-like lines within `region`
    (grayscale). Rows are region-local. Empty list = no text found."""
    h, w = region.shape[:2]
    if h < 8 or w < 8:
        return []
    # Morphological gradient highlights character edges regardless of color.
    grad = cv2.morphologyEx(region, cv2.MORPH_GRADIENT,
                            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
    _, bw = cv2.threshold(grad, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    # Connect neighbouring characters into a single horizontal line blob.
    connect_w = max(9, int(w * 0.03))
    connected = cv2.morphologyEx(
        bw, cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (connect_w, 1)))
    contours, _ = cv2.findContours(connected, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
    lines: list[tuple[int, int]] = []
    for c in contours:
        x, y, cw, ch = cv2.boundingRect(c)
        # Text line: wide, short, meaningfully long relative to frame width.
        if cw >= w * 0.12 and 0.02 * h <= ch <= 0.30 * h and cw >= ch * 3:
            lines.append((y, y + ch))
    return lines


def detect_subtitles(video_path: str | Path, start: float, end: float,
                     cfg: dict | None = None) -> dict:
    """SUBTITLE_DETECT_RESULT for source range [start, end]. Cached."""
    cfg = cfg or load_config()
    video_path = Path(video_path)
    if not video_path.exists():
        raise StyleError(f"video not found for subtitle detection: {video_path}")

    scfg = cfg.get("style", {}).get("subtitle_detect", {})
    sample_fps = float(scfg.get("sample_fps", 1.0))
    search_band = float(scfg.get("search_band_ratio", 0.45))
    persistence = float(scfg.get("persistence_ratio", 0.30))
    window_seconds = float(scfg.get("window_seconds", 6.0))

    cache_dir = ROOT / cfg["paths"]["cache_dir"] / "subtitle_detect"
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = f"{file_hash(video_path)[:24]}_{start:.2f}_{end:.2f}_{config_hash(cfg, 'style')}"
    cache_file = cache_dir / f"{key}.json"
    if cache_file.exists():
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        validate(data, "subtitle_detect_result")
        return data

    result = _detect_uncached(video_path, start, end, sample_fps,
                              search_band, persistence,
                              window_seconds=window_seconds)
    validate(result, "subtitle_detect_result")
    cache_file.write_text(json.dumps(result), encoding="utf-8")
    log.info("subtitles present=%s band=[%.2f,%.2f] conf=%.2f over %d frames",
             result["present"], result["band_top_pct"], result["band_bottom_pct"],
             result["confidence"], result["sampled_frames"])
    return result


def _detect_uncached(video_path: Path, start: float, end: float,
                     sample_fps: float, search_band: float,
                     persistence: float, window_seconds: float = 6.0) -> dict:
    """Sample frames across [start, end] and look for a text-like band.

    A candidate band position is found via a `window_seconds` sliding window
    (not a single average over the whole range) — burned-in subtitles are
    often per-line/intermittent (on screen only while that line of dialogue
    plays), and averaging presence over a long clip dilutes a real, clearly
    -visible band below `persistence` even though it is obviously there for
    several consecutive seconds.

    That candidate is then only accepted if either (a) it holds for a single
    continuous run covering >= `persistence` of the whole range (the original
    global test — a caption that is simply on screen continuously), or (b) it
    recurs across >= 2 separate bursts with a gap between them (dialogue-line
    captions turning on/off/on as new lines are spoken). A candidate that is
    a ONE-OFF burst — visible for a while, then never again — fails both and
    is rejected: that shape matches an incidental on-screen object (signage,
    a prop, a jersey) holding still for a few seconds, not a subtitle track,
    and accepting single one-off bursts made real footage's static text-like
    background objects false-positive as burned subtitles."""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise StyleError(f"OpenCV could not open {video_path}")
    try:
        dur = max(0.0, end - start)
        n = max(1, min(120, int(dur * sample_fps)))
        frame_hits: list[np.ndarray] = []
        frame_h = 0
        for i in range(n):
            t = start + (dur * (i + 0.5) / n) if dur > 0 else start
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            H = gray.shape[0]
            if frame_h == 0:
                frame_h = H
            hits = np.zeros(H, dtype=bool)
            y0 = int(H * (1.0 - search_band))
            for (rt, rb) in _text_lines_in_region(gray[y0:H, :]):
                hits[y0 + rt: y0 + rb] = True
            frame_hits.append(hits)
        sampled = len(frame_hits)
        empty = {"present": False, "band_top_pct": 0.0, "band_bottom_pct": 0.0,
                "confidence": 0.0, "sampled_frames": sampled}
        if sampled == 0:
            return empty

        # 1) candidate band position: best-confidence window anywhere in range.
        window = max(1, min(sampled, round(window_seconds * sample_fps)))
        cand_top = cand_bottom = None
        cand_conf = 0.0
        for w0 in range(0, sampled - window + 1):
            votes = np.mean(frame_hits[w0:w0 + window], axis=0)
            hot = votes >= persistence
            top, bottom, conf = _largest_run(hot, votes)
            if top is not None and conf > cand_conf:
                cand_top, cand_bottom, cand_conf = top, bottom, conf
        if cand_top is None:
            return empty

        # 2) validate the candidate against the FULL range: per-frame, does
        # this specific row band hold (majority of its rows hit)?
        band_hit = np.array([bool(frame_hits[i][cand_top:cand_bottom].mean() >= 0.5)
                             for i in range(sampled)])
        runs = []
        i = 0
        while i < sampled:
            if band_hit[i]:
                j = i
                while j < sampled and band_hit[j]:
                    j += 1
                runs.append(j - i)
                i = j
            else:
                i += 1
        if not runs:
            return empty
        longest = max(runs)
        # a "burst" must hold for >=2s of real time, not a single sampled
        # frame — a lone hit is noise/flicker, not a recurring caption line.
        min_burst = max(2, round(2.0 * sample_fps))
        bursts = [r for r in runs if r >= min_burst]
        continuous = (longest / sampled) >= persistence
        recurring = len(bursts) >= 2
        if not (continuous or recurring):
            return empty
        return {
            "present": True,
            "band_top_pct": round(cand_top / frame_h, 4),
            "band_bottom_pct": round(cand_bottom / frame_h, 4),
            "confidence": round(float(cand_conf), 4),
            "sampled_frames": sampled,
        }
    finally:
        cap.release()


def _largest_run(hot: np.ndarray, persist: np.ndarray):
    """Longest contiguous run of hot rows → (top, bottom, mean_persistence)."""
    best_len = best_top = best_bot = 0
    i = 0
    n = len(hot)
    found = False
    while i < n:
        if hot[i]:
            j = i
            while j < n and hot[j]:
                j += 1
            if j - i > best_len:
                best_len, best_top, best_bot = j - i, i, j
                found = True
            i = j
        else:
            i += 1
    if not found:
        return None, None, 0.0
    return best_top, best_bot, float(persist[best_top:best_bot].mean())


def verify_no_leftover_subs(final_path: str | Path, exclude_top_pct: float,
                            exclude_bottom_pct: float,
                            cfg: dict | None = None) -> dict | None:
    """Hard invariant for a REPLACE/KEEP clip: scan the FINAL rendered video
    for a text-like band (same detector as detect_subtitles, uncached — the
    final render is unique per clip so there's nothing to cache). If a band
    is found that does not substantially overlap the region ClipForge's own
    caption/CTA occupies (`exclude_top_pct`..`exclude_bottom_pct`), that band
    is an unexplained leftover (e.g. a source subtitle the crop/keep decision
    failed to remove or defer to) — return its SUBTITLE_DETECT_RESULT so the
    caller can raise/log. Returns None when nothing unexplained is found."""
    cfg = cfg or load_config()
    final_path = Path(final_path)
    if not final_path.exists():
        raise StyleError(f"final render not found for invariant check: {final_path}")
    scfg = cfg.get("style", {}).get("subtitle_detect", {})
    sample_fps = float(scfg.get("sample_fps", 1.0))
    search_band = float(scfg.get("search_band_ratio", 0.45))
    persistence = float(scfg.get("persistence_ratio", 0.30))
    window_seconds = float(scfg.get("window_seconds", 6.0))

    dur = probe(final_path)["duration"]
    result = _detect_uncached(final_path, 0.0, dur, sample_fps, search_band,
                              persistence, window_seconds=window_seconds)
    if not result["present"]:
        return None
    top, bottom = result["band_top_pct"], result["band_bottom_pct"]
    band_h = bottom - top
    overlap = max(0.0, min(bottom, exclude_bottom_pct) - max(top, exclude_top_pct))
    if band_h <= 0 or (overlap / band_h) >= 0.5:
        return None  # detected band is (mostly) our own caption/CTA — expected
    return result


# --- synthetic self-test (shared by pytest and __main__) -------------------

def make_synthetic(path: Path, with_subs: bool, seconds: float = 2.0) -> Path:
    """Render a tiny test clip. with_subs burns a text-like band (a row of
    small white marks on a dark strip) in the lower third — same morphological
    signature as real subtitles, but font-free so it is portable. Used by the
    module smoke test and tests/test_subtitle_detect.py."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Mid-gray background so the frame is not uniformly flat.
    vf = "drawbox=x=0:y=0:w=640:h=360:color=gray@0.4:t=fill"
    if with_subs:
        # Dark strip + a row of small white boxes (fake glyphs) at y~300/360 (~0.84).
        vf += ",drawbox=x=60:y=292:w=520:h=40:color=black@0.8:t=fill"
        for k in range(14):
            x = 72 + k * 36
            vf += f",drawbox=x={x}:y=300:w=22:h=24:color=white:t=fill"
    run_ffmpeg(["-f", "lavfi", "-i", f"color=c=black:s=640x360:d={seconds}:r=10",
                "-vf", vf, "-frames:v", str(int(seconds * 10)),
                "-pix_fmt", "yuv420p", str(path)])
    return path


def make_intermittent_synthetic(path: Path, on_s: float = 4.0, off_s: float = 8.0,
                                 cycles: int = 3, band_y_ratio: float = 0.84) -> Path:
    """Render a test clip where a text-like band (same signature as
    make_synthetic) is burned in only for `on_s` seconds out of every
    `on_s + off_s` cycle — reproduces a source that captions per dialogue
    line rather than continuously. Repeats `cycles` times. Used to regression
    -test the false negative where averaging presence over the WHOLE clip
    range dilutes a real, clearly-visible-but-intermittent band below
    persistence_ratio."""
    path.parent.mkdir(parents=True, exist_ok=True)
    cycle = on_s + off_s
    total = cycle * cycles
    y = int(360 * band_y_ratio) - 20
    vf = "drawbox=x=0:y=0:w=640:h=360:color=gray@0.4:t=fill"
    for k in range(cycles):
        c0 = k * cycle
        c1 = c0 + on_s
        vf += f",drawbox=x=60:y={y}:w=520:h=40:color=black@0.8:t=fill:enable='between(t,{c0},{c1})'"
        for gi in range(14):
            x = 72 + gi * 36
            vf += (f",drawbox=x={x}:y={y + 8}:w=22:h=24:color=white:t=fill"
                   f":enable='between(t,{c0},{c1})'")
    run_ffmpeg(["-f", "lavfi", "-i", f"color=c=black:s=640x360:d={total}:r=10",
                "-vf", vf, "-frames:v", str(int(total * 10)),
                "-pix_fmt", "yuv420p", str(path)])
    return path


def _smoke() -> None:
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="subdetect_"))
    subd = make_synthetic(tmp / "subs.mp4", with_subs=True)
    clean = make_synthetic(tmp / "clean.mp4", with_subs=False)
    dur = probe(subd)["duration"]
    r_sub = _detect_uncached(subd, 0.0, dur, 2.0, 0.45, 0.30)
    r_clean = _detect_uncached(clean, 0.0, probe(clean)["duration"], 2.0, 0.45, 0.30)
    print("subs :", r_sub)
    print("clean:", r_clean)
    assert r_sub["present"] and r_sub["band_top_pct"] > 0.5, "should detect lower band"
    assert not r_clean["present"], "clean clip must not report subtitles"
    print("subtitle_detect smoke OK")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="detect burned-in subtitles / smoke test")
    ap.add_argument("video", nargs="?", help="video to inspect (omit for smoke test)")
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--end", type=float, default=0.0)
    a = ap.parse_args()
    if not a.video:
        _smoke()
    else:
        end = a.end or probe(a.video)["duration"]
        print(json.dumps(detect_subtitles(a.video, a.start, end), indent=2))
