"""Real-pipeline caption/font previews.

Renders ONE still frame through the EXACT ASS + FFmpeg subtitle burn that
captions.py uses in production (same write_ass, same subtitles filter, same
fontsdir), so a font/preset preview is pixel-faithful to a finished clip — not
a browser CSS approximation. Shared by the caption-preset picker and the font
gallery. Output is a small cached PNG per (preset, font, text).

The frame is rendered at true caption resolution (1080x1920) so font size,
outline, shadow and the per-word highlight colour are exactly as burned, then
the caption band is cropped and scaled down to a gallery-sized strip."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import fontreg
from captions import write_ass
from config import ROOT, load_config
from ffutil import filter_path, run_ffmpeg
from logutil import get_logger

log = get_logger("style_preview")

CACHE_DIR = ROOT / "cache" / "font_previews"
SAMPLE_TEXT = "Your Caption Here"
_VERSION = "1"                          # bump to invalidate all cached previews
_FRAME_W, _FRAME_H = 1080, 1920
_BAND_H = 360                           # caption band cropped out of the frame
_OUT_W = 760                            # gallery strip width
_ANCHOR = 0.52                          # CAPTION POSITION LAW low bound → mid frame
_HL_TIME = 0.75                         # extract when the middle word is highlighted


def _sample_words() -> list[dict]:
    """Clip-relative words for SAMPLE_TEXT, timed so the middle word is the
    active (highlighted) one at _HL_TIME."""
    toks = SAMPLE_TEXT.split()
    step = 0.5
    return [{"word": t, "start": round(i * step, 3), "end": round((i + 1) * step, 3)}
            for i, t in enumerate(toks)]


def _cache_key(preset: dict, font_family: str, font_file: Path | None) -> str:
    stamp = ""
    if font_file and font_file.exists():
        st = font_file.stat()
        stamp = f"{st.st_size}:{int(st.st_mtime)}"
    blob = json.dumps({"p": preset, "f": font_family, "s": stamp,
                       "t": SAMPLE_TEXT, "v": _VERSION}, sort_keys=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:20]


def preview_png(preset_name: str, font_family: str | None = None,
                cfg: dict | None = None) -> Path:
    """Return a cached PNG of SAMPLE_TEXT burned with `preset_name`, optionally
    overriding the preset's font to `font_family`. Regenerated only when the
    preset, font file, sample text or renderer version changes.

    Raises KeyError for an unknown preset and CaptionError on a burn failure
    (propagated from write_ass / ffmpeg)."""
    cfg = cfg or load_config()
    if preset_name not in cfg["captions"]["presets"]:
        raise KeyError(f"unknown caption preset '{preset_name}'")

    # deep copy so overriding the font never touches the shared singleton
    cfg = copy.deepcopy(cfg)
    preset = cfg["captions"]["presets"][preset_name]
    if font_family:
        preset["font"] = font_family

    font_file = fontreg.font_path_for_family(cfg, font_family) if font_family else None
    key = _cache_key(preset, font_family or preset["font"], font_file)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out = CACHE_DIR / f"{key}.png"
    if out.exists():
        return out

    ass_path = CACHE_DIR / f"{key}.ass"
    write_ass(_sample_words(), ass_path, cfg, preset_name,
              play_w=_FRAME_W, play_h=_FRAME_H, anchor=_ANCHOR, clip_duration=2.0)
    fontsdir = fontreg.fonts_dir(cfg)
    band_y = max(0, int(_ANCHOR * _FRAME_H - _BAND_H / 2))
    vf = (f"subtitles=filename='{filter_path(ass_path)}'"
          f":fontsdir='{filter_path(fontsdir)}',"
          f"crop={_FRAME_W}:{_BAND_H}:0:{band_y},"
          f"scale={_OUT_W}:-1")
    run_ffmpeg(["-f", "lavfi", "-i",
                f"color=c=0x0b0b0f:s={_FRAME_W}x{_FRAME_H}:d=2",
                "-ss", str(_HL_TIME), "-vf", vf, "-frames:v", "1", out])
    ass_path.unlink(missing_ok=True)
    return out


if __name__ == "__main__":  # smoke: render each bundled preset once
    c = load_config()
    for name in c["captions"]["presets"]:
        p = preview_png(name, cfg=c)
        assert p.exists() and p.stat().st_size > 0, name
        print("ok", name, p)
