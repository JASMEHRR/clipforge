"""YouTube: connection state, one-time authorization (explicit user action
only), manual upload of a run's kept clips, the auto-upload panel, and the
manual 'Upload now' batch override on top of the auto-upload candidate
queue (upload_scheduler.find_candidates)."""
from __future__ import annotations

import threading
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from config import ROOT, load_config, save_config
from logutil import get_logger
from server.copy import friendly
from server.routes_library import safe_job_path

log = get_logger("server")
router = APIRouter()


def _authorized() -> bool:
    import youtube_upload as yt
    try:
        return bool(yt.credentials_available() and yt.has_cached_token())
    except Exception:  # noqa: BLE001 — any probe failure counts as not connected
        return False


@router.get("/api/youtube/state")
def youtube_state():
    import youtube_upload as yt
    from upload_scheduler import load_log, panel_state
    cfg = load_config()
    state = {"configured": yt.credentials_available(),
             "authorized": _authorized(),
             "setup_instructions": yt.SETUP_INSTRUCTIONS}
    try:
        state["panel"] = panel_state(cfg, load_log(), state["authorized"])
    except Exception as e:  # noqa: BLE001 — panel must render even if the log breaks
        log.warning("auto-upload panel state failed: %s", e)
        state["panel"] = None
    return state


@router.post("/api/youtube/authorize")
def authorize():
    """One-time OAuth in the user's browser. Only ever called from an explicit
    button press — never on page load."""
    import youtube_upload as yt
    if not yt.credentials_available():
        raise HTTPException(409, "YouTube isn't set up yet — add the Google "
                                 "credentials file first (see Settings).")
    try:
        yt.authorize()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, friendly(e, "Connecting to YouTube"))
    return {"authorized": _authorized()}


class AutoUploadRequest(BaseModel):
    enabled: bool


@router.put("/api/youtube/auto")
def set_auto_upload(req: AutoUploadRequest):
    try:
        save_config({"upload": {"auto_enabled": bool(req.enabled)}})
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, friendly(e, "Saving that setting"))
    return {"enabled": bool(req.enabled)}


class UploadRequest(BaseModel):
    job_name: str
    privacy: str = "private"


@router.post("/api/youtube/upload")
def upload_job(req: UploadRequest):
    import youtube_upload as yt
    from rerender import load_job
    if not _authorized():
        raise HTTPException(409, "Connect to YouTube first (one-time).")
    job_dir = safe_job_path(req.job_name)
    try:
        job = load_job(job_dir)
        results = []
        for c in job["clips"]:
            if not c.get("kept"):
                continue
            r = yt.upload_clip(c["path"], c["metadata"], privacy=req.privacy)
            results.append({"title": c["metadata"]["title"], "url": r["url"]})
        return {"uploaded": results}
    except Exception as e:  # noqa: BLE001 — includes friendly quota message
        raise HTTPException(502, friendly(e, "Uploading"))


# ---------------------------------------------------------------- queue --
# The auto-upload candidate queue (upload_scheduler.find_candidates), plus a
# manual "Upload now" override that publishes a chosen batch immediately
# instead of waiting for the next scheduled slot.

def _safe_candidate_path(key: str) -> Path:
    """Resolve an upload_scheduler candidate key ('output/<job>/clip_NN') to
    its clip directory, refusing anything that escapes output/."""
    import upload_scheduler as sched
    out_root = sched.OUTPUT_DIR.resolve()
    p = (ROOT / key).resolve()
    if not p.is_relative_to(out_root) or p == out_root:
        raise HTTPException(404, "That clip can't be found.")
    return p


def _candidate_summary(c: dict) -> dict:
    meta = c["meta"]
    vir = meta.get("virality", {})
    start, end = meta.get("original_source_start_s"), meta.get("original_source_end_s")
    return {
        "key": c["key"], "title": meta.get("title", "Untitled"), "score": c["score"],
        "band": vir.get("band"), "source_name": meta.get("source_name", ""),
        "duration": (end - start) if start is not None and end is not None else None,
        "video_url": f"/api/youtube/queue/video/{c['key']}",
        "duplicates": len(c.get("duplicates", [])),
    }


