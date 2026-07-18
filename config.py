"""Config loading: config.yaml + .env (optional). Also the Python-version
startup check and config hashing for cache keys."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
from pathlib import Path

import yaml

from errors import ConfigError

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"
# User overrides saved from the Settings tab live here so config.yaml (and its
# comments) stays pristine. This file is deep-merged over config.yaml on load.
LOCAL_PATH = ROOT / "config.local.yaml"

_cached: dict | None = None


def _deep_merge(base: dict, over: dict) -> dict:
    """Recursively merge `over` into `base` (returns `base`, mutated)."""
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def check_python_version(cfg: dict) -> None:
    required = cfg.get("python", {}).get("required", "3.11")
    major, minor = (int(x) for x in required.split(".")[:2])
    if sys.version_info[:2] != (major, minor):
        if cfg.get("python", {}).get("allow_unsupported"):
            return
        raise ConfigError(
            f"Python {required}.x is required (you are on "
            f"{sys.version_info.major}.{sys.version_info.minor}). "
            "MediaPipe wheels lag newer Python versions. Install Python "
            f"{required} and recreate the venv, or set python.allow_unsupported: "
            "true in config.yaml at your own risk."
        )


def load_config(path: Path | None = None, check_python: bool = True) -> dict:
    """Load config.yaml, overlay .env into os.environ (never into the dict)."""
    global _cached
    if _cached is not None and path is None:
        return _cached
    p = path or CONFIG_PATH
    if not p.exists():
        raise ConfigError(f"config file not found: {p}")
    with open(p, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if path is None and LOCAL_PATH.exists():  # Settings-tab overrides
        try:
            local = yaml.safe_load(LOCAL_PATH.read_text(encoding="utf-8")) or {}
            _deep_merge(cfg, local)
        except (yaml.YAMLError, OSError) as e:
            raise ConfigError(f"could not read {LOCAL_PATH.name}: {e}") from e
    if check_python:
        check_python_version(cfg)
    _load_dotenv(ROOT / ".env")
    if path is None:
        _cached = cfg
    return cfg


def save_config(updates: dict) -> dict:
    """Persist `updates` (nested dict) to config.local.yaml, deep-merged over any
    existing overrides, and refresh the in-process config singleton. Returns the
    updated config. config.yaml itself is never modified."""
    global _cached
    existing: dict = {}
    if LOCAL_PATH.exists():
        existing = yaml.safe_load(LOCAL_PATH.read_text(encoding="utf-8")) or {}
    merged = _deep_merge(existing, updates)
    with open(LOCAL_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(merged, f, sort_keys=False, default_flow_style=False)
    _cached = None                 # force reload with the new overrides
    return load_config()


def reload_config() -> dict:
    """Drop the cached singleton and reload. Use after editing config.local.yaml
    outside save_config (e.g. removing a key, which a deep-merge can't do)."""
    global _cached
    _cached = None
    return load_config()


def _load_dotenv(env_path: Path) -> None:
    """Minimal .env loader (no dependency needed at import time). Existing
    environment variables win; the build never requires .env to exist."""
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


def hex_to_ass(hex_color: str) -> str:
    """Color → ASS '&H00BBGGRR'. Accepts '#RRGGBB', 'RRGGBB', 'rgb(r,g,b)' /
    'rgba(r,g,b,a)' (as Gradio's ColorPicker emits), and passes through values
    already in ASS form. Invalid input raises ConfigError."""
    s = (hex_color or "").strip()
    if s.upper().startswith("&H"):
        return s
    if s.lower().startswith(("rgb(", "rgba(")):
        nums = s[s.index("(") + 1:s.index(")")].split(",")
        try:
            r, g, b = (int(round(float(nums[i]))) for i in range(3))
        except (ValueError, IndexError) as e:
            raise ConfigError(f"invalid rgb color: {hex_color!r}") from e
        return f"&H00{b:02X}{g:02X}{r:02X}"
    s = s.lstrip("#")
    if len(s) != 6 or any(c not in "0123456789abcdefABCDEF" for c in s):
        raise ConfigError(f"invalid hex color: {hex_color!r}")
    rr, gg, bb = s[0:2], s[2:4], s[4:6]
    return f"&H00{bb}{gg}{rr}".upper()


def apply_run_options(cfg: dict, opts: dict) -> dict:
    """Return a DEEP COPY of cfg with per-run UI options applied. Pure (never
    mutates the shared singleton), so it also feeds re-render safely. Every
    option is optional; absent/blank keys leave cfg untouched, so an empty opts
    yields today's behaviour exactly.

    Recognised keys: cta_text, highlight_hex, preset, pacing (0..1 aggressiveness),
    clip_min, clip_max, watermark_mode (off|text|image), watermark_text,
    watermark_image (logo path), watermark_position, font_family (per-preset
    caption font override).

    Editing-preset keys (presets.expand): caption_primary_hex,
    caption_font_size, caption_anchor, caption_animation (karaoke|fade|box),
    sfx_enabled, sfx_pack, sfx_volume_db, speed_ramps, punch_in, popins,
    transition, intro, outro — the last six land under style.* as render-time
    config for cut/reframe/captions.
    """
    c = copy.deepcopy(cfg)
    o = opts or {}

    cta_text = (o.get("cta_text") or "").strip()
    if cta_text:
        c.setdefault("style", {}).setdefault("cta", {})
        c["style"]["cta"]["enabled"] = True
        c["style"]["cta"]["text"] = cta_text

    hi = (o.get("highlight_hex") or "").strip()
    preset = o.get("preset") or c.get("captions", {}).get("preset")
    if hi and preset and preset in c.get("captions", {}).get("presets", {}):
        c["captions"]["presets"][preset]["highlight_color"] = hex_to_ass(hi)

    font = (o.get("font_family") or "").strip()
    if font and preset and preset in c.get("captions", {}).get("presets", {}):
        c["captions"]["presets"][preset]["font"] = font

    pacing = o.get("pacing")
    if pacing is not None and pacing != "":
        agg = max(0.0, min(1.0, float(pacing)))
        st = c.setdefault("style", {})
        # gentle (0) → keep long pauses; aggressive (1) → tight cuts. Bounds
        # stay inside the safe range the refiner already clamps to.
        st["max_pause_s"] = round(0.9 - 0.55 * agg, 3)      # 0.90 → 0.35
        st["target_pause_s"] = round(0.5 - 0.25 * agg, 3)   # 0.50 → 0.25

    cmin, cmax = o.get("clip_min"), o.get("clip_max")
    if cmin:
        c.setdefault("clips", {})["min_seconds"] = int(cmin)
    if cmax:
        c.setdefault("clips", {})["max_seconds"] = int(cmax)
    if cmin and cmax and int(cmin) > int(cmax):  # guard: keep a valid range
        c["clips"]["min_seconds"], c["clips"]["max_seconds"] = int(cmax), int(cmin)

    wm_mode = (o.get("watermark_mode") or "").strip().lower()
    wm_text = (o.get("watermark_text") or "").strip()
    wm_image = (o.get("watermark_image") or "").strip()
    if wm_mode or wm_text or wm_image:
        wm = c.setdefault("captions", {}).setdefault("watermark", {})
        # explicit mode wins; otherwise infer from which field was provided
        mode = wm_mode or ("image" if wm_image else "text" if wm_text else "off")
        wm["mode"] = mode
        wm["enabled"] = (mode == "text")          # keep legacy flag consistent
        if wm_text:
            wm["text"] = wm_text
        if wm_image:
            wm["image_path"] = wm_image
        if o.get("watermark_position"):
            wm["position"] = o["watermark_position"]

    # --- editing-preset keys (presets.expand) ---
    presets = c.get("captions", {}).get("presets", {})
    if preset and preset in presets:
        cap = presets[preset]
        if o.get("caption_primary_hex"):
            cap["primary_color"] = hex_to_ass(o["caption_primary_hex"])
        if o.get("caption_font_size"):
            cap["font_size"] = int(o["caption_font_size"])
        if o.get("caption_animation"):
            cap["style"] = str(o["caption_animation"])
    if o.get("caption_anchor"):
        c.setdefault("style", {}).setdefault("captions", {})[
            "vertical_anchor"] = float(o["caption_anchor"])

    if o.get("sfx_enabled") is not None:
        sfx = c.setdefault("sfx", {})
        sfx["enabled"] = bool(o["sfx_enabled"])
        if o.get("sfx_pack"):
            sfx["pack"] = str(o["sfx_pack"])
        if o.get("sfx_volume_db") is not None:
            sfx["volume_db"] = float(o["sfx_volume_db"])

    st = c.setdefault("style", {})
    for key in ("speed_ramps", "punch_in", "popins", "transition",
                "intro", "outro"):
        if o.get(key):
            st[key] = o[key]

    if (o.get("credit_text") or "").strip():
        # creator credit appended to every clip description (channel auto-pull)
        c.setdefault("metadata", {})["credit_text"] = o["credit_text"].strip()

    return c


def config_hash(cfg: dict, *sections: str) -> str:
    """Stable short hash of selected config sections (cache keys)."""
    subset = {s: cfg.get(s) for s in sections} if sections else cfg
    blob = json.dumps(subset, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def file_hash(path: str | Path, chunk_mb: int = 8) -> str:
    """Streaming sha256 of a file (memory-safe for large videos)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_mb * 1024 * 1024):
            h.update(chunk)
    return h.hexdigest()
