"""Run lifecycle: file uploads, starting a run, live progress (WS + poll),
cancel. All heavy work happens on daemon threads via server.jobs."""
from __future__ import annotations

import asyncio
import re
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile, WebSocket, \
    WebSocketDisconnect
from pydantic import BaseModel

from config import ROOT, apply_run_options, load_config
from errors import JobCancelled
from logutil import get_logger
from server import jobs
from server.copy import friendly

log = get_logger("server")
router = APIRouter()

UPLOAD_DIR = ROOT / "cache" / "uploads"
BRANDING_DIR = ROOT / "assets" / "user_branding"
_CHUNK = 1024 * 1024


def _save_upload(file: UploadFile, dest_dir: Path, max_mb: int) -> Path:
    """Stream a multipart upload to disk with a size cap. Raises HTTPException
    with a plain-language message on failure."""
    if not file.filename:
        raise HTTPException(422, "That file has no name — please try again.")
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", Path(file.filename).name)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{uuid.uuid4().hex[:8]}_{safe}"
    limit = max_mb * 1024 * 1024
    written = 0
    try:
        with dest.open("wb") as out:
            while chunk := file.file.read(_CHUNK):
                written += len(chunk)
                if written > limit:
                    raise HTTPException(
                        413, f"That file is bigger than {max_mb} MB — "
                             "please use a smaller one.")
                out.write(chunk)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    except OSError as e:
        dest.unlink(missing_ok=True)
        raise HTTPException(500, friendly(e, "Saving your file"))
    return dest


@router.post("/api/uploads")
def upload_video(file: UploadFile):
    cfg = load_config()
    max_mb = int(cfg.get("ui", {}).get("max_upload_mb", 4096))
    dest = _save_upload(file, UPLOAD_DIR, max_mb)
    return {"path": str(dest)}


@router.post("/api/uploads/logo")
def upload_logo(file: UploadFile):
    """Persist a watermark logo under assets/user_branding/ (survives restarts;
    re-uploading the same name replaces it). Returns a repo-relative path as
    apply_run_options expects."""
    tmp = _save_upload(file, UPLOAD_DIR, 64)
    try:
        BRANDING_DIR.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]", "_",
                      Path(file.filename or "logo.png").name) or "logo.png"
        dest = BRANDING_DIR / safe
        shutil.copyfile(tmp, dest)
        return {"path": str(dest.relative_to(ROOT)).replace("\\", "/")}
    except OSError as e:
        raise HTTPException(500, friendly(e, "Saving your logo"))
    finally:
        tmp.unlink(missing_ok=True)


@router.post("/api/uploads/font")
def upload_font(file: UploadFile):
    import fontreg
    tmp = _save_upload(file, UPLOAD_DIR, 32)
    try:
        info = fontreg.register_upload(str(tmp))
        return {"family": info["family"]}
    except Exception as e:  # noqa: BLE001 — a bad font file must not 500 opaquely
        raise HTTPException(422, friendly(e, "Adding that font"))
    finally:
        tmp.unlink(missing_ok=True)


class RunRequest(BaseModel):
    source: str                       # URL or a path returned by /api/uploads
    preset: str | None = None
    aspect: str = "9:16"
    target_count: int | None = None   # None/0 = automatic
    provider: str | None = None
    music: str | None = None          # "", track id, "auto", "random"
    music_volume_db: float = -18.0
    style_refine: bool = True
    viral: bool = True
    viral_upload: bool = False        # privacy gate: explicit opt-in only
    style_profile: str | None = None
    subs_mode: str | None = None
    # per-run style & branding (config.apply_run_options keys)
    cta_text: str = ""
    highlight_hex: str = ""
    pacing: float | str = ""
    clip_min: float | str = ""
    clip_max: float | str = ""
    watermark_mode: str = "text"
    watermark_text: str = ""
    watermark_image: str = ""         # repo-relative path from /api/uploads/logo
    watermark_position: str = "bottom-right"
    font_family: str = ""


def _resolve_music_choice(choice: str | None) -> str:
    """'random' picks a concrete track id at run start (mirrors the old UI)."""
    if choice == "random":
        import random as _random

        import music
        try:
            ids = [t["id"] for t in music.list_tracks()]
        except Exception as e:  # noqa: BLE001 — manifest optional
            log.warning("music manifest unavailable: %s", e)
            ids = []
        return _random.choice(ids) if ids else ""
    return choice or ""


