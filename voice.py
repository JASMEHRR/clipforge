"""Local text-to-speech for Create mode (kokoro-onnx, fully offline).

kokoro-onnx is onnxruntime-based, so it runs in the main venv with no torch
pin to conflict with mediapipe/faster-whisper (unlike chatterbox, which needed
its own venv). The model + voices files (~340 MB) download once from the URLs
in config `create.voice.*` into `create.voice.model_dir` (under cache/, so
gitignored) and are reused after that.

Public API:
  ensure_models(cfg) -> (model_path, voices_path)
  synthesize_lines(lines, out_dir, cfg) -> list[{"path", "duration_s"}]
"""
from __future__ import annotations

import threading
import urllib.request
from pathlib import Path

from config import ROOT, load_config
from errors import ClipForgeError
from logutil import get_logger

log = get_logger("voice")

_USER_AGENT = "ClipForge/2.0 (+https://github.com/; kokoro model fetch)"
_MIN_MODEL_BYTES = 10 * 1024 * 1024     # anything smaller is a failed/partial download
_download_lock = threading.Lock()
_engine_lock = threading.Lock()
_engine_cache: dict[tuple[str, str], object] = {}


class VoiceError(ClipForgeError):
    stage = "voice"


def _vcfg(cfg: dict) -> dict:
    try:
        return cfg["create"]["voice"]
    except KeyError as e:
        raise VoiceError("config is missing create.voice") from e


def _download(url: str, dest: Path, timeout_s: int = 120) -> None:
    """Stream `url` to `dest` via a .part file; raise VoiceError on failure."""
    tmp = dest.with_suffix(dest.suffix + ".part")
    log.info("downloading %s -> %s", url, dest)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout_s) as resp, \
                open(tmp, "wb") as f:
            while chunk := resp.read(1024 * 1024):
                f.write(chunk)
    except (OSError, ValueError) as e:  # URLError/HTTPError/timeouts are OSError
        tmp.unlink(missing_ok=True)
        raise VoiceError(f"download failed: {url}", detail=str(e)[:300]) from e
    if tmp.stat().st_size < _MIN_MODEL_BYTES:
        size = tmp.stat().st_size
        tmp.unlink(missing_ok=True)
        raise VoiceError(f"download too small ({size} bytes): {url}")
    tmp.replace(dest)


def ensure_models(cfg: dict | None = None) -> tuple[Path, Path]:
    """Return (model_path, voices_path), downloading them on first use."""
    cfg = cfg or load_config()
    v = _vcfg(cfg)
    model_dir = ROOT / str(v.get("model_dir", "cache/models/kokoro"))
    model_path = model_dir / Path(str(v["model_url"])).name
    voices_path = model_dir / Path(str(v["voices_url"])).name
    with _download_lock:
        model_dir.mkdir(parents=True, exist_ok=True)
        for url, dest in ((v["model_url"], model_path),
                          (v["voices_url"], voices_path)):
            if not (dest.is_file() and dest.stat().st_size >= _MIN_MODEL_BYTES):
                _download(str(url), dest)
    return model_path, voices_path


def _engine(model_path: Path, voices_path: Path):
    """One Kokoro instance per model pair per process (loading takes seconds)."""
    key = (str(model_path), str(voices_path))
    with _engine_lock:
        if key not in _engine_cache:
            try:
                from kokoro_onnx import Kokoro
            except ImportError as e:
                raise VoiceError("kokoro-onnx is not installed",
                                 detail="pip install -r requirements.txt") from e
            try:
                _engine_cache[key] = Kokoro(str(model_path), str(voices_path))
            except Exception as e:  # noqa: BLE001 - onnxruntime raises varied types
                raise VoiceError("could not load the Kokoro model",
                                 detail=str(e)[:300]) from e
        return _engine_cache[key]


def synthesize_lines(lines: list[str], out_dir: str | Path,
                     cfg: dict | None = None) -> list[dict]:
    """Synthesize each line to `out_dir/line_NN.wav`.

    Returns [{"path": str, "duration_s": float}] in input order. Raises
    VoiceError on empty input, an unknown voice, or any synthesis/write
    failure (Create mode cannot produce a video without its narration)."""
    cfg = cfg or load_config()
    texts = [str(t).strip() for t in (lines or [])]
    if not texts or any(not t for t in texts):
        raise VoiceError("synthesize_lines needs a non-empty text for every line")
    v = _vcfg(cfg)
    voice = str(v.get("voice", "af_heart"))
    speed = float(v.get("speed", 1.0))
    lang = str(v.get("lang", "en-us"))
    if not 0.5 <= speed <= 2.0:
        raise VoiceError(f"create.voice.speed {speed} is outside 0.5-2.0")

    try:
        import soundfile as sf
    except ImportError as e:
        raise VoiceError("soundfile is not installed",
                         detail="pip install -r requirements.txt") from e

    engine = _engine(*ensure_models(cfg))
    known = getattr(engine, "get_voices", None)
    if callable(known):
        try:
            voices = set(known())
        except Exception:  # noqa: BLE001 - voice listing is advisory only
            voices = set()
        if voices and voice not in voices:
            raise VoiceError(f"unknown Kokoro voice '{voice}'",
                             detail="available: " + ", ".join(sorted(voices)))

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    log.info("tts: %d line(s) via kokoro (voice=%s, speed=%.2f)",
             len(texts), voice, speed)
    for i, text in enumerate(texts):
        out_path = out_dir / f"line_{i:02d}.wav"
        try:
            samples, sr = engine.create(text, voice=voice, speed=speed, lang=lang)
        except Exception as e:  # noqa: BLE001 - phonemizer/onnx errors vary
            raise VoiceError(f"kokoro synthesis failed on line {i}",
                             detail=str(e)[:300]) from e
        if sr <= 0 or len(samples) == 0:
            raise VoiceError(f"kokoro returned no audio for line {i}")
        try:
            sf.write(str(out_path), samples, sr)
        except (OSError, RuntimeError) as e:
            raise VoiceError(f"could not write {out_path.name}",
                             detail=str(e)[:300]) from e
        results.append({"path": str(out_path),
                        "duration_s": round(len(samples) / float(sr), 3)})
    return results
