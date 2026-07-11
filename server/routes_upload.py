"""YouTube: connection state, one-time authorization (explicit user action
only), manual upload of a run's kept clips, and the auto-upload panel."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config import load_config, save_config
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