@router.get("/api/youtube/queue")
def youtube_queue():
    import upload_scheduler as sched
    cfg = load_config()
    log_data = sched.load_log()
    candidates = sched.find_candidates(cfg, log_data)
    wm = cfg.get("upload", {}).get("end_watermark", {})
    uploaded = sorted(log_data.get("uploads", {}).values(),
                      key=lambda e: e.get("uploaded_at", ""), reverse=True)
    return {
        "candidates": [_candidate_summary(c) for c in candidates],
        "uploads_today": sched.uploads_today(log_data),
        "max_per_day": cfg.get("upload", {}).get("max_per_day", 3),
        "end_watermark": {"enabled": bool(wm.get("enabled")),
                          "text": wm.get("text", "ClipForge"),
                          "duration_s": wm.get("duration_s", 1.2)},
        "uploaded": [{"title": e.get("title", "Untitled"),
                      "video_id": e.get("video_id", ""),
                      "url": f"https://youtu.be/{e.get('video_id', '')}",
                      "uploaded_at": e.get("uploaded_at", ""),
                      "publish_at": e.get("publish_at", ""),
                      "score": e.get("virality_score")} for e in uploaded],
    }


@router.get("/api/youtube/queue/video/{key:path}")
def queue_video(key: str):
    p = _safe_candidate_path(key) / "final.mp4"
    if not p.is_file():
        raise HTTPException(404, "File not found.")
    return FileResponse(str(p))


class QueueSelectRequest(BaseModel):
    mode: str = "top"          # "top" (best `count` by score) | "manual" (`keys`)
    count: int = 0
    keys: list[str] = []


def _select(req: QueueSelectRequest):
    """Server-authoritative reselection: always recomputes candidates fresh
    (never trusts a client-held list) so a stale UI can't upload/preview a
    clip that's since been excluded, uploaded elsewhere, or deleted."""
    import upload_scheduler as sched
    if req.mode not in ("top", "manual"):
        raise HTTPException(422, "That selection mode isn't recognized.")
    cfg = load_config()
    log_data = sched.load_log()
    candidates = sched.find_candidates(cfg, log_data)
    picked = sched.select_candidates(candidates, req.mode, req.count, req.keys)
    return cfg, log_data, picked


@router.post("/api/youtube/queue/select")
def queue_select(req: QueueSelectRequest):
    import upload_scheduler as sched
    cfg, log_data, picked = _select(req)
    return {"items": [_candidate_summary(c) for c in picked],
            "warning": sched.cap_warning(cfg, log_data, len(picked))}


_BATCHES: dict[str, dict] = {}
_BATCHES_LOCK = threading.Lock()


@router.post("/api/youtube/queue/upload")
def queue_upload(req: QueueSelectRequest):
    import upload_scheduler as sched
    import youtube_upload as yt
    if not _authorized():
        raise HTTPException(409, "Connect to YouTube first (one-time).")
    cfg, log_data, picked = _select(req)
    if not picked:
        raise HTTPException(422, "No clips to upload — pick at least one.")

    batch_id = uuid.uuid4().hex[:12]
    items = {c["key"]: {"key": c["key"],
                        "title": sched.build_snippet(c["meta"])["title"],
                        "status": "pending"} for c in picked}
    with _BATCHES_LOCK:
        _BATCHES[batch_id] = {"state": "running", "items": items}

    def on_progress(result: dict) -> None:
        with _BATCHES_LOCK:
            _BATCHES[batch_id]["items"][result["key"]] = result

    def work() -> None:
        try:
            youtube = yt.build_service()
            for c in picked:
                with _BATCHES_LOCK:
                    _BATCHES[batch_id]["items"][c["key"]]["status"] = "uploading"
                sched.upload_now(youtube, cfg, log_data, [c], on_progress=on_progress)
        except Exception as e:  # noqa: BLE001 — batch worker must never crash silently
            log.error("upload-now batch %s crashed: %s", batch_id, e)
        finally:
            with _BATCHES_LOCK:
                _BATCHES[batch_id]["state"] = "done"

    threading.Thread(target=work, daemon=True).start()
    return {"batch_id": batch_id}


@router.get("/api/youtube/queue/upload/{batch_id}")
def queue_upload_status(batch_id: str):
    with _BATCHES_LOCK:
        batch = _BATCHES.get(batch_id)
        if batch is None:
            raise HTTPException(404, "That batch isn't active anymore.")
        return {"state": batch["state"], "items": list(batch["items"].values())}
