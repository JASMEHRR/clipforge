"""Read-side library: finished jobs, clip files (video/srt/json/png), zip
bundles, music tracks, fonts, and caption-style previews."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from config import ROOT, load_config
from logutil import get_logger
from server.copy import friendly

log = get_logger("server")
router = APIRouter()

_FILE_SUFFIXES = {".mp4", ".srt", ".json", ".png", ".jpg", ".zip"}


def output_root() -> Path:
    return (ROOT / load_config()["paths"]["output_dir"]).resolve()


def safe_job_path(job_name: str, *parts: str) -> Path:
    """Resolve a path under output/ and refuse anything that escapes it."""
    root = output_root()
    p = (root / job_name).joinpath(*parts).resolve()
    if not p.is_relative_to(root):
        raise HTTPException(404, "File not found.")
    return p


def _load_job_record(job_name: str) -> dict:
    p = safe_job_path(job_name, "job.json")
    if not p.exists():
        raise HTTPException(404, "That run's files can't be found — "
                                 "it may have been deleted.")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise HTTPException(500, friendly(e, "Opening that run"))


def _clip_extras(job_name: str, clip: dict) -> dict:
    """Per-clip fields the UI needs beyond job.json, read from
    clip_NN/metadata.json (where find_candidates reads them): the auto-upload
    opt-out flag and the content niche."""
    excluded = False
    niche = None
    try:
        meta_p = safe_job_path(job_name, f"clip_{clip['index']:02d}",
                               "metadata.json")
        if meta_p.exists():
            meta = json.loads(meta_p.read_text(encoding="utf-8"))
            excluded = bool(meta.get("upload", {}).get("exclude"))
            niche = meta.get("niche")
    except Exception:  # noqa: BLE001 — missing/old metadata → defaults
        excluded = False
    return {"upload_excluded": excluded, "niche": niche}


@router.get("/api/jobs")
def list_jobs():
    """Past runs, newest first, straight from the output folder (covers runs
    that predate or bypass history.db)."""
    root = output_root()
    if not root.exists():
        return {"jobs": []}
    out = []
    for d in sorted((p for p in root.iterdir() if p.is_dir()), reverse=True):
        jp = d / "job.json"
        if not jp.exists():
            continue
        try:
            job = json.loads(jp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("skipping unreadable job %s: %s", d.name, e)
            continue
        clips = job.get("clips", [])
        out.append({"name": d.name, "created": job.get("created", ""),
                    "source": job.get("source", ""),
                    "status": job.get("status", ""),
                    "clip_count": len(clips),
                    "kept": sum(1 for c in clips if c.get("kept")),
                    "niches": sorted({c["niche"] for c in clips
                                      if c.get("niche")})})
    return {"jobs": out}


@router.get("/api/jobs/{job_name}")
def get_job(job_name: str):
    job = _load_job_record(job_name)
    for c in job.get("clips", []):
        c.update(_clip_extras(job_name, c))
    job["name"] = job_name
    return job


@router.get("/api/jobs/{job_name}/zip")
def zip_job(job_name: str):
    from bundle import zip_job as _zip
    job_dir = safe_job_path(job_name)
    if not (job_dir / "job.json").exists():
        raise HTTPException(404, "That run's files can't be found.")
    try:
        return FileResponse(str(_zip(job_dir)), filename=f"{job_name}.zip")
    except Exception as e:  # noqa: BLE001 — zip failure must be a sentence, not a 500 page
        raise HTTPException(500, friendly(e, "Packaging the clips"))


@router.get("/api/files/{job_name}/{clip}/{name}")
def get_file(job_name: str, clip: str, name: str):
    p = safe_job_path(job_name, clip, name)
    if p.suffix.lower() not in _FILE_SUFFIXES or not p.is_file():
        raise HTTPException(404, "File not found.")
    return FileResponse(str(p))  # FileResponse handles Range → video seeking


@router.get("/api/files/{job_name}/{name}")
def get_job_file(job_name: str, name: str):
    p = safe_job_path(job_name, name)
    if p.suffix.lower() not in _FILE_SUFFIXES or not p.is_file():
        raise HTTPException(404, "File not found.")
    return FileResponse(str(p))


@router.get("/api/music")
def list_music():
    import music
    try:
        return {"tracks": music.list_tracks()}
    except Exception as e:  # noqa: BLE001 — manifest optional
        log.warning("music manifest unavailable: %s", e)
        return {"tracks": []}


@router.get("/api/music/{track_id}/audio")
def music_audio(track_id: str):
    """Track audio for the picker's preview player (downloads on first use)."""
    import music
    try:
        track = music.get_track(track_id)
        path = Path(music.ensure_track(track)).resolve()
    except Exception as e:  # noqa: BLE001 — preview failure must not crash UI
        raise HTTPException(502, "Couldn't load that track right now. Check "
                                 "your internet connection and try again.")
    music_root = (ROOT / "assets" / "music").resolve()
    if not path.is_relative_to(music_root) or not path.is_file():
        raise HTTPException(404, "File not found.")
    return FileResponse(str(path))


@router.get("/api/fonts")
def list_fonts():
    import fontreg
    try:
        return {"fonts": fontreg.list_fonts(load_config())}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, friendly(e, "Listing fonts"))


@router.get("/api/presets")
def list_presets():
    cfg = load_config()
    return {"presets": sorted(cfg["captions"]["presets"].keys()),
            "default": cfg["captions"]["preset"]}


@router.get("/api/preview")
def style_preview(preset: str, font: str | None = None):
    """Real-burn caption preview PNG (cached by style_preview)."""
    from style_preview import preview_png
    cfg = load_config()
    if preset not in cfg["captions"]["presets"]:
        raise HTTPException(404, "That caption style doesn't exist.")
    try:
        return FileResponse(str(preview_png(preset, font or None, cfg)))
    except Exception as e:  # noqa: BLE001 — preview burn can fail without ffmpeg fonts
        raise HTTPException(500, friendly(e, "Drawing the style preview"))


@router.get("/api/profiles")
def list_profiles():
    pdir = ROOT / "profiles"
    profiles = []
    if pdir.exists():
        for p in sorted(pdir.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}
            profiles.append({"name": p.stem,
                             "description": data.get("description", "")})
    return {"profiles": profiles or [{"name": "default", "description": ""}]}
