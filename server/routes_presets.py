"""Editing presets: CRUD over presets/*.json + a pixel-faithful caption
preview so a preset can be tested before saving. Thin layer over presets.py."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from logutil import get_logger
from server.copy import friendly

log = get_logger("server")
router = APIRouter()


class PresetBody(BaseModel):
    preset: dict


@router.get("/api/edit-presets")
def get_presets():
    import presets
    return {"presets": presets.list_presets()}


@router.post("/api/edit-presets")
def create_preset(body: PresetBody):
    import presets
    try:
        return presets.save_preset(body.preset)
    except presets.PresetError as e:
        raise HTTPException(422, friendly(e, "Saving this preset"))


@router.delete("/api/edit-presets/{name}")
def remove_preset(name: str):
    import presets
    try:
        presets.delete_preset(name)
    except presets.PresetError:
        raise HTTPException(404, "That preset doesn't exist anymore.")
    return {"deleted": name}


@router.post("/api/edit-presets/preview")
def preview_preset(body: PresetBody):
    """Burn the preset's caption style onto the sample frame through the real
    ASS + ffmpeg path (style_preview) and return the PNG. Works on unsaved
    preset data so the editor can preview before saving."""
    import presets
    import style_preview
    from config import apply_run_options, load_config
    from schemas import SchemaValidationError, validate

    data = body.preset
    try:
        validate(data, "edit_preset")
    except SchemaValidationError as e:
        raise HTTPException(422, friendly(e, "Checking this preset"))
    opts = presets.expand(data)
    cfg = apply_run_options(load_config(), opts)
    preset_name = opts.get("preset") or cfg["captions"]["preset"]
    try:
        png = style_preview.preview_png(preset_name,
                                        opts.get("font_family") or None,
                                        cfg=cfg)
    except KeyError:
        raise HTTPException(422, "That caption style doesn't exist.")
    except Exception as e:  # noqa: BLE001 — a burn failure must not 500 opaquely
        raise HTTPException(500, friendly(e, "Rendering the preview"))
    return FileResponse(png, media_type="image/png")
