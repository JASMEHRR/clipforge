"""Chatterbox TTS worker — runs ONLY under the isolated .venv-tts interpreter
(avatar.tts.python). chatterbox-tts pins torch 2.6 and friends, which must
never enter the main venv (they conflict with the pinned mediapipe /
faster-whisper stack), so avatar.synthesize_batch shells out to this script.

Protocol (one batch per process, model loads once):
  stdin  — a single JSON object:
             {"ref_audio": str, "device": "auto"|"cpu",
              "jobs": [{"text": str, "out_path": str}, ...]}
  stdout — exactly one JSON line:
             {"ok": true, "results": [{"out_path": str, "duration_s": float}]}
           or {"ok": false, "error": str}
  stderr — human-readable progress/log lines only.
  exit   — 0 on ok, 1 otherwise.

No ClipForge imports: the isolated venv contains only chatterbox-tts and its
dependencies. Keep it that way.
"""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _pick_device(requested: str) -> str:
    if requested == "cpu":
        return "cpu"
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
        _log("CUDA not available — using CPU (expect minutes per clip)")
    except Exception as e:  # torch import/probe must never kill the batch
        _log(f"torch CUDA probe failed ({e}) — using CPU")
    return "cpu"


def main() -> int:
    try:
        req = json.load(sys.stdin)
        ref = str(req["ref_audio"])
        jobs = list(req["jobs"])
        if not Path(ref).is_file():
            raise FileNotFoundError(f"voice reference not found: {ref}")
        if not jobs:
            raise ValueError("no TTS jobs given")

        device = _pick_device(str(req.get("device", "auto")))
        _log(f"loading chatterbox on {device} ...")
        from chatterbox.tts import ChatterboxTTS
        import torchaudio
        try:
            model = ChatterboxTTS.from_pretrained(device=device)
        except Exception as e:
            if device != "cuda":
                raise
            # 4GB cards may not fit the model — CPU always works, just slower
            _log(f"CUDA load failed ({e}) — retrying on CPU")
            device = "cpu"
            model = ChatterboxTTS.from_pretrained(device="cpu")

        results = []
        for n, job in enumerate(jobs, 1):
            text, out_path = str(job["text"]), Path(str(job["out_path"]))
            _log(f"[{n}/{len(jobs)}] synthesizing {len(text.split())} words "
                 f"-> {out_path.name}")
            try:
                wav = model.generate(text, audio_prompt_path=ref)
            except Exception as e:
                if device != "cuda":
                    raise
                # typically CUDA OOM mid-batch: reload once on CPU and go on
                _log(f"CUDA generate failed ({e}) — reloading on CPU")
                device = "cpu"
                model = ChatterboxTTS.from_pretrained(device="cpu")
                wav = model.generate(text, audio_prompt_path=ref)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            torchaudio.save(str(out_path), wav, model.sr)
            duration_s = float(wav.shape[-1]) / float(model.sr)
            results.append({"out_path": str(out_path),
                            "duration_s": round(duration_s, 3)})
            _log(f"[{n}/{len(jobs)}] wrote {out_path} ({duration_s:.1f}s)")

        print(json.dumps({"ok": True, "results": results}), flush=True)
        return 0
    except Exception as e:  # protocol: every failure is one JSON error line
        _log(traceback.format_exc())
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}),
              flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
