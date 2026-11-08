"""Illustration worker for Create mode. Runs ONLY inside .venv-art (torch +
diffusers; their pins conflict with the main venv's mediapipe/faster-whisper).
art.py is the only caller - it speaks a JSON seam:

  stdin   one request {"model", "cache_dir", "steps", "guidance",
                       "items": [{"prompt", "seed", "width", "height", "out"}]}
  stdout  one JSON line per item {"i", "ok", "s", "error"?}, then {"done": true}

Library chatter goes to stderr so stdout stays machine-readable. An item that
fails is reported and skipped; the worker only dies on a load failure.

GTX 16xx note: the fp16 VAE overflows to NaN (= a black picture), the fp16
UNet does not - so the UNet runs fp16 (fits 4 GB, ~3 s/step) and only the VAE
decode runs fp32. guidance <= 1 disables classifier-free guidance, which halves
the work; LCM models are trained for that range.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

_OUT = sys.stdout
sys.stdout = sys.stderr          # anything a library prints must not corrupt the seam


def emit(obj: dict) -> None:
    _OUT.write(json.dumps(obj) + "\n")
    _OUT.flush()


def load(model: str, cache_dir: str):
    import torch
    from diffusers import DiffusionPipeline, LCMScheduler
    kw = dict(torch_dtype=torch.float16, cache_dir=cache_dir, safety_checker=None,
              requires_safety_checker=False)
    try:                                     # offline first: no Hub round-trip per run
        pipe = DiffusionPipeline.from_pretrained(model, local_files_only=True, **kw)
    except Exception:  # noqa: BLE001 - not cached yet -> one-time download
        pipe = DiffusionPipeline.from_pretrained(model, **kw)
    pipe.scheduler = LCMScheduler.from_config(pipe.scheduler.config)
    pipe.vae.to(torch.float32)
    pipe.to("cuda")
    pipe.set_progress_bar_config(disable=True)
    return pipe


def draw(pipe, item: dict, steps: int, guidance: float) -> None:
    import torch
    from PIL import Image
    lat = pipe(item["prompt"], num_inference_steps=steps, guidance_scale=guidance,
               width=int(item["width"]), height=int(item["height"]),
               generator=torch.Generator("cpu").manual_seed(int(item["seed"])),
               output_type="latent").images
    if torch.isnan(lat).any():
        raise RuntimeError("the model produced NaN latents")
    with torch.no_grad():
        img = pipe.vae.decode(lat.float() / pipe.vae.config.scaling_factor).sample
    arr = ((img[0].clamp(-1, 1) + 1) * 127.5).permute(1, 2, 0).round().byte().cpu().numpy()
    out = Path(item["out"])
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".part.png")
    Image.fromarray(arr).save(tmp)
    tmp.replace(out)


def main() -> int:
    sys.stdin.reconfigure(encoding="utf-8")
    req = json.loads(sys.stdin.read())
    pipe = load(req["model"], req["cache_dir"])
    for i, item in enumerate(req["items"]):
        t = time.perf_counter()
        try:
            draw(pipe, item, int(req["steps"]), float(req["guidance"]))
            emit({"i": i, "ok": True, "s": round(time.perf_counter() - t, 1)})
        except Exception as e:  # noqa: BLE001 - report the item, keep drawing the rest
            emit({"i": i, "ok": False, "s": round(time.perf_counter() - t, 1),
                  "error": f"{type(e).__name__}: {e}"})
    emit({"done": True})
    return 0


if __name__ == "__main__":
    sys.exit(main())
