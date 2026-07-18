"""Read-side library: finished jobs, clip files (video/srt/json/png), zip
bundles, music tracks, fonts, and caption-style previews."""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

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


def dir_size(path: Path) -> int:
    """Total size in bytes of everything under `path` (0 if it doesn't exist)."""
    total = 0
    for f in path.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            pass
    return total


def _clip_dir_from_key(key: str) -> Path:
    """Resolve an upload-scheduler clip key ('output/<job>/clip_NN', however
    the output dir is named) to its folder. Only the last two path segments
    (job, clip) are trusted; safe_job_path re-sandboxes them under output/."""
    parts = [p for p in key.replace("\\", "/").split("/") if p]
    if len(parts) < 2:
        raise HTTPException(404, "That clip can't be found.")
    return safe_job_path(parts[-2], parts[-1])


def _prune_job_clip(job_name: str, index: int) -> None:
    """Drop a deleted clip's entry from job.json so Results/History don't list
    a clip whose files are gone. Best-effort — a stale job.json never blocks
    the disk deletion that already happened."""
    try:
        jp = safe_job_path(job_name, "job.json")
        if not jp.exists():
            return
        job = json.loads(jp.read_text(encoding="utf-8"))
        clips = job.get("clips")
        if not isinstance(clips, list):
            return
        job["clips"] = [c for c in clips if c.get("index") != index]
        jp.write_text(json.dumps(job, indent=2, ensure_ascii=False),
                      encoding="utf-8")
    except (OSError, json.JSONDecodeError, HTTPException) as e:
        log.warning("could not prune %s clip %s from job.json: %s",
                    job_name, index, e)


def delete_clip_dir(clip_dir: Path) -> int:
    """Delete a clip folder (final.mp4 + intermediates) and return bytes freed,
    then prune its job.json entry. The upload log is deliberately never touched:
    a deleted-but-uploaded clip keeps its log entry, so it stays deduped and
    never becomes re-eligible for upload."""
    size = dir_size(clip_dir)
    shutil.rmtree(clip_dir)
    m = re.match(r"clip_(\d+)$", clip_dir.name)
    if m:
        _prune_job_clip(clip_dir.parent.name, int(m.group(1)))
    return size


class DeleteClipsRequest(BaseModel):
    keys: list[str]


@router.delete("/api/clips")
def delete_clips(req: DeleteClipsRequest):
    """Delete clip folders from disk. Refuses any clip that's currently
    uploading; keeps every upload_log entry intact (see delete_clip_dir)."""
    from server.routes_upload import key_is_uploading
    results = []
    reclaimed = 0
    for key in req.keys:
        try:
            clip_dir = _clip_dir_from_key(key)
        except HTTPException:
            results.append({"key": key, "status": "not_found"})
            continue
        if not clip_dir.is_dir():
            results.append({"key": key, "status": "not_found"})
            continue
        if key_is_uploading(key):
            results.append({"key": key, "status": "uploading"})
            continue
        try:
            freed = delete_clip_dir(clip_dir)
        except OSError as e:
            results.append({"key": key, "status": "error", "error": str(e)})
            continue
        reclaimed += freed
        results.append({"key": key, "status": "deleted", "bytes": freed})
    invalidate_all_clips_cache()
    return {"results": results, "reclaimed_bytes": reclaimed,
            "deleted": sum(1 for r in results if r["status"] == "deleted")}


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
    approval = "pending"
    size = 0
    try:
        clip_dir = safe_job_path(job_name, f"clip_{clip['index']:02d}")
        meta_p = clip_dir / "metadata.json"
        if clip_dir.is_dir():
            size = dir_size(clip_dir)
        if meta_p.exists():
            meta = json.loads(meta_p.read_text(encoding="utf-8"))
            excluded = bool(meta.get("upload", {}).get("exclude"))
            niche = meta.get("niche")
            approval = meta.get("upload", {}).get("approval") or "pending"
    except Exception:  # noqa: BLE001 — missing/old metadata → defaults
        excluded = False
    return {"upload_excluded": excluded, "niche": niche,
            "approval": approval, "bytes": size}


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


