"""Per-shot intelligent reframing to vertical (or square) crops.

Strategy per sampled frame (every N frames):
  1. MediaPipe face detection — largest face wins; with multiple faces the
     "active speaker" is approximated as largest face weighted by mouth-openness
     VARIANCE from Face Mesh landmarks (no audio-visual ASD — out of scope).
  2. No face → motion-centroid fallback (frame differencing).
  3. No motion → center crop with headroom.

The sparse targets are interpolated to every frame, then per-scene-segment:
look-ahead smoothing (centered moving average) → EMA → velocity clamp.
Smoothness is MEASURABLE: path_metrics() reports max per-frame crop-center
velocity/acceleration in output-pixel space; enforce_smoothness() clamps and
verifies against config thresholds (unit-tested pure functions).

Rendering: ffmpeg sendcmd drives a dynamic crop filter (decode+encode stays in
ffmpeg; audio copied)."""
from __future__ import annotations

import json
import math
from collections import defaultdict, deque
from pathlib import Path

import numpy as np

from config import load_config
from errors import ReframeError
from ffutil import filter_path, probe, run_ffmpeg, video_encode_args
from logutil import get_logger

log = get_logger("reframe")


# ------------------------------------------------------- pure path helpers

def smooth_path(raw: list[float], ema_alpha: float, lookahead: int) -> list[float]:
    """Centered moving average (look-ahead: future frames influence current
    crop) followed by EMA. Pure, unit-tested."""
    if not raw:
        return []
    arr = np.asarray(raw, dtype=np.float64)
    k = max(1, int(lookahead))
    kernel = np.ones(2 * k + 1) / (2 * k + 1)
    padded = np.pad(arr, k, mode="edge")
    ma = np.convolve(padded, kernel, mode="valid")
    out = [ma[0]]
    for v in ma[1:]:
        out.append(out[-1] + ema_alpha * (v - out[-1]))
    return [float(x) for x in out]


def clamp_velocity(path: list[float], max_v: float) -> list[float]:
    """Limit per-frame movement of the crop center. Pure, unit-tested."""
    if not path:
        return []
    out = [path[0]]
    for v in path[1:]:
        step = v - out[-1]
        out.append(out[-1] + max(-max_v, min(max_v, step)))
    return out


def follow_path(targets: list[float], max_v: float, max_a: float) -> list[float]:
    """Acceleration-limited trapezoidal follower: tracks `targets` while
    guaranteeing |velocity| ≤ max_v and |Δvelocity| ≤ max_a per frame BY
    CONSTRUCTION (velocity clamping alone spikes acceleration when noisy
    targets flip direction). Braking distance keeps it from oscillating.
    Pure, unit-tested."""
    if not targets:
        return []
    pos, vel = float(targets[0]), 0.0
    out = [pos]
    for target in targets[1:]:
        err = target - pos
        brake = math.sqrt(2.0 * max_a * abs(err)) if err else 0.0
        desired = math.copysign(min(brake, max_v, abs(err)), err)
        vel += max(-max_a, min(max_a, desired - vel))
        vel = max(-max_v, min(max_v, vel))
        pos += vel
        out.append(pos)
    return out


def path_metrics(path: list[float]) -> dict:
    """Max per-frame velocity and acceleration of a crop-center path."""
    if len(path) < 3:
        return {"max_velocity": 0.0, "max_accel": 0.0}
    a = np.asarray(path)
    vel = np.diff(a)
    acc = np.diff(vel)
    return {"max_velocity": float(np.max(np.abs(vel))),
            "max_accel": float(np.max(np.abs(acc)))}


def enforce_smoothness(path: list[float], rcfg: dict,
                       scale: float = 1.0) -> tuple[list[float], dict, bool]:
    """Run the accel-limited follower (thresholds are in OUTPUT px; `scale`
    converts source px → output px) and verify metrics against config."""
    s = max(scale, 1e-6)
    max_v = rcfg["max_center_velocity_px"] / s
    max_a = rcfg["max_center_accel_px"] / s
    followed = follow_path(path, max_v, max_a)
    m = path_metrics([p * scale for p in followed])
    ok = (m["max_velocity"] <= rcfg["max_center_velocity_px"] + 1e-3
          and m["max_accel"] <= rcfg["max_center_accel_px"] + 1e-3)
    return followed, m, ok