def _build_cfg(req: RunRequest) -> dict:
    cfg = apply_run_options(load_config(), {
        "cta_text": req.cta_text, "highlight_hex": req.highlight_hex,
        "preset": req.preset or None, "pacing": req.pacing,
        "clip_min": req.clip_min, "clip_max": req.clip_max,
        "watermark_text": req.watermark_text,
        "watermark_position": req.watermark_position,
        "watermark_mode": req.watermark_mode,
        "watermark_image": req.watermark_image,
        "font_family": req.font_family})
    if req.style_profile:
        cfg["style"]["profile"] = f"profiles/{req.style_profile}.json"
    cfg.setdefault("viral_v2", {})["enabled"] = bool(req.viral)
    # allow_upload comes only from an explicit per-run opt-in; the config
    # default stays false (privacy gate).
    cfg["viral_v2"]["allow_upload"] = bool(req.viral_upload)
    return cfg


@router.post("/api/runs")
def start_run(req: RunRequest):
    import pipeline

    source = req.source.strip()
    if not source:
        raise HTTPException(422, "Paste a link or choose a video file first.")
    if not source.startswith("http") and not Path(source).exists():
        raise HTTPException(422, "That video file can't be found — "
                                 "please choose it again.")
    try:
        cfg = _build_cfg(req)
    except Exception as e:  # noqa: BLE001 — bad option must not 500 opaquely
        raise HTTPException(422, friendly(e, "Checking your options"))
    music = _resolve_music_choice(req.music)
    try:
        job_dir = pipeline.new_job_dir(cfg, source)
    except OSError as e:
        raise HTTPException(500, friendly(e, "Starting this run"))
    handle = jobs.create(job_dir.name)

    def work(h: jobs.RunHandle) -> None:
        try:
            job = pipeline.run_job(
                source, cfg, provider=req.provider or None,
                job_dir=job_dir, preset=req.preset or None,
                aspect=req.aspect or "9:16",
                target_count=int(req.target_count or 0) or None,
                music=music or None,
                music_volume_db=float(req.music_volume_db),
                tracker=h.tracker, style_refine=bool(req.style_refine),
                subs_mode=req.subs_mode or None, cancel=h.cancel_event)
            h.finish("done", result=job)
        except JobCancelled:
            h.finish("cancelled")
        except Exception as e:  # noqa: BLE001 — worker must record, never raise
            h.finish("error", error=friendly(e, "This run"))

    jobs.launch(handle, work)
    return {"run_id": handle.id}


def _run_status(handle: jobs.RunHandle) -> dict:
    return {"run_id": handle.id, "state": handle.state,
            "snapshot": handle.snapshot(), "error": handle.error,
            "result": handle.result if handle.state == "done" else None}


def _run_list_item(handle: jobs.RunHandle) -> dict:
    """Compact summary for the Activity list: id, state, progress and the
    current stage label, derived from the handle's latest snapshot."""
    snap = handle.snapshot() or {}
    stages = snap.get("stages", [])
    running = [s for s in stages if s.get("state") == "running"]
    stage = (running[-1]["label"] if running
             else stages[-1]["label"] if stages else None)
    return {"run_id": handle.id, "state": handle.state,
            "overall": snap.get("overall", 0.0), "stage": stage,
            "elapsed": snap.get("elapsed"), "error": handle.error}


@router.get("/api/runs")
def list_runs():
    """All runs the in-memory registry still knows about (running + recently
    finished this session). Running first, then newest by id — job dir names
    are timestamp-prefixed, so a reverse id sort is chronological."""
    items = [_run_list_item(h) for h in jobs.REGISTRY.values()]
    running = [r for r in items if r["state"] == "running"]
    rest = sorted((r for r in items if r["state"] != "running"),
                  key=lambda r: r["run_id"], reverse=True)
    return {"runs": running + rest}


@router.get("/api/runs/{run_id}")
def run_status(run_id: str):
    handle = jobs.get(run_id)
    if handle is None:
        raise HTTPException(404, "That run isn't active anymore — "
                                 "check History for finished clips.")
    return _run_status(handle)


@router.post("/api/runs/{run_id}/cancel")
def cancel_run(run_id: str):
    handle = jobs.get(run_id)
    if handle is None:
        raise HTTPException(404, "That run isn't active anymore.")
    handle.cancel_event.set()
    return {"state": handle.state,
            "message": "Stopping — the current step finishes first, "
                       "then the run stops."}


@router.websocket("/ws/runs/{run_id}")
async def run_ws(ws: WebSocket, run_id: str):
    handle = jobs.get(run_id)
    if handle is None:
        await ws.close(code=4404)
        return
    await ws.accept()
    q = handle.subscribe()
    try:
        snap = handle.snapshot()
        if snap:
            await ws.send_json({"type": "snapshot", "data": snap})
        while True:
            try:
                item = await asyncio.wait_for(q.get(), timeout=1.0)
                await ws.send_json({"type": "snapshot", "data": item})
            except asyncio.TimeoutError:
                pass
            if handle.state != "running":
                await ws.send_json({
                    "type": handle.state,
                    "result": handle.result if handle.state == "done" else None,
                    "error": handle.error})
                break
    except WebSocketDisconnect:
        pass
    finally:
        handle.unsubscribe(q)