_ALL_CLIPS_CACHE: dict[str, list[dict]] = {}


def _cache_key() -> str:
    import config
    return config.current_workspace.get()


def invalidate_all_clips_cache() -> None:
    """Every route that changes a clip's status/approval/existence after the
    All-clips index has been scanned must call this — otherwise the cached
    list silently disagrees with disk until the user hits Refresh. Clears
    every workspace's cache (cheap — each rebuilds lazily on next request)."""
    _ALL_CLIPS_CACHE.clear()


def _clip_status(key: str, scheduled_keys: set, uploaded_keys: set,
                 approval: str, is_sample: bool) -> str:
    """sample|pending|approved|rejected|scheduled|uploaded. scheduled/uploaded
    come from upload_scheduler.classify_uploads (log-only, no live API call —
    that's the same fallback classification the YouTube tab uses when it
    can't reach the API) so this never disagrees with the log's own ground
    truth about what actually left the app."""
    if key in scheduled_keys:
        return "scheduled"
    if key in uploaded_keys:
        return "uploaded"
    if approval == "rejected":
        return "rejected"
    if approval == "approved":
        return "approved"
    if is_sample:
        return "sample"
    return "pending"


def _scan_all_clips() -> list[dict]:
    """Every clip on disk across every job, newest job first — the source list
    for the All-clips tab. Reuses the same job.json + metadata.json reads as
    /api/jobs/{name} (_load_job_record, _clip_extras) rather than a second
    parsing path."""
    root = output_root()
    if not root.exists():
        return []
    import upload_scheduler as sched
    from sample_source import SAMPLE_PATH
    log_data = sched.load_log()
    split = sched.classify_uploads(log_data)
    scheduled_keys = {r["key"] for r in split["scheduled"]}
    uploaded_keys = {r["key"] for r in split["published"]}

    out = []
    for jd in sorted((p for p in root.iterdir() if p.is_dir()), reverse=True):
        try:
            job = _load_job_record(jd.name)
        except HTTPException:
            continue
        is_sample = job.get("source") == str(SAMPLE_PATH)
        for c in job.get("clips", []):
            idx = c.get("index")
            if idx is None:
                continue
            extras = _clip_extras(jd.name, c)
            nn = f"{idx:02d}"
            key = f"output/{jd.name}/clip_{nn}"
            meta = c.get("metadata") or {}
            out.append({
                "key": key, "job": jd.name, "index": idx,
                "title": meta.get("title") or f"Clip {idx + 1}",
                "duration": c.get("duration"),
                "bytes": extras["bytes"], "niche": extras["niche"],
                "score": (c.get("virality") or {}).get("score"),
                "status": _clip_status(key, scheduled_keys, uploaded_keys,
                                       extras["approval"], is_sample),
                "created": job.get("created", ""),
                "video_url": f"/api/files/{jd.name}/clip_{nn}/final.mp4",
            })
    return out


@router.get("/api/clips/all")
def list_all_clips(refresh: bool = False, include_sample: bool = False):
    """Flat index of every clip on disk (all jobs, all statuses) for the
    All-clips library tab AND the Avatar Host clip picker — the single shared
    source for both. Cached after the first scan — pass ?refresh=1 (the UI's
    Refresh button) to force a rescan; every status-changing route (delete,
    approve/reject, exclude, upload) invalidates it too.

    status=="sample" clips (from `pipeline.py --sample --provider mock` demo
    runs) are excluded by default — they're keyless-demo output, not real
    content, and previously cluttered both tabs with mock: titles. Pass
    ?include_sample=1 to see them (e.g. while testing the sample flow)."""
    key = _cache_key()
    if key not in _ALL_CLIPS_CACHE or refresh:
        _ALL_CLIPS_CACHE[key] = _scan_all_clips()
    clips = _ALL_CLIPS_CACHE[key]
    if not include_sample:
        clips = [c for c in clips if c["status"] != "sample"]
    return {"clips": clips}


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
