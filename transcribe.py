"""Transcribe: faster-whisper with word-level timestamps.

Model matrix (config.yaml): GPU → large-v3/float16, CPU → small/int8.
Results cached under cache/transcripts keyed by audio hash + model + config
hash, so re-runs are instant."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from config import ROOT, config_hash, file_hash, load_config
from errors import TranscribeError
from logutil import get_logger
from schemas import validate

log = get_logger("transcribe")

_SENT_END = re.compile(r"[.!?…]['\")\]]*$")
MAX_SENTENCE_SECONDS = 30.0


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


def transcribe(audio_path: str | Path, cfg: dict | None = None,
               debug_dir: str | Path | None = None) -> dict:
    """Returns Transcript dict (schema-validated)."""
    cfg = cfg or load_config()
    audio_path = Path(audio_path)
    if not audio_path.exists():
        raise TranscribeError(f"audio not found: {audio_path}")

    model_name, device, compute = model_config(cfg)
    cache_dir = ROOT / cfg["paths"]["cache_dir"] / "transcripts"
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = f"{file_hash(audio_path)[:24]}_{model_name}_{config_hash(cfg, 'whisper')}"
    cache_file = cache_dir / f"{key}.json"
    if cache_file.exists():
        log.info("transcript cache hit: %s", cache_file.name)
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        validate(data, "transcript")
        return data

    log.info("whisper %s on %s (%s)", model_name, device, compute)
    try:
        from faster_whisper import WhisperModel
        model = WhisperModel(model_name, device=device, compute_type=compute,
                             download_root=str(ROOT / cfg["whisper"]["model_dir"]))
        segments, info = model.transcribe(
            str(audio_path), word_timestamps=True, vad_filter=True,
            language=cfg["whisper"].get("language"))
        words = []
        for seg in segments:  # generator — streams, memory-safe
            for w in seg.words or []:
                token = w.word.strip()
                if token:
                    words.append({"word": token,
                                  "start": round(float(w.start), 3),
                                  "end": round(float(w.end), 3)})
        language = info.language or "en"
        duration = float(info.duration or 0.0)
    except TranscribeError:
        raise
    except Exception as e:  # noqa: BLE001
        raise TranscribeError("faster-whisper failed", detail=str(e)[:500]) from e

    sentences = build_sentences(words)
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
    if debug_dir:
        Path(debug_dir).mkdir(parents=True, exist_ok=True)
        (Path(debug_dir) / "transcript.json").write_text(
            json.dumps(data, indent=2), encoding="utf-8")
    return data


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
