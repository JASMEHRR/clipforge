"""Settings (persisted to config.local.yaml only), hardware summary,
self-updater, and the batch queue."""
from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from config import load_config, save_config
from logutil import get_logger
from server.copy import friendly

log = get_logger("server")
router = APIRouter()


@router.get("/api/settings")
def get_settings():
    cfg = load_config()
    llm = cfg.get("llm", {})
    return {
        "compute": cfg.get("render", {}).get("compute", "auto"),
        "whisper_model": cfg.get("whisper", {}).get("model_override", ""),
        "provider": llm.get("provider", "mock"),
        "gemini_model": llm.get("gemini_model", ""),
        "groq_model": llm.get("groq_model", ""),
        "ollama_model": llm.get("ollama_model", ""),
        "auto_open": cfg.get("ui", {}).get("auto_open", True),
        "custom_niches": ", ".join(
            cfg.get("classify", {}).get("custom_niches", []) or []),
    }


class SettingsRequest(BaseModel):
    compute: str = "auto"
    whisper_model: str = ""
    provider: str = "mock"
    gemini_model: str = ""
    groq_model: str = ""
    ollama_model: str = ""
    custom_niches: str = ""


@router.put("/api/settings")
def put_settings(req: SettingsRequest):
    if req.compute not in ("auto", "gpu", "cpu"):
        raise HTTPException(422, "Speed setting must be auto, gpu or cpu.")
    try:
        save_config({
            "render": {"compute": req.compute},
            "whisper": {"model_override": req.whisper_model or ""},
            "llm": {"provider": req.provider,
                    "gemini_model": req.gemini_model.strip(),
                    "groq_model": req.groq_model.strip(),
                    "ollama_model": req.ollama_model.strip()},
            "classify": {"custom_niches": sorted(
                {s.strip().lower() for s in req.custom_niches.split(",")
                 if s.strip()})},
        })
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, friendly(e, "Saving your settings"))
    return get_settings()


@router.get("/api/system")
def system_info():
    import updater
    from ffutil import nvenc_available
    from transcribe import gpu_available
    try:
        gpu = bool(gpu_available())
        nvenc = bool(nvenc_available())
    except Exception as e:  # noqa: BLE001 — probes must not break the page
        log.warning("hardware probe failed: %s", e)
        gpu = nvenc = False
    speed = ("full" if gpu and nvenc else
             "partial" if gpu or nvenc else "cpu")
    return {"version": updater.current_version(),
            "cpu_count": os.cpu_count(),
            "gpu_transcribe": gpu, "gpu_encode": nvenc,
            "acceleration": speed}


@router.get("/api/update")
def update_state():
    import updater
    return {"state": updater.get_state(),
            "current": updater.current_version()}


@router.post("/api/update/check")
def update_check():
    import updater
    try:
        return {"state": updater.check_for_update(),
                "current": updater.current_version()}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, friendly(e, "Checking for updates"))


@router.post("/api/update/apply")
def update_apply():
    import updater
    try:
        return {"message": updater.apply_update()}
    except Exception as e:  # noqa: BLE001 — updater rolls back; say why
        raise HTTPException(500,
                            f"Update failed (no changes were kept): {e}")


# ------------------------------------------------------------------ batch

class BatchAddRequest(BaseModel):
    sources: list[str]
    music: str = ""


@router.get("/api/batch")
def batch_status():
    from batch import get_queue
    return {"rows": get_queue().status_rows()}


@router.post("/api/batch")
def batch_add(req: BatchAddRequest):
    from batch import get_queue
    from server.routes_run import _resolve_music_choice
    q = get_queue()
    n = 0
    for line in req.sources:
        line = line.strip()
        if not line:
            continue
        resolved = _resolve_music_choice(req.music)
        q.add(line, **({"music": resolved} if resolved else {}))
        n += 1
    return {"queued": n, "rows": q.status_rows()}


@router.get("/api/batch/zip")
def batch_zip():
    """One zip of every finished queue job (mirrors the old UI's
    'Download everything')."""
    from batch import get_queue
    from bundle import zip_jobs
    dirs = [i["job_dir"] for i in get_queue().items
            if i["status"] == "done" and i.get("job_dir")]
    if not dirs:
        raise HTTPException(404, "Nothing finished in the queue yet.")
    try:
        return FileResponse(str(zip_jobs(dirs)), filename="clipforge-all.zip")
    except Exception as e:  # noqa: BLE001 — zip failure must be a sentence
        raise HTTPException(500, friendly(e, "Packaging the clips"))


class InboxRequest(BaseModel):
    enabled: bool


@router.put("/api/batch/inbox")
def batch_inbox(req: InboxRequest):
    from batch import get_queue
    q = get_queue()
    msg = (q.start_inbox_watcher() if req.enabled
           else q.stop_inbox_watcher())
    return {"enabled": bool(req.enabled), "message": msg}