def crop_geometry(w: int, h: int, aspect: str) -> tuple[int, int, int]:
    """(crop_w, crop_h, y0) for the target aspect within a w×h source."""
    if aspect == "1:1":
        side = min(w, h)
        return side, side, (h - side) // 2
    cw = int(h * 9 / 16) & ~1  # 9:16 (default)
    if cw <= w:
        return cw, h, 0
    ch = int(w * 16 / 9) & ~1  # source narrower than 9:16 — crop height
    return w, min(ch, h), max(0, (h - ch) // 2)


# ---------------------------------------------------------- target tracking

class _FaceTracker:
    """Per-run mouth-openness history, bucketed by horizontal position."""

    def __init__(self, width: int, buckets: int = 6):
        self.width = width
        self.buckets = buckets
        self.hist: dict[int, deque] = defaultdict(lambda: deque(maxlen=12))

    def bucket(self, cx: float) -> int:
        return min(self.buckets - 1, int(cx / self.width * self.buckets))

    def add(self, cx: float, mouth_open: float) -> None:
        self.hist[self.bucket(cx)].append(mouth_open)

    def variance(self, cx: float) -> float:
        h = self.hist[self.bucket(cx)]
        return float(np.var(h)) if len(h) >= 3 else 0.0


def _mouth_openness(landmarks, h: int) -> float:
    """Lip gap (landmarks 13/14) normalized by face height (10/152)."""
    top, bottom = landmarks[13], landmarks[14]
    fh = abs(landmarks[152].y - landmarks[10].y) * h
    return abs(bottom.y - top.y) * h / max(fh, 1e-6)


def track_targets(clip_path: Path, rcfg: dict, w: int, h: int,
                  every: int | None = None) -> tuple[list[float | None], dict]:
    """Sampled target x-centers (None = no signal → later filled with center).
    Returns (targets_per_sampled_frame, stats). `every` is the frame sampling
    stride; on a reduced-fps proxy pass 1 (every frame)."""
    import cv2
    import mediapipe as mp

    every = max(1, int(every if every is not None
                       else rcfg["face_detect_every_n_frames"]))
    cap = cv2.VideoCapture(str(clip_path))
    tracker = _FaceTracker(w)
    targets: list[float | None] = []
    stats = {"face": 0, "motion": 0, "center": 0}
    prev_gray = None

    with mp.solutions.face_detection.FaceDetection(
            model_selection=1,
            min_detection_confidence=rcfg["min_face_confidence"]) as fd, \
         mp.solutions.face_mesh.FaceMesh(
            static_image_mode=False, max_num_faces=4,
            refine_landmarks=False,
            min_detection_confidence=rcfg["min_face_confidence"]) as fm:
        idx = 0
        while True:
            # grab() skips the expensive decode for frames we won't inspect
            if idx % every:
                if not cap.grab():
                    break
                idx += 1
                continue
            ok, frame = cap.read()
            if not ok:
                break
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            faces = []
            det = fd.process(rgb)
            for d in (det.detections or []):
                bb = d.location_data.relative_bounding_box
                cx = (bb.xmin + bb.width / 2) * w
                faces.append((cx, bb.width * bb.height))
            if faces:
                if len(faces) > 1:
                    mesh = fm.process(rgb)
                    for fl in (mesh.multi_face_landmarks or []):
                        lm = fl.landmark
                        fcx = float(np.mean([p.x for p in lm])) * w
                        tracker.add(fcx, _mouth_openness(lm, h))
                    best = max(faces, key=lambda f: f[1] *
                               (1.0 + 2.0 * min(tracker.variance(f[0]) * 100, 2.0)))
                else:
                    best = faces[0]
                targets.append(float(np.clip(best[0], 0, w)))
                stats["face"] += 1
            else:
                gray = cv2.cvtColor(cv2.resize(frame, (w // 2 or 1, h // 2 or 1)),
                                    cv2.COLOR_BGR2GRAY)
                t = None
                if prev_gray is not None:
                    diff = cv2.absdiff(gray, prev_gray)
                    _, th = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)
                    m = cv2.moments(th)
                    if m["m00"] > 800:  # enough moving mass
                        t = float(np.clip(m["m10"] / m["m00"] * 2, 0, w))
                prev_gray = gray
                targets.append(t)
                stats["motion" if t is not None else "center"] += 1
            idx += 1
    cap.release()
    return targets, stats


# --------------------------------------------------------------- main entry

def _build_proxy(source: Path, start: float, dur: float, proxy: Path,
                 proxy_h: int, proxy_fps: float) -> None:
    """Small, cheap, low-fps proxy of just the clip window for MediaPipe
    tracking. Fast keyframe seek (-ss before -i), video only."""
    run_ffmpeg(["-ss", f"{start:.3f}", "-i", source, "-t", f"{dur:.3f}", "-an",
                "-vf", f"scale=-2:{proxy_h},fps={proxy_fps}",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", proxy],
               timeout=900)


def reframe_clip(source: str | Path, start: float, end: float,
                 out_path: str | Path, scene_cuts_rel: list[float] | None = None,
                 cfg: dict | None = None, aspect: str = "9:16",
                 debug_dir: str | Path | None = None,
                 info: dict | None = None) -> dict:
    """Reframe the [start, end] window of `source` to the target aspect in a
    SINGLE re-encode (seek + crop + scale straight from the source — no
    intermediate full-res cut). Tracking runs on a 360p low-fps proxy; crop
    coordinates are scaled back to full resolution. Returns a metrics dict."""
    cfg = cfg or load_config()
    rcfg = cfg["reframe"]
    source, out_path = Path(source), Path(out_path)
    if not source.exists():
        raise ReframeError(f"source not found: {source}")
    if aspect == "16:9":
        raise ReframeError("16:9 is pass-through — pipeline must skip reframe")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    src = info or probe(source)
    w, h, fps = int(src["width"]), int(src["height"]), float(src["fps"])
    dur = end - start
    if dur <= 0:
        raise ReframeError(f"invalid range: start={start} end={end}")
    ow, oh = ((rcfg["output"]["width"], rcfg["output"]["height"])
              if aspect == "9:16" else (1080, 1080))
    cw, ch, y0 = crop_geometry(w, h, aspect)
    n_frames = max(1, int(round(dur * fps)))

    proxy_h = int(rcfg.get("proxy_height", 360))
    proxy_fps = float(rcfg.get("proxy_fps", 12))
    proxy = out_path.with_name(out_path.stem + "_proxy.mp4")
    try:
        _build_proxy(source, start, dur, proxy, proxy_h, proxy_fps)
        pinfo = probe(proxy)
        pw = int(pinfo["width"])
        targets, stats = track_targets(proxy, rcfg, pw, int(pinfo["height"]),
                                       every=1)
    except Exception as e:  # noqa: BLE001 — tracking failure → center fallback
        log.warning("tracking failed (%s) — center crop with headroom", e)
        targets, stats, pw = [], {"face": 0, "motion": 0, "center": 1}, w
    finally:
        proxy.unlink(missing_ok=True)

    center = w / 2.0
    half = cw / 2.0
    x_scale = w / max(pw, 1)  # proxy-x → source-x
    # clamp raw targets into the reachable range BEFORE smoothing so the
    # follower's guarantees survive (a post-hoc position clamp would kink
    # the path and break the acceleration bound)
    sampled = [float(np.clip(t * x_scale if t is not None else center,
                             half, w - half))
               for t in targets] or [center]
    # interpolate sampled proxy targets (spaced 1/proxy_fps apart) onto every
    # output frame time
    sample_times = np.arange(len(sampled)) / proxy_fps
    frame_times = np.arange(n_frames) / fps
    full = np.interp(frame_times, sample_times, sampled).tolist()

    # per-scene segments: smoothing resets at shot boundaries
    cut_frames = sorted({int(t * fps) for t in (scene_cuts_rel or [])
                         if 0 < t < dur})
    bounds = [0] + [f for f in cut_frames if 0 < f < n_frames] + [n_frames]
    scale = ow / cw  # source px -> output px
    path, all_ok, worst = [], True, {"max_velocity": 0.0, "max_accel": 0.0}
    for a, b in zip(bounds[:-1], bounds[1:]):
        seg = smooth_path(full[a:b], rcfg["ema_alpha"], rcfg["lookahead_frames"])
        seg, m, ok = enforce_smoothness(seg, rcfg, scale)
        path.extend(seg)
        all_ok &= ok
        worst = {k: max(worst[k], m[k]) for k in worst}

    # ffmpeg sendcmd file: one crop-x command per frame
    cmd_file = out_path.with_suffix(".cmds.txt")
    with open(cmd_file, "w", encoding="ascii") as f:
        for i, p in enumerate(path):
            f.write(f"{i / fps:.4f} crop@dyn x {p - half:.1f};\n")

    vf = (f"sendcmd=f='{filter_path(cmd_file)}',"
          f"crop@dyn=w={cw}:h={ch}:x={max(0.0, path[0] - half):.1f}:y={y0},"
          f"scale={ow}:{oh}:flags=lanczos,setsar=1")
    # single re-encode from source: fast seek, crop, scale, and re-encode audio
    # for the same window (aac keeps sync across arbitrary start times)
    run_ffmpeg(["-ss", f"{start:.3f}", "-i", source, "-t", f"{dur:.3f}",
                "-vf", vf] + video_encode_args(cfg)
               + ["-c:a", "aac", "-b:a", cfg["render"]["audio_bitrate"],
                  "-movflags", "+faststart", out_path])

    metrics = {"aspect": aspect, "crop": [cw, ch], "output": [ow, oh],
               "tracking": stats, "smoothness": worst,
               "smoothness_ok": bool(all_ok),
               "segments": len(bounds) - 1}
    log.info("reframed %s [%.1f-%.1f]: tracking=%s smoothness=%s ok=%s",
             source.name, start, end, stats,
             {k: round(v, 2) for k, v in worst.items()}, all_ok)

    if debug_dir:
        Path(debug_dir).mkdir(parents=True, exist_ok=True)
        (Path(debug_dir) / f"{out_path.stem}_croppath.json").write_text(
            json.dumps({"metrics": metrics, "path_every_10": path[::10]}),
            encoding="utf-8")
    if not cmd_file.exists() or not out_path.exists():
        raise ReframeError("reframe output missing")
    cmd_file.unlink(missing_ok=True)
    return metrics


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="smoke: reframe a source window")
    ap.add_argument("source")
    ap.add_argument("start", type=float)
    ap.add_argument("end", type=float)
    ap.add_argument("--out", default="output/_smoke_reframe.mp4")
    ap.add_argument("--aspect", default="9:16")
    a = ap.parse_args()
    print(json.dumps(reframe_clip(a.source, a.start, a.end, a.out,
                                  aspect=a.aspect), indent=2))
