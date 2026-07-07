"""Transcribe: faster-whisper with word-level timestamps.

Model matrix (config.yaml): GPU → large-v3/float16, CPU → small/int8.
Results cached under cache/transcripts keyed by audio hash + model + config
hash, so re-runs are instant."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

from config import ROOT, config_hash, file_hash, load_config
from errors import TranscribeError
from ffutil import run_ffmpeg
from logutil import get_logger
from schemas import validate

log = get_logger("transcribe")

_SENT_END = re.compile(r"[.!?…]['\")\]]*$")
MAX_SENTENCE_SECONDS = 30.0


def _register_cuda_dll_dirs() -> None:
    """ctranslate2's CUDA build needs cuBLAS/cuDNN on the DLL search path.
    On Windows there's no system CUDA Toolkit here, so pull them from the
    pip-installed nvidia-cublas-cu12 / nvidia-cudnn-cu12 wheels instead
    (see requirements.txt). No-op if those packages aren't installed
    (e.g. Linux/Docker, or CPU-only setups).

    cuDNN ships bundled inside ctranslate2's own package directory, so it
    resolves on its own. cuBLAS does not: ctranslate2 loads it lazily via a
    plain LoadLibraryA("cublas64_12.dll") call (src/cuda/cublas_stub.cc),
    which os.add_dll_directory() alone does not satisfy. ctranslate2 has a
    built-in escape hatch for exactly this — it honors CUDA_PATH and calls
    SetDllDirectoryA(CUDA_PATH + "\\bin") before that load — so point
    CUDA_PATH at the pip-installed cublas package instead of a real CUDA
    Toolkit install."""
    if sys.platform != "win32":
        return
    for pkg in ("nvidia.cublas", "nvidia.cudnn"):
        try:
            mod = importlib.import_module(pkg)
        except ImportError:
            continue
        roots = [Path(p) for p in getattr(mod, "__path__", [])]
        if not roots and mod.__file__:
            roots = [Path(mod.__file__).parent]
        for root in roots:
            for dll_dir in {p.parent for p in root.rglob("*.dll")}:
                os.add_dll_directory(str(dll_dir))
            if pkg == "nvidia.cublas" and not os.environ.get("CUDA_PATH"):
                os.environ["CUDA_PATH"] = str(root)


def gpu_available() -> bool:
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:  # noqa: BLE001
        return False


def model_config(cfg: dict) -> tuple[str, str, str]:
    """Returns (model_name, device, compute_type) per the model matrix."""
    if gpu_available():
        w = cfg["whisper"]["gpu"]
        return w["model"], "cuda", w["compute_type"]
    w = cfg["whisper"]["cpu"]
    return w["model"], "cpu", w["compute_type"]


def _spans_key(spans: list[tuple] | None) -> str:
    """Stable short hash of the span set for the transcript cache key."""
    if not spans:
        return "whole"
    blob = ";".join(f"{s:.3f}-{e:.3f}" for s, e in spans).encode("utf-8")
    return "spans_" + hashlib.sha256(blob).hexdigest()[:12]


def _build_model(cfg: dict):
    model_name, device, compute = model_config(cfg)
    if device == "cuda":
        _register_cuda_dll_dirs()
    from faster_whisper import WhisperModel
    model = WhisperModel(model_name, device=device, compute_type=compute,
                         cpu_threads=(os.cpu_count() or 4),
                         download_root=str(ROOT / cfg["whisper"]["model_dir"]))
    return model, model_name, device, compute


def _run_whisper(model, path: str | Path, cfg: dict):
    """Transcribe one audio file; return (words, info). Options match the
    performance/quality contract: VAD on, greedy (beam per config), no
    cross-segment conditioning (avoids hallucinated carry-over on clips)."""
    beam = int(cfg["whisper"].get("beam_size", 1))
    segments, info = model.transcribe(
        str(path), word_timestamps=True, vad_filter=True, beam_size=beam,
        condition_on_previous_text=False, language=cfg["whisper"].get("language"))
    words = []
    for seg in segments:  # generator — streams, memory-safe
        for w in seg.words or []:
            token = w.word.strip()
            if token:
                words.append({"word": token,
                              "start": float(w.start),
                              "end": float(w.end)})
    return words, info


def _transcribe_spans(model, audio_path: Path, spans: list[tuple], cfg: dict,
                      progress_cb=None) -> tuple[list[dict], list[dict], str]:
    """Transcribe only the shortlisted spans. Each span is trimmed from the wav
    with a fast keyframe seek, transcribed, and its word times offset back to
    absolute. Sentences are grouped per span so grouping never bridges a gap."""
    all_words: list[dict] = []
    all_sents: list[dict] = []
    language = ""
    total = max(1e-6, sum(e - s for s, e in spans))
    done = 0.0
    with tempfile.TemporaryDirectory(prefix="clipforge_spans_") as td:
        for i, (s, e) in enumerate(spans):
            clip = Path(td) / f"span_{i:03d}.wav"
            run_ffmpeg(["-ss", f"{s:.3f}", "-i", audio_path,
                        "-t", f"{e - s:.3f}", "-c", "copy", clip], timeout=600)
            words, info = _run_whisper(model, clip, cfg)
            language = language or (info.language or "")
            span_words = [{"word": w["word"],
                           "start": round(w["start"] + s, 3),
                           "end": round(w["end"] + s, 3)} for w in words]
            all_words.extend(span_words)
            all_sents.extend(build_sentences(span_words))
            done += (e - s)
            if progress_cb:
                progress_cb(min(1.0, done / total))
    all_words.sort(key=lambda w: w["start"])
    all_sents.sort(key=lambda s: s["start"])
    return all_words, all_sents, (language or "en")


def transcribe(audio_path: str | Path, cfg: dict | None = None,
               spans: list[tuple] | None = None,
               debug_dir: str | Path | None = None,
               progress_cb=None) -> dict:
    """Returns Transcript dict (schema-validated). `progress_cb(frac)` gets
    live 0..1 progress. When `spans` is given, only those (start, end) regions
    are transcribed (segment-first); otherwise the whole audio is transcribed."""
    cfg = cfg or load_config()
    audio_path = Path(audio_path)
    if not audio_path.exists():
        raise TranscribeError(f"audio not found: {audio_path}")

    model_name, device, _ = model_config(cfg)
    cache_dir = ROOT / cfg["paths"]["cache_dir"] / "transcripts"
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = (f"{file_hash(audio_path)[:24]}_{model_name}_"
           f"{config_hash(cfg, 'whisper')}_{_spans_key(spans)}")
    cache_file = cache_dir / f"{key}.json"
    if cache_file.exists():
        log.info("transcript cache hit: %s", cache_file.name)
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        validate(data, "transcript")
        _write_debug(data, debug_dir)
        return data

    beam = int(cfg["whisper"].get("beam_size", 1))
    log.info("whisper %s on %s (beam=%d, %s)", model_name, device, beam,
             f"{len(spans)} spans" if spans else "whole video")
    try:
        model, _, _, _ = _build_model(cfg)
        if spans:
            words, sentences, language = _transcribe_spans(
                model, audio_path, spans, cfg, progress_cb)
            duration = sum(e - s for s, e in spans)
        else:
            raw, info = _run_whisper(model, audio_path, cfg)
            total = float(info.duration or 0.0) or 1.0
            words = []
            for w in raw:
                words.append({"word": w["word"],
                              "start": round(w["start"], 3),
                              "end": round(w["end"], 3)})
                if progress_cb:
                    progress_cb(min(1.0, w["end"] / total))
            sentences = build_sentences(words)
            language = info.language or "en"
            duration = float(info.duration or 0.0)
    except TranscribeError:
        raise
    except Exception as e:  # noqa: BLE001
        raise TranscribeError("faster-whisper failed", detail=str(e)[:500]) from e

    data = {
        "text": " ".join(w["word"] for w in words),
        "language": language,
        "duration": duration,
        "words": words,
        "sentences": sentences,
    }
    validate(data, "transcript")
    cache_file.write_text(json.dumps(data), encoding="utf-8")
    log.info("transcribed: %d words, %d sentences, lang=%s",
             len(words), len(sentences), language)
    _write_debug(data, debug_dir)
    return data


def _write_debug(data: dict, debug_dir) -> None:
    if debug_dir:
        Path(debug_dir).mkdir(parents=True, exist_ok=True)
        (Path(debug_dir) / "transcript.json").write_text(
            json.dumps(data, indent=2), encoding="utf-8")


def build_sentences(words: list[dict]) -> list[dict]:
    """Group word stream into sentences on terminal punctuation; force a break
    when a sentence exceeds MAX_SENTENCE_SECONDS (run-on speech)."""
    sentences, current = [], []
    for w in words:
        current.append(w)
        too_long = w["end"] - current[0]["start"] > MAX_SENTENCE_SECONDS
        if _SENT_END.search(w["word"]) or too_long:
            sentences.append(_finish(current))
            current = []
    if current:
        sentences.append(_finish(current))
    return sentences


def _finish(ws: list[dict]) -> dict:
    return {"text": " ".join(w["word"] for w in ws),
            "start": ws[0]["start"], "end": ws[-1]["end"], "words": ws}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="smoke: transcribe a wav")
    ap.add_argument("audio")
    a = ap.parse_args()
    t = transcribe(a.audio)
    print(json.dumps({"language": t["language"], "words": len(t["words"]),
                      "sentences": len(t["sentences"]),
                      "first": t["sentences"][0] if t["sentences"] else None},
                     indent=2))
