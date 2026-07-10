"""Per-clip editing: re-render with new bounds/style, regenerate title &
description, snap bounds to sentences, and the auto-upload opt-out flag."""
from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from logutil import get_logger
from server import jobs
from server.copy import friendly
from server.routes_library import safe_job_path

log = get_logger("server")
router = APIRouter()


class RerenderRequest(BaseModel):
    start: float
    end: float
    preset: str | None = None
    style_refine: bool | None = None
    subs_mode: str | None = None


@router.post("/api/jobs/{job_name}/clips/{index}/rerender")
def rerender(job_name: str, index: int, req: RerenderRequest):
    """Starts a re-render on a worker thread; progress streams over the same
    /ws/runs/{id} channel as full runs. Re-render ids never enter history."""
    from rerender import rerender_clip
    job_dir = safe_job_path(job_name)
    if not (job_dir / "job.json").exists():
        raise HTTPException(404, "That run's files can't be found.")
    handle = jobs.create(f"rr_{uuid.uuid4().hex[:8]}")

    def work(h: jobs.RunHandle) -> None:
        try:
            clip = rerender_clip(job_dir, index, float(req.start),
                                 float(req.end), req.preset or None,
                                 style_refine=req.style_refine,
                                 subs_mode=req.subs_mode or None,
                                 tracker=h.tracker)
            h.finish("done", result=clip)
        except Exception as e:  # noqa: BLE001 — worker must record, never raise
            h.finish("error", error=friendly(e, "Re-rendering this clip"))

    jobs.launch(handle, work)
    return {"run_id": handle.id}


@router.post("/api/jobs/{job_name}/clips/{index}/metadata")
def regen_metadata(job_name: str, index: int):
    from rerender import regenerate_metadata
    job_dir = safe_job_path(job_name)
    try:
        return regenerate_metadata(job_dir, index)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, friendly(
            e, "Regenerating the title and description"))


@router.get("/api/jobs/{job_name}/clips/snap")
def snap(job_name: str, start: float, end: float):
    from rerender import snap_bounds
    job_dir = safe_job_path(job_name)
    try:
        s, e = snap_bounds(job_dir, float(start), float(end))
        return {"start": s, "end": e}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, friendly(e, "Snapping to sentences"))


class ExcludeRequest(BaseModel):
    exclude: bool


@router.put("/api/jobs/{job_name}/clips/{index}/exclude")
def set_exclude(job_name: str, index: int, req: ExcludeRequest):
    """Persist the per-clip auto-upload opt-out where the upload scheduler's
    find_candidates reads it (clip_NN/metadata.json → upload.exclude)."""
    meta_path = safe_job_path(job_name, f"clip_{index:02d}", "metadata.json")
    if not meta_path.exists():
        raise HTTPException(404, "That clip can't be found.")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta.setdefault("upload", {})["exclude"] = bool(req.exclude)
        meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False),
                             encoding="utf-8")
    except (OSError, json.JSONDecodeError) as e:
        raise HTTPException(500, friendly(e, "Saving that choice"))
    return {"exclude": bool(req.exclude)}
