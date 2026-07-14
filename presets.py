"""Editing presets: named, saved bundles of per-run options (caption style,
aspect, SFX pack, music, speed ramps, punch-in zooms, keyword pop-ins,
transitions, intro/outro, watermark).

A preset is one JSON file in presets/<slug>.json validated against the
`edit_preset` schema. `expand()` flattens a preset into the flat option dict
that `config.apply_run_options` consumes, plus the run-level keys the caller
threads into `pipeline.run_job` directly (`aspect`, `music`,
`music_volume_db`). Presets never contain logic — they are pure data, so the
same preset drives the pipeline, re-render, and channel auto-pull identically."""
from __future__ import annotations

import json
import re
from pathlib import Path

from config import ROOT
from errors import ClipForgeError
from logutil import get_logger
from schemas import SchemaValidationError, validate

log = get_logger("presets")

PRESETS_DIR = ROOT / "presets"


class PresetError(ClipForgeError):
    stage = "presets"


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not s:
        raise PresetError(f"preset name unusable as a filename: {name!r}")
    return s


def _path(name: str) -> Path:
    return PRESETS_DIR / f"{_slug(name)}.json"


def list_presets() -> list[dict]:
    """All valid presets, sorted by name. Malformed files are skipped with a
    warning — one broken preset must not take down the preset list."""
    if not PRESETS_DIR.exists():
        return []
    out = []
    for p in sorted(PRESETS_DIR.glob("*.json")):
        try:
            out.append(load_preset_file(p))
        except (PresetError, OSError) as e:
            log.warning("skipping invalid preset %s: %s", p.name, e)
    return sorted(out, key=lambda x: x["name"].lower())


def load_preset_file(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise PresetError(f"preset unreadable: {path.name}", detail=str(e)) from e
    try:
        validate(data, "edit_preset")
    except SchemaValidationError as e:
        raise PresetError(f"preset invalid: {path.name}", detail=str(e)) from e
    return data


def load_preset(name: str) -> dict:
    path = _path(name)
    if not path.exists():
        raise PresetError(f"unknown preset '{name}'")
    return load_preset_file(path)


def save_preset(data: dict) -> dict:
    """Validate and persist a preset (create or overwrite by name)."""
    try:
        validate(data, "edit_preset")
    except SchemaValidationError as e:
        raise PresetError("preset does not match the schema",
                          detail=str(e)) from e
    PRESETS_DIR.mkdir(parents=True, exist_ok=True)
    path = _path(data["name"])
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)
    log.info("preset saved: %s", path.name)
    return data


def delete_preset(name: str) -> None:
    path = _path(name)
    if not path.exists():
        raise PresetError(f"unknown preset '{name}'")
    path.unlink()
    log.info("preset deleted: %s", path.name)


def expand(preset: dict) -> dict:
    """Preset → flat run-option dict.

    Keys consumed by config.apply_run_options: preset, font_family,
    highlight_hex, caption_primary_hex, caption_font_size, caption_anchor,
    caption_animation, cta_text, watermark_*, sfx_enabled/sfx_pack/
    sfx_volume_db, speed_ramps, punch_in, popins, transition, intro, outro.
    Run-level keys the caller threads into run_job itself: aspect, music,
    music_volume_db."""
    opts: dict = {}
    cap = preset.get("caption", {})
    if cap.get("preset"):
        opts["preset"] = cap["preset"]
    if cap.get("font_family"):
        opts["font_family"] = cap["font_family"]
    if cap.get("highlight_hex"):
        opts["highlight_hex"] = cap["highlight_hex"]
    if cap.get("primary_hex"):
        opts["caption_primary_hex"] = cap["primary_hex"]
    if cap.get("font_size"):
        opts["caption_font_size"] = cap["font_size"]
    if cap.get("anchor"):
        opts["caption_anchor"] = cap["anchor"]
    if cap.get("animation"):
        opts["caption_animation"] = cap["animation"]

    if preset.get("cta_text"):
        opts["cta_text"] = preset["cta_text"]

    wm = preset.get("watermark", {})
    if wm.get("mode"):
        opts["watermark_mode"] = wm["mode"]
    if wm.get("text"):
        opts["watermark_text"] = wm["text"]
    if wm.get("image"):
        opts["watermark_image"] = wm["image"]
    if wm.get("position"):
        opts["watermark_position"] = wm["position"]

    sfx = preset.get("sfx", {})
    if sfx:
        opts["sfx_enabled"] = bool(sfx.get("enabled", False))
        if sfx.get("pack"):
            opts["sfx_pack"] = sfx["pack"]
        if sfx.get("volume_db") is not None:
            opts["sfx_volume_db"] = sfx["volume_db"]

    for key in ("speed_ramps", "punch_in", "popins", "transition",
                "intro", "outro"):
        if preset.get(key):
            opts[key] = preset[key]

    # run-level args (not config keys) — caller pops these
    if preset.get("aspect"):
        opts["aspect"] = preset["aspect"]
    music = preset.get("music", {})
    if music.get("track"):
        opts["music"] = music["track"]
    if music.get("volume_db") is not None:
        opts["music_volume_db"] = music["volume_db"]
    return opts
