"""AI illustrations for Create mode (create.visual_style: illustrated).

  plan_beats   narrated lines -> picture beats (~create.art.beat_s seconds each,
               never crossing a section, so every chapter Short opens on a
               fresh picture)
  art_prompt   a line's `visual` + the chapter scene + the house style
  draw_images  renders the pictures with a local Stable Diffusion model
               (DreamShaper 8 LCM) through art_worker.py inside .venv-art;
               results are cached by prompt+seed+settings in cache/art/images
  cutout       splits a picture into a cut-out character (isnet-general-use
               matte, Apache-2.0, ONNX on CPU) and the background with that
               character painted out, so animate.py can move them separately

Everything runs on this machine; nothing is uploaded and no paid API is used.

CLI:
  .venv/Scripts/python.exe art.py setup          # create .venv-art + install
  .venv/Scripts/python.exe art.py draw "a smiling octopus on a coral reef"
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import numpy as np

from config import ROOT, load_config
from errors import ClipForgeError
from logutil import get_logger

log = get_logger("art")

# where each chapter scene (schemas.KIDS_SCENES) takes place, as prompt words
SETTINGS = {
    "space": "in outer space with colorful planets and twinkling stars",
    "ocean": "underwater in the bright blue ocean",
    "sky": "in a bright blue sky with fluffy clouds",
    "meadow": "in a sunny green meadow with flowers",
    "snow": "in a snowy icy landscape",
    "desert": "in a warm sunny desert",
    "body": "inside the human body, colorful friendly science cartoon",
}
_ISNET_SHA256 = "60920e99c45464f2ba57bee2ad08c919a52bbf852739e96947fbb4358c0d964a"


class ArtError(ClipForgeError):
    stage = "art"


def _acfg(cfg: dict) -> dict:
    a = (cfg.get("create") or {}).get("art")
    if not isinstance(a, dict):
        raise ArtError("config is missing the create.art: block", retryable=False)
    return a


def _cache(cfg: dict, *parts: str) -> Path:
    return ROOT / cfg["paths"]["cache_dir"] / Path(*parts)


# ====================================================================== planning

def plan_beats(lines: list[dict], beat_s: float) -> list[dict]:
    """Pure. Group consecutive timeline lines ({section, visual, start, end})
    into picture beats: a beat grows until it covers beat_s seconds, and a new
    section always starts a new beat. Returns [{first, last, visual, section}]."""
    if beat_s <= 0:
        raise ArtError("create.art.beat_s must be positive", retryable=False)
    beats: list[dict] = []
    for i, ln in enumerate(lines):
        b = beats[-1] if beats else None
        if (b is None or b["section"] != ln["section"]
                or lines[b["last"]]["end"] - lines[b["first"]]["start"] >= beat_s):
            beats.append({"first": i, "last": i, "visual": ln["visual"],
                          "section": ln["section"]})
        else:
            b["last"] = i
    return beats


def art_prompt(visual: str, scene: str, style: str) -> str:
    """Pure. The picture prompt for one beat."""
    parts = [visual.strip().rstrip(".,"), SETTINGS.get(scene, ""), style.strip()]
    return ", ".join(p for p in parts if p)


def image_key(prompt: str, seed: int, acfg: dict) -> str:
    """Cache key: everything that changes the pixels."""
    blob = json.dumps([acfg.get("model"), int(acfg.get("steps", 6)),
                       float(acfg.get("guidance", 1.0)), int(acfg.get("width", 768)),
                       int(acfg.get("height", 432)), prompt, int(seed)])
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:20]


# ====================================================================== drawing

def _worker_python(acfg: dict) -> Path:
    py = ROOT / str(acfg.get("python", ".venv-art/Scripts/python.exe"))
    if not py.exists():
        raise ArtError(f"illustration environment not found ({py})",
                       detail="set it up once with: .venv/Scripts/python.exe art.py setup",
                       retryable=False)
    return py


def draw_images(items: list[tuple[str, int]], cfg: dict,
                size: tuple[int, int] | None = None) -> list[Path]:
    """Render (prompt, seed) pairs; returns one PNG path per item, in order.
    `size` (w, h) overrides create.art width/height (explainer objects are
    square). Cached pictures are reused; the rest go to one art_worker.py run
    (the model loads once). Raises ArtError if any picture is still missing."""
    acfg = _acfg(cfg)
    w, h = size or (int(acfg.get("width", 768)), int(acfg.get("height", 432)))
    acfg = {**acfg, "width": w, "height": h}          # the cache key sees the real size
    if w % 8 or h % 8:
        raise ArtError("create.art width/height must be multiples of 8", retryable=False)
    img_dir = _cache(cfg, "art", "images")
    paths = [img_dir / f"{image_key(p, s, acfg)}.png" for p, s in items]
    todo = {}                                     # path -> request item (dedupes repeats)
    for (prompt, seed), path in zip(items, paths):
        if not path.exists() and path not in todo:
            todo[path] = {"prompt": prompt, "seed": int(seed), "width": w,
                          "height": h, "out": str(path)}
    if todo:
        _run_worker(list(todo.values()), acfg, cfg)
    missing = [p for p in paths if not p.exists()]
    if missing:
        raise ArtError(f"{len(set(missing))} of {len(set(paths))} pictures failed to draw",
                       detail=f"see {_cache(cfg, 'art', 'worker.log')}")
    return paths


def _run_worker(reqs: list[dict], acfg: dict, cfg: dict) -> None:
    py = _worker_python(acfg)
    req = {"model": str(acfg.get("model", "Lykon/dreamshaper-8-lcm")),
           "cache_dir": str(_cache(cfg, "models", "art")),
           "steps": int(acfg.get("steps", 6)), "guidance": float(acfg.get("guidance", 1.0)),
           "items": reqs}
    log_path = _cache(cfg, "art", "worker.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # generous: first run downloads ~4 GB; afterwards ~20 s per picture on a GTX 1650
    limit_s = 1800 + 120 * len(reqs)
    log.info("drawing %d picture(s) with %s", len(reqs), req["model"])
    t0 = time.monotonic()
    with open(log_path, "w", encoding="utf-8") as err:
        proc = subprocess.Popen([str(py), "-I", str(ROOT / "art_worker.py")],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=err, text=True, encoding="utf-8",
                                env=_worker_env())
        watchdog = threading.Timer(limit_s, proc.kill)     # a hung GPU never hangs the night
        watchdog.start()
        try:
            proc.stdin.write(json.dumps(req))
            proc.stdin.close()
            done = 0
            for raw in proc.stdout:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if msg.get("done"):
                    break
                done += 1
                if not msg.get("ok"):
                    log.warning("picture %d failed: %s", msg.get("i"), msg.get("error"))
                eta = (time.monotonic() - t0) / done * (len(reqs) - done)
                log.info("picture %d/%d (%.1fs, ~%.0f min left)", done, len(reqs),
                         msg.get("s", 0), eta / 60)
            proc.wait(timeout=60)
        finally:
            watchdog.cancel()
            if proc.poll() is None:
                proc.kill()
    if time.monotonic() - t0 >= limit_s:
        raise ArtError(f"illustration worker timed out after {limit_s}s", detail=str(log_path))
    if proc.returncode not in (0, None):
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-1500:]
        raise ArtError(f"illustration worker exited with code {proc.returncode}",
                       detail=tail)


def _worker_env() -> dict:
    import os
    env = dict(os.environ)
    env.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


# ====================================================================== cut-out

_SESSION = None


def _matte_session(cfg: dict):
    """isnet-general-use ONNX (Apache-2.0, from the rembg releases), downloaded
    once and checked against a pinned SHA-256."""
    global _SESSION
    if _SESSION is not None:
        return _SESSION
    import onnxruntime as ort
    path = _cache(cfg, "models", "cutout", "isnet-general-use.onnx")
    if not path.exists():
        url = str(_acfg(cfg).get("cutout_url"))
        log.info("downloading the cut-out model (~170 MB) from %s", url)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        try:
            urllib.request.urlretrieve(url, tmp)
        except OSError as e:
            raise ArtError("could not download the cut-out model", detail=str(e)) from e
        digest = hashlib.sha256(tmp.read_bytes()).hexdigest()
        if digest != _ISNET_SHA256:
            tmp.unlink(missing_ok=True)
            raise ArtError("cut-out model checksum mismatch", detail=digest, retryable=False)
        tmp.replace(path)
    _SESSION = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
    return _SESSION


def matte(rgb: np.ndarray, cfg: dict) -> np.ndarray:
    """Foreground alpha (float32 HxW, 0..1) for an RGB uint8 picture."""
    import cv2
    sess = _matte_session(cfg)
    h, w = rgb.shape[:2]
    x = cv2.resize(rgb, (1024, 1024), interpolation=cv2.INTER_AREA).astype(np.float32)
    x = x / max(float(x.max()), 1e-6) - 0.5
    out = sess.run(None, {sess.get_inputs()[0].name:
                          x.transpose(2, 0, 1)[None].astype(np.float32)})[0][0, 0]
    out = (out - out.min()) / max(float(out.max() - out.min()), 1e-6)
    return cv2.resize(out, (w, h), interpolation=cv2.INTER_LINEAR).astype(np.float32)


def split_layers(rgb: np.ndarray, alpha: np.ndarray, min_area: float = 0.02,
                 max_area: float = 0.55):
    """(fg_rgba | None, box | None, bg_rgb). fg is the main subject cropped to
    its box (x, y, w, h) with straight alpha; bg has the subject painted out.
    A matte that is too small or too big (no clear subject, or the 'subject'
    is the whole scene) yields fg None and the untouched picture."""
    import cv2
    h, w = rgb.shape[:2]
    binary = (alpha > 0.5).astype(np.uint8)
    frac = float(binary.mean())
    if not min_area <= frac <= max_area:
        return None, None, rgb
    n, labels, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
    areas = stats[1:, cv2.CC_STAT_AREA]
    keep_ids = 1 + np.flatnonzero(areas >= 0.15 * areas.max())   # drop specks
    keep = np.isin(labels, keep_ids).astype(np.uint8)
    soft = cv2.dilate(keep, np.ones((5, 5), np.uint8))
    a = np.clip(alpha * soft * 255.0, 0, 255).astype(np.uint8)
    ys, xs = np.nonzero(keep)
    pad = 6
    x0, y0 = max(0, xs.min() - pad), max(0, ys.min() - pad)
    x1, y1 = min(w, xs.max() + pad + 1), min(h, ys.max() + pad + 1)
    fg = np.dstack([rgb[y0:y1, x0:x1], a[y0:y1, x0:x1]])
    # paint the subject out (at half size: Telea is slow on big holes) so the
    # moving character never reveals a ghost of itself underneath
    k = max(9, int(0.035 * w)) | 1
    hole = cv2.dilate(keep, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    small = cv2.resize(rgb, (w // 2, h // 2), interpolation=cv2.INTER_AREA)
    small_hole = cv2.resize(hole, (w // 2, h // 2), interpolation=cv2.INTER_NEAREST)
    filled = cv2.resize(cv2.inpaint(small, small_hole, 9, cv2.INPAINT_TELEA), (w, h),
                        interpolation=cv2.INTER_CUBIC)
    bg = np.where(hole[..., None] > 0, filled, rgb)
    return fg, (int(x0), int(y0), int(x1 - x0), int(y1 - y0)), bg


def cutout(img_path: Path, cfg: dict, max_area: float = 0.55) -> dict:
    """{"bg": png, "fg": png | None, "box": [x, y, w, h] | None} for a drawn
    picture, cached next to the pictures (cache/art/layers)."""
    import cv2
    d = _cache(cfg, "art", "layers")
    meta_p = d / f"{img_path.stem}.json"
    if meta_p.exists():
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
        if Path(meta["bg"]).exists() and (meta["fg"] is None or Path(meta["fg"]).exists()):
            return meta
    bgr = cv2.imread(str(img_path), cv2.IMREAD_COLOR)
    if bgr is None:
        raise ArtError(f"unreadable picture {img_path}")
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    fg, box, bg = split_layers(rgb, matte(rgb, cfg), max_area=max_area)
    d.mkdir(parents=True, exist_ok=True)
    bg_p = d / f"{img_path.stem}_bg.png"
    cv2.imwrite(str(bg_p), cv2.cvtColor(bg, cv2.COLOR_RGB2BGR))
    fg_p = None
    if fg is not None:
        fg_p = d / f"{img_path.stem}_fg.png"
        cv2.imwrite(str(fg_p), cv2.cvtColor(fg, cv2.COLOR_RGBA2BGRA))
    meta = {"bg": str(bg_p), "fg": str(fg_p) if fg_p else None,
            "box": list(box) if box else None}
    meta_p.write_text(json.dumps(meta), encoding="utf-8")
    return meta


# ====================================================================== CLI

def setup() -> int:
    """Create .venv-art next to the main venv and install requirements-art.txt."""
    venv = ROOT / ".venv-art"
    py = venv / "Scripts" / "python.exe"
    if not py.exists():
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    subprocess.run([str(py), "-m", "pip", "install", "--upgrade", "pip"], check=True)
    subprocess.run([str(py), "-m", "pip", "install", "-r",
                    str(ROOT / "requirements-art.txt")], check=True)
    subprocess.run([str(py), "-c", "import torch; print('CUDA available:', "
                    "torch.cuda.is_available())"], check=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Create mode illustrations")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup", help="create .venv-art and install the drawing model's libraries")
    dr = sub.add_parser("draw", help="draw one test picture and cut it out")
    dr.add_argument("visual")
    dr.add_argument("--scene", default="meadow", choices=sorted(SETTINGS))
    dr.add_argument("--seed", type=int, default=1)
    args = ap.parse_args(argv)
    if args.cmd == "setup":
        return setup()
    cfg = load_config()
    prompt = art_prompt(args.visual, args.scene, str(_acfg(cfg).get("style", "")))
    path = draw_images([(prompt, args.seed)], cfg)[0]
    print(json.dumps({"image": str(path), **cutout(path, cfg)}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
