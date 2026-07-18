"""Animated Avatar Host: LivePortrait motion-transfer for the intro/outro
avatar, replacing the static PNG overlay when avatar.animation.enabled, with
an optional MuseTalk lip-sync post-process when avatar.animation.lip_sync.
enabled.

Owns:
  - AnimatedAvatarRenderer: a two-stage pipeline —
      1. runs liveportrait_worker.py in the ISOLATED .venv-avatar-anim
         (LivePortrait pins its own torch build and has no pip package —
         never enters the main venv, same isolation rule as avatar.py's
         .venv-tts / tts_worker.py) to transfer natural head motion from a
         bundled generic driving video onto the avatar image. This is
         motion-transfer only, NOT audio-driven — the mouth shapes it
         produces do not match the actual TTS words.
      2. optionally runs musetalk_worker.py in a SEPARATE isolated
         .venv-musetalk (MuseTalk pins torch==2.0.1/cu118 plus an
         mmcv/mmdet/mmpose stack that conflicts with LivePortrait's
         torch==2.3.0/cu121 — its own venv for the same reason .venv-tts and
         .venv-avatar-anim are separate) to re-sync just the mouth region of
         stage 1's output to the real TTS wav via latent-space inpainting —
         lighter than full diffusion, fits a 4GB GPU in fp16. Preserves head
         motion/identity from stage 1; only overwrites the lower-face
         region. Stage 2 is opt-in and falls back to stage 1's motion-only
         output on failure (avatar.animation.lip_sync.fallback_to_motion_only).
  - background removal via MediaPipe SelfieSegmentation, which is ALREADY a
    pinned main-venv dependency (mediapipe==0.10.14, same as reframe.py) — no
    new dependency for compositing the animated avatar with alpha
  - a per-clip content-hash cache for the (slow) animation render, mirroring
    config.config_hash/file_hash used by the pipeline's other stage markers

Every failure raises AvatarError. Silent fallback is a bug — apply_avatar
decides whether to fall back to the static PNG, and logs when it does; this
module's own fallback_to_motion_only tier (stage 2 only) works the same way.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from avatar import AvatarError
from config import ROOT, config_hash, file_hash
from logutil import get_logger

log = get_logger("avatar_anim")

# LivePortrait's inference.py prints emoji (rich progress bars) to stdout.
# subprocess.run(text=True) on Windows defaults to the console codepage
# (cp1252), which can't encode them and crashes the whole inference process
# with UnicodeEncodeError — AFTER the actual (CUDA, correct) computation has
# already run. Forcing UTF-8 stdio is the fix; nothing else about the
# subprocess call changes.
_UTF8_ENV = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}

ANIM_VENV_DIR = ROOT / ".venv-avatar-anim"
LIVEPORTRAIT_DIR = ROOT / "cache" / "liveportrait"
WORKER_PATH = ROOT / "liveportrait_worker.py"
DEFAULT_DRIVING_VIDEO = ROOT / "assets" / "avatar_driving" / "talking_loop.mp4"

MUSETALK_VENV_DIR = ROOT / ".venv-musetalk"
MUSETALK_DIR = ROOT / "cache" / "musetalk"
MUSETALK_WORKER_PATH = ROOT / "musetalk_worker.py"

# Hallo2 (fudan-generative-vision) — audio-driven talking head, the current
# avatar.animation.engine. Replaces the LivePortrait(+MuseTalk) two-stage
# engine: Hallo2 animates the portrait directly from the TTS wav in one pass.
HALLO2_VENV_DIR = ROOT / ".venv-hallo2"
HALLO2_DIR = ROOT / "cache" / "hallo2"
HALLO2_WORKER_PATH = ROOT / "hallo2_worker.py"


def _acfg(cfg: dict) -> dict:
    return cfg.get("avatar", {}).get("animation", {})


def _h2cfg(cfg: dict) -> dict:
    return _acfg(cfg).get("hallo2", {})


def _lscfg(cfg: dict) -> dict:
    return _acfg(cfg).get("lip_sync", {})


def _venv_python(cfg: dict) -> Path:
    raw = str(_acfg(cfg).get("python", ".venv-avatar-anim/Scripts/python.exe"))
    p = Path(raw)
    return p if p.is_absolute() else ROOT / p


def _liveportrait_dir(cfg: dict) -> Path:
    raw = str(_acfg(cfg).get("liveportrait_dir", "cache/liveportrait"))
    p = Path(raw)
    return p if p.is_absolute() else ROOT / p


def _musetalk_venv_python(cfg: dict) -> Path:
    raw = str(_lscfg(cfg).get("python", ".venv-musetalk/Scripts/python.exe"))
    p = Path(raw)
    return p if p.is_absolute() else ROOT / p


def _musetalk_dir(cfg: dict) -> Path:
    raw = str(_lscfg(cfg).get("musetalk_dir", "cache/musetalk"))
    p = Path(raw)
    return p if p.is_absolute() else ROOT / p


def _hallo2_venv_python(cfg: dict) -> Path:
    raw = str(_h2cfg(cfg).get("python", ".venv-hallo2/Scripts/python.exe"))
    p = Path(raw)
    return p if p.is_absolute() else ROOT / p


def _hallo2_dir(cfg: dict) -> Path:
    raw = str(_h2cfg(cfg).get("hallo2_dir", "cache/hallo2"))
    p = Path(raw)
    return p if p.is_absolute() else ROOT / p


def _hallo2_pretrained_dir(cfg: dict) -> Path:
    raw = str(_h2cfg(cfg).get("pretrained_dir", "cache/hallo2/pretrained_models"))
    p = Path(raw)
    return p if p.is_absolute() else ROOT / p


def _hallo2_config(cfg: dict) -> Path:
    raw = str(_h2cfg(cfg).get("config", "")).strip()
    if not raw:
        return _hallo2_dir(cfg) / "configs" / "inference" / "long.yaml"
    p = Path(raw)
    return p if p.is_absolute() else ROOT / p


def _is_bundled_liveportrait_example(p: Path, cfg: dict) -> bool:
    """True when `p`'s content is byte-identical to one of LivePortrait's
    own bundled example driving clips — the signal that a dev/verification
    default is in use rather than the user's own footage, independent of
    what the file happens to be named at `p`."""
    examples_dir = _liveportrait_dir(cfg) / "assets" / "examples" / "driving"
    if not examples_dir.is_dir():
        return False
    try:
        target_hash = file_hash(p)
    except OSError:
        return False
    for example in examples_dir.glob("*.mp4"):
        try:
            if file_hash(example) == target_hash:
                return True
        except OSError:
            continue
    return False


def _resolve_driving_video(cfg: dict) -> Path:
    raw = str(_acfg(cfg).get("driving_video", "")).strip() or \
        str(DEFAULT_DRIVING_VIDEO.relative_to(ROOT).as_posix())
    p = Path(raw)
    p = p if p.is_absolute() else ROOT / p
    if not p.is_file():
        raise AvatarError(
            f"avatar animation driving video not found: {p} — place a short "
            "generic talking-head clip there or set avatar.animation."
            "driving_video")
    if _is_bundled_liveportrait_example(p, cfg):
        log.warning(
            "avatar animation is using a BUNDLED LivePortrait example clip "
            "as its driving video (%s) — this is fine for development/"
            "verification only. Replace it with your own driving footage "
            "before a real/production run (set avatar.animation."
            "driving_video, or overwrite assets/avatar_driving/"
            "talking_loop.mp4).", p)
    return p


def _cache_key(image_path: Path, audio_path: Path, driving_path: Path,
              duration_s: float, cfg: dict) -> str:
    parts = "|".join([
        file_hash(image_path), file_hash(audio_path), file_hash(driving_path),
        f"{duration_s:.3f}", config_hash(cfg, "avatar")])
    import hashlib
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()[:16]


class AnimatedAvatarRenderer:
    """Motion engine selected via avatar.animation.engine (only
    'liveportrait' today); optional lip-sync post-process selected via
    avatar.animation.lip_sync.engine (only 'musetalk' today). Compositing
    code (avatar.build_composite_graph) never sees engine internals — it
    just consumes the video-with-alpha render() returns."""

    def __init__(self, cfg: dict):
        self.cfg = cfg

    # ---------------------------------------------------------- raw render

    def _run_liveportrait(self, source_image: Path, driving_video: Path,
                          out_path: Path) -> Path:
        py = _venv_python(self.cfg)
        if not py.is_file():
            raise AvatarError(
                "animation venv not found — run "
                "`python avatar.py setup-anim-venv` once "
                "(clones LivePortrait + downloads model weights, ~2-3 GB)",
                detail=str(py))
        lp_dir = _liveportrait_dir(self.cfg)
        payload = {
            "source_image": str(source_image),
            "driving_video": str(driving_video),
            "out_path": str(out_path),
            "liveportrait_dir": str(lp_dir),
            "device": str(_acfg(self.cfg).get("device", "auto")),
        }
        timeout = float(_acfg(self.cfg).get("timeout_s", 600))
        log.info("animation: running LivePortrait via %s (device=%s)",
                 py, payload["device"])
        try:
            proc = subprocess.run(
                [str(py), str(WORKER_PATH)], input=json.dumps(payload),
                capture_output=True, text=True, encoding="utf-8",
                timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise AvatarError(
                f"animation worker timed out after {timeout:.0f}s "
                "(raise avatar.animation.timeout_s or set "
                "avatar.animation.device: cpu)",
                detail=(e.stderr or "")[-1000:] if isinstance(e.stderr, str)
                else None)
        except OSError as e:
            raise AvatarError(f"could not launch animation worker: {e}",
                              detail=str(py))

        stderr_tail = (proc.stderr or "")[-1000:]
        lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
        reply = None
        if lines:
            try:
                reply = json.loads(lines[-1])
            except json.JSONDecodeError:
                reply = None
        if reply is None:
            raise AvatarError(f"animation worker exited {proc.returncode}",
                              detail=stderr_tail)
        if not reply.get("ok"):
            raise AvatarError(
                f"animation failed: {reply.get('error', 'unknown error')}",
                detail=stderr_tail)
        if not out_path.is_file() or out_path.stat().st_size == 0:
            raise AvatarError(f"animation output missing or empty: {out_path}",
                              detail=stderr_tail)
        return out_path

    def _run_musetalk(self, raw_video: Path, audio_path: Path,
                      out_path: Path) -> Path:
        """Re-sync raw_video's mouth region to audio_path (the real TTS wav)
        via MuseTalk, preserving raw_video's head motion/identity elsewhere.
        Raises AvatarError on any failure — caller (render()) decides whether
        to fall back to raw_video (motion-only) per lip_sync.
        fallback_to_motion_only."""
        import ffutil
        py = _musetalk_venv_python(self.cfg)
        if not py.is_file():
            raise AvatarError(
                "lip-sync venv not found — run "
                "`python avatar.py setup-musetalk-venv` once "
                "(clones MuseTalk + downloads model weights, ~3-4 GB)",
                detail=str(py))
        mt_dir = _musetalk_dir(self.cfg)
        ffmpeg_dir = str(Path(ffutil.ffmpeg_bin()).resolve().parent)
        payload = {
            "raw_video": str(raw_video),
            "audio_path": str(audio_path),
            "out_path": str(out_path),
            "musetalk_dir": str(mt_dir),
            "bbox_shift": int(_lscfg(self.cfg).get("bbox_shift", 0)),
            "version": str(_lscfg(self.cfg).get("version", "v15")),
            "ffmpeg_dir": ffmpeg_dir,
        }
        timeout = float(_lscfg(self.cfg).get("timeout_s", 1200))
        log.info("lip-sync: running MuseTalk via %s", py)
        try:
            proc = subprocess.run(
                [str(py), str(MUSETALK_WORKER_PATH)], input=json.dumps(payload),
                capture_output=True, text=True, encoding="utf-8",
                timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise AvatarError(
                f"lip-sync worker timed out after {timeout:.0f}s "
                "(raise avatar.animation.lip_sync.timeout_s)",
                detail=(e.stderr or "")[-1000:] if isinstance(e.stderr, str)
                else None)
        except OSError as e:
            raise AvatarError(f"could not launch lip-sync worker: {e}",
                              detail=str(py))

        stderr_tail = (proc.stderr or "")[-1000:]
        lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
        reply = None
        if lines:
            try:
                reply = json.loads(lines[-1])
            except json.JSONDecodeError:
                reply = None
        if reply is None:
            raise AvatarError(f"lip-sync worker exited {proc.returncode}",
                              detail=stderr_tail)
        if not reply.get("ok"):
            raise AvatarError(
                f"lip-sync failed: {reply.get('error', 'unknown error')}",
                detail=stderr_tail)
        if not out_path.is_file() or out_path.stat().st_size == 0:
            raise AvatarError(f"lip-sync output missing or empty: {out_path}",
                              detail=stderr_tail)
        return out_path

    # ---------------------------------------------------- background removal

    def _segment_alpha(self, raw_video: Path, alpha_out: Path) -> None:
        """MediaPipe SelfieSegmentation, frame by frame: raw_video (opaque) ->
        alpha_out (.mov, qtrle, alpha channel), same treatment build_composite_
        graph already applies to the static PNG (format=rgba)."""
        import cv2
        import mediapipe as mp
        import ffutil

        cap = cv2.VideoCapture(str(raw_video))
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        mask_path = alpha_out.with_name(alpha_out.stem + "_mask.mp4")
        writer = cv2.VideoWriter(str(mask_path),
                                 cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
        try:
            with mp.solutions.selfie_segmentation.SelfieSegmentation(
                    model_selection=1) as seg:
                while True:
                    ok, frame = cap.read()
                    if not ok:
                        break
                    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    result = seg.process(rgb)
                    mask = (result.segmentation_mask > 0.5).astype("uint8") * 255
                    writer.write(cv2.cvtColor(mask, cv2.COLOR_GRAY2BGR))
        finally:
            cap.release()
            writer.release()

        ffutil.run_ffmpeg([
            "-i", raw_video, "-i", mask_path,
            "-filter_complex", "[1:v]format=gray[a];[0:v][a]alphamerge",
            "-c:v", "qtrle", "-pix_fmt", "argb", alpha_out])
        mask_path.unlink(missing_ok=True)

    # -------------------------------------------------------- Hallo2 engine

    def _run_hallo2(self, source_image: Path, driving_audio: Path,
                    out_path: Path) -> Path:
        """Animate source_image from driving_audio via Hallo2 (one pass, no
        driving video, no separate lip-sync). Raises AvatarError on any
        failure — the caller has no lower tier to fall back to."""
        py = _hallo2_venv_python(self.cfg)
        if not py.is_file():
            raise AvatarError(
                "Hallo2 venv not found — run `python avatar.py setup-hallo2` "
                "once (clones Hallo2 + downloads model weights, several GB; "
                "needs a CUDA GPU with plenty of VRAM — Hallo2 targets "
                "~A100-class cards, it will not run on a small 4GB laptop GPU)",
                detail=str(py))
        h2 = _h2cfg(self.cfg)
        payload = {
            "source_image": str(source_image),
            "driving_audio": str(driving_audio),
            "out_path": str(out_path),
            "hallo2_dir": str(_hallo2_dir(self.cfg)),
            "pretrained_dir": str(_hallo2_pretrained_dir(self.cfg)),
            "config": str(_hallo2_config(self.cfg)),
            "pose_weight": float(h2.get("pose_weight", 1.0)),
            "face_weight": float(h2.get("face_weight", 1.0)),
            "lip_weight": float(h2.get("lip_weight", 1.0)),
            "face_expand_ratio": float(h2.get("face_expand_ratio", 1.2)),
        }
        timeout = float(h2.get("timeout_s", 2400))
        log.info("animation: running Hallo2 via %s", py)
        try:
            proc = subprocess.run(
                [str(py), str(HALLO2_WORKER_PATH)], input=json.dumps(payload),
                capture_output=True, text=True, encoding="utf-8",
                timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise AvatarError(
                f"Hallo2 worker timed out after {timeout:.0f}s "
                "(raise avatar.animation.hallo2.timeout_s)",
                detail=(e.stderr or "")[-1000:] if isinstance(e.stderr, str)
                else None)
        except OSError as e:
            raise AvatarError(f"could not launch Hallo2 worker: {e}",
                              detail=str(py))

        stderr_tail = (proc.stderr or "")[-1000:]
        lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
        reply = None
        if lines:
            try:
                reply = json.loads(lines[-1])
            except json.JSONDecodeError:
                reply = None
        if reply is None:
            raise AvatarError(f"Hallo2 worker exited {proc.returncode}",
                              detail=stderr_tail)
        if not reply.get("ok"):
            raise AvatarError(
                f"Hallo2 failed: {reply.get('error', 'unknown error')}",
                detail=stderr_tail)
        if not out_path.is_file() or out_path.stat().st_size == 0:
            raise AvatarError(f"Hallo2 output missing or empty: {out_path}",
                              detail=stderr_tail)
        return out_path

    def _render_hallo2(self, audio_path: Path, image_path: Path,
                       duration_s: float, out_path: Path) -> Path:
        """avatar.animation.engine: hallo2 — animate the static avatar image
        directly from the TTS wav, then fit to the segment length and cut out
        the background to alpha. Mandatory: any failure raises AvatarError."""
        import ffutil
        log.info("ENTER AnimatedAvatarRenderer._render_hallo2() "
                 "source_image=%s audio=%s out=%s duration_s=%.2f",
                 image_path.resolve(), audio_path.resolve(),
                 out_path.resolve(), duration_s)
        key = _cache_key(image_path, audio_path, image_path, duration_s,
                         self.cfg)
        sidecar = out_path.with_suffix(".hash.json")
        if out_path.is_file() and sidecar.is_file():
            try:
                cached_key = json.loads(sidecar.read_text())["key"]
            except (OSError, json.JSONDecodeError, KeyError):
                cached_key = None
            if cached_key == key:
                log.info("animation cache hit: %s", out_path.name)
                return out_path

        tmp_dir = out_path.parent / f".anim_tmp_{out_path.stem}"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        talking_path = tmp_dir / "talking.mp4"
        self._run_hallo2(image_path, audio_path, talking_path)

        # Hallo2 output length follows the audio; the avatar segment is
        # duration_s (speech + freeze pad). tpad clones the last frame to
        # cover any shortfall, then -t caps the result at exactly duration_s.
        fitted = tmp_dir / "fitted.mp4"
        ffutil.run_ffmpeg([
            "-i", str(talking_path), "-t", f"{duration_s:.3f}",
            "-vf", f"tpad=stop_mode=clone:stop_duration={duration_s:.3f}",
            "-an", "-pix_fmt", "yuv420p", str(fitted)])
        self._segment_alpha(fitted, out_path)

        if not out_path.is_file() or out_path.stat().st_size == 0:
            raise AvatarError(
                f"animation pipeline reported success but output is "
                f"missing or empty: {out_path}")

        sidecar.write_text(json.dumps({"key": key, "lipsynced": True}),
                          encoding="utf-8")
        for f in (talking_path, fitted):
            f.unlink(missing_ok=True)
        try:
            tmp_dir.rmdir()
        except OSError:
            pass
        log.info("animation rendered (hallo2): %s (%.1fs) — verified on disk "
                 "(%d bytes)", out_path.name, duration_s,
                 out_path.stat().st_size)
        return out_path

    # -------------------------------------------------------------- public

    def _render_musetalk_only(self, audio_path: Path, image_path: Path,
                              duration_s: float, out_path: Path) -> Path:
        """avatar.animation.engine: musetalk_only — lip-sync the static
        avatar image directly to audio_path via MuseTalk, skipping
        LivePortrait's motion-transfer stage entirely (no driving video
        involved). Mandatory: MuseTalk failure raises AvatarError up through
        render_intro/render_outro to apply_avatar's existing
        fallback_to_static handling — there is no motion-only tier here
        since there's no motion stage to fall back to."""
        import ffutil
        log.info("ENTER AnimatedAvatarRenderer._render_musetalk_only() "
                 "source_image=%s audio=%s out=%s duration_s=%.2f",
                 image_path.resolve(), audio_path.resolve(),
                 out_path.resolve(), duration_s)
        key = _cache_key(image_path, audio_path, image_path, duration_s,
                         self.cfg)
        sidecar = out_path.with_suffix(".hash.json")
        if out_path.is_file() and sidecar.is_file():
            try:
                cached_key = json.loads(sidecar.read_text())["key"]
            except (OSError, json.JSONDecodeError, KeyError):
                cached_key = None
            if cached_key == key:
                log.info("animation cache hit: %s", out_path.name)
                return out_path

        tmp_dir = out_path.parent / f".anim_tmp_{out_path.stem}"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        raw_path = tmp_dir / "raw.mp4"
        ffutil.run_ffmpeg(["-loop", "1", "-i", image_path,
                           "-t", f"{duration_s:.3f}", "-r", "25",
                           "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                           "-pix_fmt", "yuv420p", raw_path])

        lipsynced_path = tmp_dir / "lipsynced.mp4"
        self._run_musetalk(raw_path, audio_path, lipsynced_path)
        self._segment_alpha(lipsynced_path, out_path)

        if not out_path.is_file() or out_path.stat().st_size == 0:
            raise AvatarError(
                f"animation pipeline reported success but output is "
                f"missing or empty: {out_path}")

        sidecar.write_text(json.dumps({"key": key, "lipsynced": True}),
                          encoding="utf-8")
        for f in (raw_path, lipsynced_path):
            f.unlink(missing_ok=True)
        try:
            tmp_dir.rmdir()
        except OSError:
            pass
        log.info("animation rendered (musetalk_only): %s (%.1fs) — "
                 "verified on disk (%d bytes)", out_path.name, duration_s,
                 out_path.stat().st_size)
        return out_path

    def render(self, audio_path: Path, image_path: Path, duration_s: float,
              out_path: Path) -> Path:
        """Produce a video-with-alpha of `image_path` animated for
        `duration_s` seconds, cached at `out_path` (a .mov). Raises
        AvatarError on any failure."""
        import ffutil
        image_path, audio_path = Path(image_path), Path(audio_path)
        log.info("ENTER AnimatedAvatarRenderer.render() "
                 "source_image=%s audio=%s out=%s duration_s=%.2f",
                 image_path.resolve(), audio_path.resolve(),
                 out_path.resolve(), duration_s)
        engine = str(_acfg(self.cfg).get("engine", "hallo2"))
        if engine == "hallo2":
            return self._render_hallo2(audio_path, image_path, duration_s,
                                       out_path)
        if engine == "musetalk_only":
            return self._render_musetalk_only(audio_path, image_path,
                                              duration_s, out_path)
        driving = _resolve_driving_video(self.cfg)
        log.info("  driving_video=%s", driving.resolve())
        key = _cache_key(image_path, audio_path, driving, duration_s, self.cfg)
        lip_sync_enabled = bool(_lscfg(self.cfg).get("enabled", False))
        sidecar = out_path.with_suffix(".hash.json")
        if out_path.is_file() and sidecar.is_file():
            try:
                cached = json.loads(sidecar.read_text())
                cached_key = cached["key"]
                cached_lipsynced = bool(cached.get("lipsynced", False))
            except (OSError, json.JSONDecodeError, KeyError):
                cached_key, cached_lipsynced = None, False
            # A cached render that fell back to motion-only (lip-sync failed
            # at render time, not a config difference — _cache_key can't see
            # it) must NOT be reused while lip_sync is enabled, or a
            # transient MuseTalk failure would be "cached as success"
            # forever. Config-unchanged + still motion-only + lip_sync now
            # enabled -> treat as a miss and retry.
            if cached_key == key and (not lip_sync_enabled or cached_lipsynced):
                log.info("animation cache hit: %s", out_path.name)
                return out_path

        tmp_dir = out_path.parent / f".anim_tmp_{out_path.stem}"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        trimmed_driving = tmp_dir / "driving.mp4"
        # loop the (short, bundled) driving video to cover duration_s, then
        # hard-trim to the exact length so the raw render matches the segment
        ffutil.run_ffmpeg(["-stream_loop", "-1", "-i", driving,
                           "-t", f"{duration_s:.3f}", "-c", "copy",
                           trimmed_driving])

        raw_path = tmp_dir / "raw.mp4"
        self._run_liveportrait(image_path, trimmed_driving, raw_path)

        source_for_alpha = raw_path
        lipsynced_path = None
        lipsynced_ok = False
        if lip_sync_enabled:
            lipsynced_path = tmp_dir / "lipsynced.mp4"
            try:
                self._run_musetalk(raw_path, audio_path, lipsynced_path)
                source_for_alpha = lipsynced_path
                lipsynced_ok = True
            except AvatarError as e:
                if _lscfg(self.cfg).get("fallback_to_motion_only", True):
                    log.warning(
                        "lip-sync FAILED — falling back to motion-only "
                        "(LivePortrait output, audio not synced to mouth "
                        "shapes). Reason: %s", e)
                else:
                    raise

        self._segment_alpha(source_for_alpha, out_path)

        if not out_path.is_file() or out_path.stat().st_size == 0:
            raise AvatarError(
                f"animation pipeline reported success but output is "
                f"missing or empty: {out_path}")

        sidecar.write_text(json.dumps({"key": key, "lipsynced": lipsynced_ok}),
                          encoding="utf-8")
        cleanup = [trimmed_driving, raw_path]
        if lipsynced_path is not None:
            cleanup.append(lipsynced_path)
        for f in cleanup:
            f.unlink(missing_ok=True)
        try:
            tmp_dir.rmdir()
        except OSError:
            pass
        log.info("animation rendered: %s (%.1fs) — verified on disk (%d "
                 "bytes)", out_path.name, duration_s, out_path.stat().st_size)
        return out_path

    def render_intro(self, item: dict, clip_dir: Path, image_path: Path,
                     intro_dur: float) -> Path:
        out = clip_dir / "avatar_intro.mov"
        return self.render(Path(item["intro_wav"]), image_path, intro_dur, out)

    def render_outro(self, item: dict, clip_dir: Path, image_path: Path,
                     outro_dur: float) -> Path:
        out = clip_dir / "avatar_outro.mov"
        return self.render(Path(item["outro_wav"]), image_path, outro_dur, out)


HALLO2_SETUP_INSTRUCTIONS = r"""
Hallo2 avatar setup - run on a machine with a LARGE CUDA GPU. Hallo2 targets
~A100-class cards; it will NOT run on a small 4GB laptop GPU (e.g. a GTX 1650).

1) Clone Hallo2 into cache/hallo2:
     git clone https://github.com/fudan-generative-vision/hallo2 cache/hallo2

2) Create the isolated env (Python 3.10) and install deps:
     py -3.10 -m venv .venv-hallo2
     .venv-hallo2\Scripts\python -m pip install --upgrade pip
     .venv-hallo2\Scripts\python -m pip install torch==2.2.2 torchvision==0.17.2 torchaudio==2.2.2
     .venv-hallo2\Scripts\python -m pip install -r cache/hallo2/requirements.txt
     .venv-hallo2\Scripts\python -m pip install "huggingface_hub[cli]" pyyaml

3) Download the model weights (several GB):
     .venv-hallo2\Scripts\huggingface-cli download fudan-generative-ai/hallo2 --local-dir cache/hallo2/pretrained_models

4) Ensure ffmpeg is installed and on PATH.

avatar.animation.engine is already 'hallo2' (config.yaml). Paths/weights are
tunable under avatar.animation.hallo2. Then use the Avatar Host tab as usual.
"""


def setup_hallo2() -> None:
    """Print Hallo2 setup steps. Deliberately does NOT auto-run the heavy,
    GPU/CUDA-specific install (Python 3.10 + torch + multi-GB weights) — those
    belong on the capable GPU box, run by the user against Hallo2's own docs."""
    print(HALLO2_SETUP_INSTRUCTIONS)


def _pick_anim_python_launcher() -> list[str]:
    """LivePortrait is tested against Python 3.9/3.10; prefer the `py`
    launcher and try 3.12 -> 3.11 -> 3.10 (newest-first — most dev machines
    only have recent versions installed, and venv creation itself doesn't
    care which of these it uses). Falls back to the running interpreter
    (sys.executable) if `py` is missing or none of those versions resolve,
    so setup still works on a bare 3.11/3.12-only machine."""
    import shutil
    launcher = shutil.which("py")
    if launcher:
        for ver in ("-3.12", "-3.11", "-3.10"):
            probe = subprocess.run([launcher, ver, "--version"],
                                   capture_output=True)
            if probe.returncode == 0:
                return [launcher, ver]
    return [sys.executable]


def _flatten_requirements(req_path: Path, _seen: set | None = None) -> str:
    """Inline `-r <file>` includes so a pin fix applied to the combined text
    also reaches lines that only exist in an included file — LivePortrait's
    requirements.txt is just `-r requirements_base.txt` plus two lines, and
    lmdb is pinned inside requirements_base.txt, not requirements.txt
    itself. `_seen` guards against an include cycle."""
    _seen = _seen if _seen is not None else set()
    if req_path in _seen or not req_path.is_file():
        return ""
    _seen.add(req_path)
    lines = []
    for line in req_path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^-r\s+(\S+)\s*$", line)
        if m:
            lines.append(_flatten_requirements(
                req_path.parent / m.group(1).strip(), _seen))
        else:
            lines.append(line)
    return "\n".join(lines)


def _install_liveportrait_requirements(exe: Path, req_path: Path) -> None:
    """Install LivePortrait's requirements. On Windows, LivePortrait's
    pinned lmdb==1.4.1 (in requirements_base.txt, pulled in via `-r`) has no
    prebuilt wheel for recent Python (3.12+) and its sdist build needs the
    patch-ng build dependency, which fails there — flatten the `-r` includes
    and relax that one pin to a range with Windows wheels before installing.
    Linux keeps the exact pin (manylinux wheels for 1.4.1 exist there), so
    this only touches the file pip actually installs from on Windows."""
    from avatar import _run_step  # same fail-loud subprocess runner

    if sys.platform == "win32":
        flattened = _flatten_requirements(req_path)
        patched = re.sub(r"(?m)^lmdb==.*$", "lmdb>=1.6,<2", flattened)
        if patched != flattened:
            win_req = req_path.with_name("requirements.windows.txt")
            win_req.write_text(patched, encoding="utf-8")
            _run_step([exe, "-m", "pip", "install", "-r", str(win_req)],
                      "install LivePortrait requirements "
                      "(lmdb pin relaxed for Windows)")
            return
    _run_step([exe, "-m", "pip", "install", "-r", str(req_path)],
              "install LivePortrait requirements")


def _py_eval(exe: Path, code: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(exe), "-c", code], capture_output=True,
                          text=True)


def _fix_onnxruntime_provider(exe: Path) -> None:
    """LivePortrait's requirements.txt pins onnxruntime-gpu==1.18.0 for
    insightface's face detector. That wheel's CUDAExecutionProvider needs
    CUDA 11.8 + cuDNN 8 specifically — confirmed by inspecting
    onnxruntime_providers_cuda.dll's actual DLL imports via `pefile`
    (cublas64_11.dll, cudart64_110.dll, cudnn64_8.dll, cufft64_10.dll), NOT
    the commonly-assumed CUDA12/cuDNN9 pairing. A system CUDA Toolkit
    install isn't required for this: the pip-only nvidia-cuda-runtime-cu11/
    nvidia-cublas-cu11/nvidia-cudnn-cu11==8.9.5.29/nvidia-cufft-cu11 wheels
    ship the matching DLLs, and liveportrait_worker.py already puts their
    bin dirs on the subprocess PATH before launching inference.py. Verified
    end-to-end: onnxruntime-gpu's CUDA execution provider loads and runs
    correctly once these are present — a real CUDA face-detector run, not a
    slow CPU fallback.

    An earlier version of this function assumed onnxruntime-gpu could not
    work at all here and swapped it for the CPU-only onnxruntime package
    instead — that traded a ~600s-timeout failure for a much slower
    (CPU-bound) but "working" face detector, and is now known unnecessary.
    Ensure onnxruntime-gpu (not CPU-only onnxruntime) plus the matching DLL
    wheels are installed, upgrading in place if a prior run left the
    CPU-only package instead."""
    from avatar import _run_step

    def _pip_show(pkg: str) -> bool:
        return subprocess.run([str(exe), "-m", "pip", "show", pkg],
                              capture_output=True).returncode == 0

    if _pip_show("onnxruntime") and not _pip_show("onnxruntime-gpu"):
        log.info("CPU-only onnxruntime found (from a prior setup run) — "
                 "replacing with onnxruntime-gpu now that its CUDA provider "
                 "is confirmed working via matching cu11 DLL wheels")
        _run_step([str(exe), "-m", "pip", "uninstall", "-y", "onnxruntime"],
                  "remove CPU-only onnxruntime")

    if not _pip_show("onnxruntime-gpu"):
        _run_step([str(exe), "-m", "pip", "install", "onnxruntime-gpu==1.18.0"],
                  "install onnxruntime-gpu")

    _run_step([str(exe), "-m", "pip", "install",
              "nvidia-cuda-runtime-cu11", "nvidia-cublas-cu11",
              "nvidia-cudnn-cu11==8.9.5.29", "nvidia-cufft-cu11"],
              "install matching CUDA 11.8/cuDNN 8 DLL wheels for "
              "onnxruntime-gpu's CUDAExecutionProvider")


def _detect_cuda_index_url() -> str | None:
    """PyTorch wheel index matching this host's NVIDIA driver, or None for
    CPU-only. Probed via nvidia-smi (same signal ffutil.nvenc_available()
    uses) rather than assumed. torch==2.3.0's cu121 build covers current
    drivers; README also lists cu118 for older ones, but cu121 is the safer
    default and downgrades cleanly to CPU below if it doesn't actually work
    at runtime."""
    import shutil
    smi = shutil.which("nvidia-smi")
    if not smi:
        return None
    try:
        proc = subprocess.run([smi], capture_output=True, text=True,
                              timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return "https://download.pytorch.org/whl/cu121" if proc.returncode == 0 \
        else None


def _install_torch(exe: Path) -> str:
    """Install torch==2.3.0 / torchvision==0.18.0 / torchaudio==2.3.0 —
    required by LivePortrait's own pipeline code but deliberately NOT listed
    in its requirements.txt (the README has users `pip install` it
    separately, one command per CUDA version). That missing step is why the
    smoke test previously failed at `import torch` with ModuleNotFoundError.

    Tries a CUDA build when an NVIDIA GPU is present, verifies
    torch.cuda.is_available() is actually True at runtime (not just that the
    import succeeds — a CUDA wheel can import fine and still see no usable
    device), and falls back to the CPU-only build on any failure. Returns
    'cuda' or 'cpu' for whichever ended up usable. Idempotent: leaves an
    already-working install alone."""
    from avatar import _run_step

    have = _py_eval(exe, "import torch; print(torch.__version__)")
    if have.returncode == 0:
        cuda_probe = _py_eval(
            exe, "import torch; print(torch.cuda.is_available())")
        device = "cuda" if (cuda_probe.returncode == 0
                            and cuda_probe.stdout.strip() == "True") else "cpu"
        log.info("torch already installed (%s) — device=%s",
                 have.stdout.strip(), device)
        return device

    pkgs = ["torch==2.3.0", "torchvision==0.18.0", "torchaudio==2.3.0"]
    index_url = _detect_cuda_index_url()
    if index_url:
        log.info("NVIDIA GPU detected — installing CUDA torch (%s)",
                 index_url)
        _run_step([str(exe), "-m", "pip", "install", *pkgs,
                  "--index-url", index_url], "install torch (CUDA build)")
        cuda_probe = _py_eval(
            exe, "import torch; print(torch.cuda.is_available())")
        if cuda_probe.returncode == 0 and cuda_probe.stdout.strip() == "True":
            return "cuda"
        log.warning(
            "CUDA torch installed but torch.cuda.is_available() is False "
            "(%s) — reinstalling the CPU-only build",
            (cuda_probe.stderr or cuda_probe.stdout or "").strip()[-500:])

    log.info("installing CPU-only torch")
    _run_step([str(exe), "-m", "pip", "install", "--force-reinstall", *pkgs,
              "--index-url", "https://download.pytorch.org/whl/cpu"],
             "install torch (CPU build)")
    return "cpu"


def _print_torch_diagnostics(exe: Path) -> None:
    for label, code in [
        ("torch", "import torch; print(torch.__version__)"),
        ("torchvision", "import torchvision; print(torchvision.__version__)"),
        ("onnxruntime", "import onnxruntime; print(onnxruntime.__version__)"),
        ("torch.cuda.is_available()",
         "import torch; print(torch.cuda.is_available())"),
    ]:
        r = _py_eval(exe, code)
        value = r.stdout.strip() if r.returncode == 0 else \
            f"FAILED: {(r.stderr or '').strip()[-300:]}"
        print(f"  {label}: {value}")


def _hub_version(exe: Path) -> str | None:
    probe = subprocess.run(
        [str(exe), "-c",
         "import huggingface_hub; print(huggingface_hub.__version__)"],
        capture_output=True, text=True)
    return probe.stdout.strip() if probe.returncode == 0 else None


def _ensure_huggingface_hub(exe: Path) -> None:
    """Install huggingface_hub only if it's missing — never upgrade an
    existing install. An unconstrained `pip install huggingface_hub` is what
    previously broke this venv: it pulled a version newer than the pinned
    transformers==4.38.0 supports. A fresh install is constrained to a range
    known compatible with transformers 4.38.0 so that doesn't repeat."""
    have = _hub_version(exe)
    if have:
        log.info("huggingface_hub already installed (%s) — leaving as is",
                 have)
        return
    from avatar import _run_step
    _run_step([str(exe), "-m", "pip", "install",
              "huggingface_hub>=0.20,<0.25"], "install huggingface_hub")


# Required for the human pipeline (this feature never uses LivePortrait's
# animals mode) — paths per src/config/inference_config.py + crop_config.py
# in the cloned repo. A directory existing (or a lone .gitkeep from the git
# clone) proves nothing; only these specific files count as "installed".
_REQUIRED_WEIGHT_FILES = [
    "liveportrait/base_models/appearance_feature_extractor.pth",
    "liveportrait/base_models/motion_extractor.pth",
    "liveportrait/base_models/spade_generator.pth",
    "liveportrait/base_models/warping_module.pth",
    "liveportrait/retargeting_models/stitching_retargeting_module.pth",
    "liveportrait/landmark.onnx",
]


def _missing_weight_files(weights_dir: Path) -> list[str]:
    """Files from _REQUIRED_WEIGHT_FILES that are absent OR zero-byte — a
    0-byte file is what an interrupted/failed download leaves behind, so it
    must count as missing, not present."""
    return [rel for rel in _REQUIRED_WEIGHT_FILES
            if not (weights_dir / rel).is_file()
            or (weights_dir / rel).stat().st_size == 0]


def _download_liveportrait_weights(exe: Path, lp_dir: Path) -> None:
    """Fetch pretrained_weights/ via the huggingface_hub Python API
    (snapshot_download) — no huggingface-cli, no manual step. Skips only when
    every required weight file is actually present and non-empty; retries
    transient failures (snapshot_download itself resumes partial downloads
    via HTTP range requests, so a retry picks up where it left off rather
    than restarting). Raises AvatarError — never prints success — if the
    required files are still missing/empty afterward."""
    from avatar import _run_step

    weights_dir = lp_dir / "pretrained_weights"
    missing = _missing_weight_files(weights_dir)
    if not missing:
        print("Found:")
        for rel in _REQUIRED_WEIGHT_FILES:
            print(f"  {Path(rel).name}")
        return

    _ensure_huggingface_hub(exe)
    script = (
        "from huggingface_hub import snapshot_download\n"
        "snapshot_download(\n"
        "    repo_id='KlingTeam/LivePortrait',\n"
        f"    local_dir={str(weights_dir)!r},\n"
        "    ignore_patterns=['*.git*', 'README.md', 'docs'],\n"
        ")\n"
    )
    attempts = 3
    last_err: AvatarError | None = None
    for attempt in range(1, attempts + 1):
        try:
            _run_step([str(exe), "-c", script],
                      f"download LivePortrait pretrained weights "
                      f"(attempt {attempt}/{attempts})")
            last_err = None
            break
        except AvatarError as e:
            last_err = e
            if attempt < attempts:
                wait = 5 * attempt
                log.warning("weight download attempt %d/%d failed (%s) — "
                           "retrying in %ds", attempt, attempts, e, wait)
                time.sleep(wait)
    if last_err is not None:
        raise AvatarError(
            f"LivePortrait weight download failed after {attempts} "
            f"attempts: {last_err}")

    missing = _missing_weight_files(weights_dir)
    if missing:
        raise AvatarError(
            "LivePortrait weight download completed but required files are "
            "still missing or empty: " + ", ".join(missing),
            detail=str(weights_dir))

    print("Found:")
    for rel in _REQUIRED_WEIGHT_FILES:
        print(f"  {Path(rel).name}")


def _smoke_test_inference(exe: Path, lp_dir: Path, device: str = "auto") -> Path:
    """Run one real inference using LivePortrait's own bundled example
    assets, proving the venv + weights actually work end-to-end — not just
    that the files exist. `device` == 'cpu' passes --flag-force-cpu (same
    flag liveportrait_worker.py uses). Raises AvatarError with the FULL
    captured stderr/stdout (untruncated — a truncated traceback is what hid
    the real `import torch` failure last time) on any failure; returns the
    produced video's path on success."""
    import shutil

    source = lp_dir / "assets" / "examples" / "source" / "s9.jpg"
    driving = lp_dir / "assets" / "examples" / "driving" / "d0.mp4"
    if not source.is_file() or not driving.is_file():
        raise AvatarError(
            "LivePortrait example assets missing — clone may be incomplete",
            detail=f"{source} / {driving}")

    out_dir = lp_dir / "animations_smoke_test"
    if out_dir.is_dir():
        shutil.rmtree(out_dir)
    args = [str(exe), "inference.py", "-s", str(source), "-d", str(driving),
            "-o", str(out_dir)]
    if device == "cpu":
        args.append("--flag-force-cpu")
    log.info("running LivePortrait smoke test: %s", " ".join(args))
    try:
        proc = subprocess.run(args, cwd=str(lp_dir), capture_output=True,
                              text=True, encoding="utf-8", timeout=900,
                              env=_UTF8_ENV)
    except subprocess.TimeoutExpired as e:
        raise AvatarError("LivePortrait smoke test timed out after 900s",
                          detail=(e.stderr or "") if isinstance(e.stderr, str)
                          else None)
    if proc.returncode != 0:
        raise AvatarError("LivePortrait smoke test inference failed",
                          detail=proc.stderr or proc.stdout or "")

    produced = sorted(out_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime) \
        if out_dir.is_dir() else []
    if not produced:
        raise AvatarError("LivePortrait smoke test produced no output video",
                          detail=proc.stderr or proc.stdout or "")
    return produced[-1]


def setup_anim_venv() -> None:
    """Create .venv-avatar-anim and clone + install LivePortrait into
    cache/liveportrait/. Idempotent: reuses an existing venv/clone rather
    than recreating them."""
    import shutil
    from avatar import _run_step  # same fail-loud subprocess runner

    exe = ANIM_VENV_DIR / "Scripts" / "python.exe"
    print("Setting up the isolated animation venv (.venv-avatar-anim).")
    print("This clones LivePortrait and installs its torch stack "
          "(~2-3 GB) — one time only.")
    if exe.is_file():
        print(f"Reusing existing animation venv: {exe}")
    else:
        _run_step(_pick_anim_python_launcher() + ["-m", "venv",
                                                   str(ANIM_VENV_DIR)],
                  "create venv")

    lp_dir = LIVEPORTRAIT_DIR
    if not (lp_dir / "inference.py").is_file():
        lp_dir.parent.mkdir(parents=True, exist_ok=True)
        git = shutil.which("git")
        if not git:
            raise AvatarError("git not found on PATH — required to clone "
                              "KwaiVGI/LivePortrait")
        _run_step([git, "clone", "--depth", "1",
                  "https://github.com/KwaiVGI/LivePortrait.git", str(lp_dir)],
                 "clone LivePortrait")

    req = lp_dir / "requirements.txt"
    _run_step([exe, "-m", "pip", "install", "--upgrade", "pip"], "upgrade pip")
    if req.is_file():
        _install_liveportrait_requirements(exe, req)
    _fix_onnxruntime_provider(exe)
    # torch is deliberately absent from LivePortrait's requirements.txt (its
    # README has users install it separately, one command per CUDA version)
    # — without this step `import torch` fails inside live_portrait_pipeline.py.
    device = _install_torch(exe)
    _download_liveportrait_weights(exe, lp_dir)
    print(f"Animation venv ready: {exe}")
    print(f"LivePortrait cloned to: {lp_dir}")
    print(f"Pretrained weights: {lp_dir / 'pretrained_weights'}")
    print(f"Device: {device}")
    print("Diagnostics:")
    _print_torch_diagnostics(exe)

    print("Running smoke test inference (LivePortrait's own example "
          "assets) to confirm the install actually works...")
    out_video = _smoke_test_inference(exe, lp_dir, device=device)
    print(f"Smoke test passed — output video: {out_video}")

    print("Next: drop a short generic talking-head clip at "
          f"{DEFAULT_DRIVING_VIDEO.relative_to(ROOT)} (or set "
          "avatar.animation.driving_video), then set "
          "avatar.animation.enabled: true.")


# ------------------------------------------------- MuseTalk lip-sync setup

def _pick_musetalk_python_launcher() -> list[str]:
    """MuseTalk's README recommends Python 3.10, but its actual pinned
    wheels are not that narrow: confirmed directly against the wheel
    indexes (not assumed) that torch==2.0.1+cu118 publishes a cp311 wheel
    (torch-2.0.1+cu118-cp311-cp311-win_amd64.whl at
    download.pytorch.org/whl/cu118/torch/) and mmcv==2.0.1 publishes a
    matching cp311 Windows wheel too (download.openmmlab.com/mmcv/dist/
    cu118/torch2.0.0/index.html). So unlike a hypothetical hard-3.10-only
    requirement, 3.10 -> 3.11 is a safe cascade (still narrower than
    LivePortrait's 3.12->3.10 cascade, since 3.12 wheels for this exact pin
    combo are NOT confirmed to exist). Fails clearly if neither is
    available, rather than silently trying an unconfirmed interpreter."""
    import shutil
    launcher = shutil.which("py")
    if launcher:
        for ver in ("-3.10", "-3.11"):
            probe = subprocess.run([launcher, ver, "--version"],
                                   capture_output=True)
            if probe.returncode == 0:
                return [launcher, ver]
    raise AvatarError(
        "Neither Python 3.10 nor 3.11 found via the `py` launcher — "
        "MuseTalk pins torch==2.0.1, confirmed to have prebuilt wheels for "
        "cp310/cp311 only, and its mmcv/mmdet/mmpose stack matches. Install "
        "Python 3.10 (py.exe launcher will then find it as `py -3.10`) "
        "before running setup-musetalk-venv.")


def _install_musetalk_requirements(exe: Path, req_path: Path) -> None:
    """Plain install — no Windows pin-relaxation is known to be needed here
    (unlike LivePortrait's lmdb pin). Add targeted patching only if a real
    setup run actually hits a Windows-wheel gap, not speculatively."""
    from avatar import _run_step
    _run_step([exe, "-m", "pip", "install", "-r", str(req_path)],
              "install MuseTalk requirements")


def _install_mmcv_stack(exe: Path) -> None:
    """MuseTalk depends on the OpenMMLab stack (mmengine/mmcv/mmdet/mmpose)
    for face/pose detection, installed via their `mim` tool rather than
    plain pip so it resolves the prebuilt wheel matching the installed torch
    + CUDA build. Windows win_amd64 wheels for mmcv 2.0.1 against
    torch2.0.1+cu118/cp311 are confirmed to exist at
    download.openmmlab.com/mmcv/dist/cu118/torch2.0.0/index.html.

    mmpose==1.1.0 hard-depends on `chumpy` (confirmed via PyPI's own
    requires_dist for that release — not an optional/extra), which is
    unmaintained (last released ~2019) and its setup.py does `import pip`
    to read requirements — a pattern modern pip's isolated build
    environments deliberately don't support (`ModuleNotFoundError: No
    module named 'pip'`), reproduced live. The standard fix (not a
    version-specific hack — this is chumpy's well-known incompatibility
    with any modern pip) is installing it with --no-build-isolation so it
    uses this venv's already-installed pip/numpy/six directly instead of an
    isolated build env that excludes pip."""
    from avatar import _run_step
    _run_step([exe, "-m", "pip", "install", "--no-cache-dir", "-U", "openmim"],
              "install openmim")
    _run_step([exe, "-m", "pip", "install", "--no-build-isolation", "chumpy"],
              "install chumpy (mmpose dependency, needs --no-build-isolation)")
    _run_step([exe, "-m", "mim", "install", "mmengine", "mmcv==2.0.1",
              "mmdet==3.1.0", "mmpose==1.1.0"], "install mmcv/mmdet/mmpose")


def _fix_musetalk_numpy_pin(exe: Path) -> None:
    """MuseTalk's requirements.txt pins numpy==1.23.5, and that exact
    version is the only one satisfying every installed consumer at once:
    opencv-python==4.9.0 (compiled against the numpy 1.x ABI — anything 2.x
    raises `_ARRAY_API not found` / `numpy.core.multiarray failed to
    import`), tensorflow-intel==2.12.0 (pulled in transitively, pins
    numpy<1.24,>=1.22), and matplotlib (imported transitively via
    xtcocotools, a mmpose dependency — its `_check_versions()` floor varies
    by release). Two DIFFERENT unpinned transitive installs were each
    reproduced live silently bumping numpy off 1.23.5: mim-installing the
    OpenMMLab stack (mmcv/mmdet/mmpose have no numpy upper bound) pushed it
    to 2.4.6; separately, `pip install matplotlib<3.9` (needed because the
    resolved matplotlib>=3.10 wants numpy>=1.25) re-resolved and pushed
    numpy back to 2.4.6 too, since pip re-solves the whole environment on
    every install and nothing already on disk pins numpy down.

    Fix, in order: downgrade matplotlib to a release whose numpy floor
    matches 1.23.5, THEN re-pin numpy==1.23.5 as the unconditional last
    write — this function must be the last thing setup_musetalk_venv calls
    before weight download/smoke test, or a later install can undo it
    again exactly like matplotlib's did here."""
    from avatar import _run_step
    _run_step([str(exe), "-m", "pip", "install", "matplotlib<3.9"],
              "install matplotlib<3.9 (numpy 1.23.5-compatible)")

    have = _py_eval(exe, "import numpy; print(numpy.__version__)")
    if have.returncode == 0 and have.stdout.strip() == "1.23.5":
        log.info("numpy already pinned correctly (1.23.5)")
        return
    log.warning(
        "numpy is %s but MuseTalk's requirements.txt pins 1.23.5 (an "
        "unpinned transitive dependency bumped it again) — re-pinning",
        have.stdout.strip() if have.returncode == 0 else "not importable")
    _run_step([str(exe), "-m", "pip", "install", "--force-reinstall",
              "--no-deps", "numpy==1.23.5"], "re-pin numpy to 1.23.5")


def _install_musetalk_torch(exe: Path) -> str:
    """Install torch==2.0.1 (MuseTalk's pinned version, README-tested) with
    matching torchvision==0.15.2/torchaudio==2.0.2 — the official paired
    release versions for that torch build.

    Unlike _install_torch (used for LivePortrait, which never appears in
    LivePortrait's own requirements.txt, so "torch present" can only mean
    "installed by a previous run of this function"), MuseTalk's own
    requirements.txt pins accelerate==0.28.0, which declares an unpinned
    torch>=1.10.0 — confirmed live: a plain `pip install -r requirements.txt`
    pulled in torch 2.13.0 (CPU-only, latest available) as a side effect,
    entirely bypassing this function, and the downstream mmcv install then
    failed looking for a nonexistent torch2.13.0 wheel index. So "torch
    already installed" here does NOT necessarily mean "correctly installed
    by us" — only treat it as satisfied if it's actually torch==2.0.1;
    otherwise force-reinstall the pinned version over whatever is there."""
    from avatar import _run_step

    have = _py_eval(exe, "import torch; print(torch.__version__)")
    have_version = have.stdout.strip().split("+")[0] if have.returncode == 0 else None
    if have_version == "2.0.1":
        cuda_probe = _py_eval(
            exe, "import torch; print(torch.cuda.is_available())")
        device = "cuda" if (cuda_probe.returncode == 0
                            and cuda_probe.stdout.strip() == "True") else "cpu"
        log.info("torch already installed (%s) — device=%s",
                 have.stdout.strip(), device)
        return device
    if have_version is not None:
        log.warning(
            "torch %s is installed (pulled in as an unpinned transitive "
            "dependency of MuseTalk's own requirements.txt, not by this "
            "function) but MuseTalk needs exactly torch==2.0.1 — "
            "force-reinstalling the pinned version", have.stdout.strip())

    pkgs = ["torch==2.0.1", "torchvision==0.15.2", "torchaudio==2.0.2"]
    # _detect_cuda_index_url() returns a cu121 URL (LivePortrait's pin) —
    # reused here only as the nvidia-smi presence probe; MuseTalk needs its
    # own cu118-pinned URL below regardless of what that function returns.
    gpu_present = _detect_cuda_index_url() is not None
    if gpu_present:
        cu118_url = "https://download.pytorch.org/whl/cu118"
        log.info("NVIDIA GPU detected — installing CUDA torch (%s)", cu118_url)
        _run_step([str(exe), "-m", "pip", "install", "--force-reinstall", *pkgs,
                  "--index-url", cu118_url], "install torch (CUDA build)")
        cuda_probe = _py_eval(
            exe, "import torch; print(torch.cuda.is_available())")
        if cuda_probe.returncode == 0 and cuda_probe.stdout.strip() == "True":
            return "cuda"
        log.warning(
            "CUDA torch installed but torch.cuda.is_available() is False "
            "(%s) — MuseTalk's CLI has no confirmed CPU inference path, so "
            "lip-sync will not work until this is fixed",
            (cuda_probe.stderr or cuda_probe.stdout or "").strip()[-500:])

    log.info("installing CPU-only torch (MuseTalk lip-sync will not be "
             "usable — no NVIDIA GPU detected)")
    _run_step([str(exe), "-m", "pip", "install", "--force-reinstall", *pkgs,
              "--index-url", "https://download.pytorch.org/whl/cpu"],
             "install torch (CPU build)")
    return "cpu"


# Weight subdirectories/files per MuseTalk's documented models/ layout.
_REQUIRED_MUSETALK_WEIGHT_FILES = [
    "musetalk/musetalk.json",
    "musetalk/pytorch_model.bin",
    "musetalkV15/musetalk.json",
    "musetalkV15/unet.pth",
    "dwpose/dw-ll_ucoco_384.pth",
    "face-parse-bisent/79999_iter.pth",
    "face-parse-bisent/resnet18-5c106cde.pth",
    "sd-vae/config.json",
    "sd-vae/diffusion_pytorch_model.bin",
    "whisper/config.json",
    "whisper/pytorch_model.bin",
    "whisper/preprocessor_config.json",
]


def _missing_musetalk_weight_files(weights_dir: Path) -> list[str]:
    return [rel for rel in _REQUIRED_MUSETALK_WEIGHT_FILES
            if not (weights_dir / rel).is_file()
            or (weights_dir / rel).stat().st_size == 0]


def _ensure_gdown(exe: Path) -> None:
    check = subprocess.run([str(exe), "-c", "import gdown"],
                           capture_output=True)
    if check.returncode == 0:
        return
    from avatar import _run_step
    _run_step([str(exe), "-m", "pip", "install", "gdown"], "install gdown")


def _download_face_parse_weights(exe: Path, weights_dir: Path) -> None:
    """The two face-parse-bisent files come from different non-HF sources
    per MuseTalk's official download_weights.sh (verified against that
    script directly, not assumed):
      - 79999_iter.pth: Google Drive, file id 154JgKpzCPW82qINcVieuPH3fZ2e0P812
        (`gdown --id 154JgKpzCPW82qINcVieuPH3fZ2e0P812`) — this is the single
        highest-automation-risk step in this setup: Google Drive large-file
        downloads need cookie/confirm-token handling (gdown handles this
        today, but it's inherently fragile to Google changing the
        interstitial page).
      - resnet18-5c106cde.pth: a plain HTTPS download from
        https://download.pytorch.org/models/resnet18-5c106cde.pth — NOT
        Google Drive, no gdown needed.
    On failure, raise AvatarError with the exact manual-download URL/target
    path rather than leaving a half-installed venv silently reported as
    ready — do not attempt a scraper or mirror search here."""
    import urllib.request

    target_dir = weights_dir / "face-parse-bisent"
    target_dir.mkdir(parents=True, exist_ok=True)

    resnet_out = target_dir / "resnet18-5c106cde.pth"
    if not resnet_out.is_file() or resnet_out.stat().st_size == 0:
        resnet_url = "https://download.pytorch.org/models/resnet18-5c106cde.pth"
        try:
            urllib.request.urlretrieve(resnet_url, resnet_out)
        except OSError as e:
            raise AvatarError(
                f"download of resnet18-5c106cde.pth failed: {e} — download "
                f"it manually from {resnet_url} and place it at {resnet_out}")
        if not resnet_out.is_file() or resnet_out.stat().st_size == 0:
            raise AvatarError(
                "resnet18-5c106cde.pth download completed but file is "
                f"missing or empty: {resnet_out}")

    iter_out = target_dir / "79999_iter.pth"
    if not iter_out.is_file() or iter_out.stat().st_size == 0:
        file_id = "154JgKpzCPW82qINcVieuPH3fZ2e0P812"
        _ensure_gdown(exe)
        script = (
            "import gdown\n"
            f"gdown.download(id={file_id!r}, output={str(iter_out)!r}, "
            "quiet=False)\n"
        )
        proc = subprocess.run([str(exe), "-c", script])
        if proc.returncode != 0 or not iter_out.is_file() \
                or iter_out.stat().st_size == 0:
            raise AvatarError(
                "automated download of 79999_iter.pth (face-parse-bisent, "
                "Google Drive) failed — download it manually from "
                "MuseTalk's official links (see "
                "https://github.com/TMElyralab/MuseTalk#download-weights) "
                f"and place it at {iter_out}",
                detail=f"gdown file id: {file_id}")


def _download_musetalk_weights(exe: Path, mt_dir: Path) -> None:
    """Fetch models/ via per-source huggingface_hub snapshot_download calls
    (different weight groups come from different HF repos) plus a gdown
    fallback for the two Google-Drive-only face-parse-bisent files. Mirrors
    _download_liveportrait_weights's skip-if-present / retry / fail-loud
    shape."""
    from avatar import _run_step

    weights_dir = mt_dir / "models"
    missing = _missing_musetalk_weight_files(weights_dir)
    if not missing:
        print("Found:")
        for rel in _REQUIRED_MUSETALK_WEIGHT_FILES:
            print(f"  {rel}")
        return

    # ASSUMPTION: TMElyralab/MuseTalk's HF repo mirrors the models/ layout
    # documented in its README (musetalk/, musetalkV15/, syncnet/ at repo
    # root) so snapshot_download(local_dir=weights_dir) lands them directly
    # in place — not independently verified against the repo's actual file
    # tree (out of scope for this code-only pass). If the layout differs,
    # this will surface immediately as a _missing_musetalk_weight_files
    # failure below, not a silent wrong-path issue.
    _ensure_huggingface_hub(exe)
    sources = [
        ("TMElyralab/MuseTalk", weights_dir),
        ("stabilityai/sd-vae-ft-mse", weights_dir / "sd-vae"),
        ("openai/whisper-tiny", weights_dir / "whisper"),
        ("yzd-v/DWPose", weights_dir / "dwpose"),
    ]
    for repo_id, local_dir in sources:
        script = (
            "from huggingface_hub import snapshot_download\n"
            "snapshot_download(\n"
            f"    repo_id={repo_id!r},\n"
            f"    local_dir={str(local_dir)!r},\n"
            "    ignore_patterns=['*.git*', 'README.md', 'docs'],\n"
            ")\n"
        )
        attempts, last_err = 3, None
        for attempt in range(1, attempts + 1):
            try:
                _run_step([str(exe), "-c", script],
                         f"download {repo_id} weights (attempt "
                         f"{attempt}/{attempts})")
                last_err = None
                break
            except AvatarError as e:
                last_err = e
                if attempt < attempts:
                    wait = 5 * attempt
                    log.warning("%s download attempt %d/%d failed (%s) — "
                               "retrying in %ds", repo_id, attempt, attempts,
                               e, wait)
                    time.sleep(wait)
        if last_err is not None:
            raise AvatarError(
                f"MuseTalk weight download failed for {repo_id} after "
                f"{attempts} attempts: {last_err}")

    _download_face_parse_weights(exe, weights_dir)

    missing = _missing_musetalk_weight_files(weights_dir)
    if missing:
        raise AvatarError(
            "MuseTalk weight download completed but required files are "
            "still missing or empty: " + ", ".join(missing),
            detail=str(weights_dir))

    print("Found:")
    for rel in _REQUIRED_MUSETALK_WEIGHT_FILES:
        print(f"  {rel}")


def _smoke_test_musetalk(exe: Path, mt_dir: Path) -> Path:
    """Run one real inference using MuseTalk's own bundled example assets
    (data/video/yongen.mp4 + data/audio/yongen.wav — the same defaults shown
    in its own configs/inference/test.yaml) via musetalk_worker.py's own
    protocol, proving the venv + weights actually work end-to-end. Raises
    AvatarError with full untruncated stderr on failure; returns the
    produced video's path on success."""
    import ffutil

    source = mt_dir / "data" / "video" / "yongen.mp4"
    audio = mt_dir / "data" / "audio" / "yongen.wav"
    if not source.is_file() or not audio.is_file():
        raise AvatarError(
            "MuseTalk example assets missing — clone may be incomplete",
            detail=f"{source} / {audio}")

    out_dir = mt_dir / "animations_smoke_test"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "smoke_test.mp4"
    out_path.unlink(missing_ok=True)
    payload = {
        "raw_video": str(source),
        "audio_path": str(audio),
        "out_path": str(out_path),
        "musetalk_dir": str(mt_dir),
        "bbox_shift": 0,
        "version": "v15",
        "ffmpeg_dir": str(Path(ffutil.ffmpeg_bin()).resolve().parent),
    }
    log.info("running MuseTalk smoke test via %s", exe)
    try:
        proc = subprocess.run([str(exe), str(MUSETALK_WORKER_PATH)],
                              input=json.dumps(payload), capture_output=True,
                              text=True, encoding="utf-8", timeout=1200)
    except subprocess.TimeoutExpired as e:
        raise AvatarError("MuseTalk smoke test timed out after 1200s",
                          detail=(e.stderr or "") if isinstance(e.stderr, str)
                          else None)
    stderr_tail = (proc.stderr or "")[-2000:]
    lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
    reply = json.loads(lines[-1]) if lines else None
    if reply is None or not reply.get("ok"):
        raise AvatarError(
            "MuseTalk smoke test inference failed: "
            f"{reply.get('error') if reply else 'no reply'}",
            detail=stderr_tail)
    return Path(reply["out_path"])


def setup_musetalk_venv() -> None:
    """Create .venv-musetalk and clone + install MuseTalk into
    cache/musetalk/. Idempotent: reuses an existing venv/clone rather than
    recreating them. Mirrors setup_anim_venv()'s structure/order."""
    import shutil
    from avatar import _run_step  # same fail-loud subprocess runner

    exe = MUSETALK_VENV_DIR / "Scripts" / "python.exe"
    print("Setting up the isolated lip-sync venv (.venv-musetalk).")
    print("This clones MuseTalk and installs its torch + OpenMMLab stack "
          "(~3-4 GB) plus several GB of model weights — one time only.")
    if exe.is_file():
        print(f"Reusing existing lip-sync venv: {exe}")
    else:
        _run_step(_pick_musetalk_python_launcher() + ["-m", "venv",
                                                       str(MUSETALK_VENV_DIR)],
                  "create venv")

    mt_dir = MUSETALK_DIR
    if not (mt_dir / "scripts" / "inference.py").is_file():
        mt_dir.parent.mkdir(parents=True, exist_ok=True)
        git = shutil.which("git")
        if not git:
            raise AvatarError("git not found on PATH — required to clone "
                              "TMElyralab/MuseTalk")
        _run_step([git, "clone", "--depth", "1",
                  "https://github.com/TMElyralab/MuseTalk.git", str(mt_dir)],
                 "clone MuseTalk")

    req = mt_dir / "requirements.txt"
    _run_step([exe, "-m", "pip", "install", "--upgrade", "pip"], "upgrade pip")
    if req.is_file():
        _install_musetalk_requirements(exe, req)
    device = _install_musetalk_torch(exe)
    _install_mmcv_stack(exe)
    _fix_musetalk_numpy_pin(exe)
    _download_musetalk_weights(exe, mt_dir)
    print(f"Lip-sync venv ready: {exe}")
    print(f"MuseTalk cloned to: {mt_dir}")
    print(f"Model weights: {mt_dir / 'models'}")
    print(f"Device: {device}")
    if device != "cuda":
        print("WARNING: no usable CUDA device — MuseTalk lip-sync has no "
              "confirmed CPU inference path and will not work. Fix the GPU "
              "setup before relying on this feature.")
    print("Diagnostics:")
    _print_torch_diagnostics(exe)

    print("Running smoke test inference (MuseTalk's own example assets) "
          "to confirm the install actually works...")
    out_video = _smoke_test_musetalk(exe, mt_dir)
    print(f"Smoke test passed — output video: {out_video}")

    print("Next: set avatar.animation.lip_sync.enabled: true (also "
          "requires avatar.animation.enabled: true for the LivePortrait "
          "motion stage lip-sync builds on).")
