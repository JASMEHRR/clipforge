"""YouTube: connection state, one-time authorization (explicit user action
only), manual upload of a run's kept clips, the auto-upload panel, and the
manual 'Upload now' batch override on top of the auto-upload candidate
queue (upload_scheduler.find_candidates)."""
from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from config import ROOT, load_config, save_config
from logutil import get_logger
from server.copy import friendly
from server.routes_library import invalidate_all_clips_cache, safe_job_path

log = get_logger("server")
router = APIRouter()


def _authorized() -> bool:
    import youtube_upload as yt
    return yt.authorized()


def _dry_run() -> bool:
    import youtube_upload as yt
    return yt.dry_run()


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
    import upload_scheduler as sched
    import youtube_upload as yt
    from rerender import load_job
    if not _authorized():
        raise HTTPException(409, "Connect to YouTube first (one-time).")
    cfg = load_config()
    job_dir = safe_job_path(req.job_name)
    try:
        job = load_job(job_dir)
        results = []
        skipped = 0
        for c in job["clips"]:
            if not c.get("kept"):
                continue
            # the manual per-job path bypasses find_candidates, so it applies
            # the same approval gate itself (unreadable metadata == pending)
            try:
                meta = json.loads(
                    (Path(c["path"]).parent / "metadata.json")
                    .read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                meta = {}
            if not sched.approval_ok(meta, cfg):
                skipped += 1
                continue
            r = yt.upload_clip(c["path"], c["metadata"], privacy=req.privacy)
            results.append({"title": c["metadata"]["title"], "url": r["url"]})
        return {"uploaded": results, "skipped_approval": skipped}
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
    from server.routes_library import dir_size
    meta = c["meta"]
    vir = meta.get("virality", {})
    start, end = meta.get("original_source_start_s"), meta.get("original_source_end_s")
    return {
        "key": c["key"], "title": meta.get("title", "Untitled"), "score": c["score"],
        "band": vir.get("band"), "source_name": meta.get("source_name", ""),
        "niche": meta.get("niche"),
        "duration": (end - start) if start is not None and end is not None else None,
        "video_url": f"/api/youtube/queue/video/{c['key']}",
        "duplicates": len(c.get("duplicates", [])),
        "bytes": dir_size(c["dir"]),
    }


@router.get("/api/youtube/queue")
def youtube_queue():
    import archive
    import upload_scheduler as sched
    cfg = load_config()
    log_data = sched.load_log()
    from server.routes_library import _clip_dir_from_key
    candidates = sched.find_candidates(cfg, log_data)
    wm = cfg.get("upload", {}).get("end_watermark", {})

    def _on_disk(key: str) -> bool:
        try:
            return _clip_dir_from_key(key).is_dir()
        except HTTPException:
            return False

    uploaded = sorted(
        ({**v, "key": k, "on_disk": _on_disk(k)}
         for k, v in log_data.get("uploads", {}).items()),
        key=lambda e: e.get("uploaded_at", ""), reverse=True)

    # Split scheduled-on-YouTube vs published, refining with live status when
    # we can reach the API (best-effort; falls back to the publishAt clock).
    live = None
    if _authorized():
        try:
            import youtube_upload as yt
            ids = [e.get("video_id") for e in log_data.get("uploads", {}).values()
                   if e.get("video_id")]
            live = yt.video_status(ids, service=yt.build_service())
        except Exception as e:  # noqa: BLE001 — status is optional context
            log.info("live video status unavailable: %s", e)
    split = sched.classify_uploads(log_data, live)
    on_disk = {u["key"]: u["on_disk"] for u in uploaded}
    archived_ids = archive.index_by_video_id()  # one directory walk, not one glob per row
    zipped_ids = archive.zipped_video_ids()
    for row in split["scheduled"] + split["published"]:
        row["on_disk"] = on_disk.get(row["key"], False)
        vid = row.get("video_id")
        has_folder = vid in archived_ids
        # "archived" covers both: still a live folder in archive/uploaded/, or
        # already swept into a backup zip. archive_zip only names the backup
        # when there's no live folder left to open — a zipped-but-kept clip
        # (delete_originals was off) still shows "Open folder", not this.
        row["archived"] = has_folder or vid in zipped_ids
        row["archive_zip"] = (archive.find_zip_for(vid)
                              if not has_folder and vid in zipped_ids else None)
    return {
        "candidates": [_candidate_summary(c) for c in candidates],
        "require_approval": bool(
            cfg.get("upload", {}).get("require_approval", False)),
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
                      "key": e.get("key", ""), "on_disk": e.get("on_disk", False),
                      "score": e.get("virality_score")} for e in uploaded],
        "scheduled": split["scheduled"],
        "published": split["published"],
        "quota": sched.quota_status(cfg, log_data),
        "schedule_ahead_days": cfg.get("upload", {}).get("schedule_ahead_days", 3),
        "dry_run": _dry_run(),
    }


# ------------------------------------------------------------ approvals --
# The review step between "produced" and "uploadable": pending clips wait
# here until the owner approves or rejects them (upload_scheduler holds the
# gate; these routes only read state and write the per-clip decision).

@router.get("/api/youtube/approvals")
def youtube_approvals():
    import upload_scheduler as sched
    cfg = load_config()
    upload_cfg = cfg.get("upload", {})
    log_data = sched.load_log()
    pending = sched.find_pending_approval(cfg, log_data)
    # Proposed (not reserved) slots so the owner sees roughly when each clip
    # would publish if approved right now, in scan order.
    slots = sched.next_publish_times(
        len(pending), None, log_data,
        upload_cfg.get("publish_slots_ist", [12, 19]),
        upload_cfg.get("slot_spacing_minutes", 60))
    items = []
    for c, when in zip(pending, slots):
        item = _candidate_summary(c)
        item["description"] = c["meta"].get("description", "")
        item["hashtags"] = c["meta"].get("hashtags", [])
        item["proposed_publish_at"] = when.isoformat()
        items.append(item)
    return {"items": items,
            "require_approval": bool(upload_cfg.get("require_approval",
                                                    False))}


class ApproveAllRequest(BaseModel):
    approval: str = "approved"


@router.post("/api/youtube/approvals/all")
def approve_all(req: ApproveAllRequest):
    """Approve (or reject) every pending clip in one server-side pass — one
    request, no client-side race against a changing pending list."""
    import upload_scheduler as sched
    if req.approval not in ("approved", "rejected"):
        raise HTTPException(422, "Approval must be approved or rejected.")
    cfg = load_config()
    pending = sched.find_pending_approval(cfg, sched.load_log())
    updated = 0
    for c in pending:
        meta_path = c["dir"] / "metadata.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta.setdefault("upload", {})["approval"] = req.approval
            meta_path.write_text(json.dumps(meta, indent=2,
                                            ensure_ascii=False),
                                 encoding="utf-8")
            updated += 1
        except (OSError, json.JSONDecodeError) as e:
            log.warning("approve-all skipped %s: %s", c["key"], e)
    if updated:
        invalidate_all_clips_cache()
    return {"updated": updated}


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


def key_is_uploading(key: str) -> bool:
    """True while an in-flight 'Upload now' batch is still handling this clip —
    deleting its file mid-upload would corrupt the upload, so delete refuses."""
    with _BATCHES_LOCK:
        for batch in _BATCHES.values():
            if batch["state"] != "running":
                continue
            item = batch["items"].get(key)
            if item and item.get("status") in ("pending", "uploading"):
                return True
    return False


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
            invalidate_all_clips_cache()  # some clips may have gone out before a crash

    threading.Thread(target=work, daemon=True).start()
    return {"batch_id": batch_id}


@router.get("/api/youtube/queue/upload/{batch_id}")
def queue_upload_status(batch_id: str):
    with _BATCHES_LOCK:
        batch = _BATCHES.get(batch_id)
        if batch is None:
            raise HTTPException(404, "That batch isn't active anymore.")
        return {"state": batch["state"], "items": list(batch["items"].values())}


# --------------------------------------------------------- disk cleanup --

@router.get("/api/storage")
def storage():
    """Disk used by output/, and how much is safely reclaimable — the local
    files of clips already uploaded (they live on YouTube now)."""
    import upload_scheduler as sched
    from server.routes_library import _clip_dir_from_key, dir_size, output_root
    root = output_root()
    total = dir_size(root) if root.exists() else 0
    cleanable = 0
    for key in sched.load_log().get("uploads", {}):
        try:
            d = _clip_dir_from_key(key)
        except HTTPException:
            continue
        if d.is_dir() and not key_is_uploading(key):
            cleanable += dir_size(d)
    return {"total_bytes": total, "cleanable_bytes": cleanable}


@router.post("/api/youtube/cleanup-uploaded")
def cleanup_uploaded():
    """Delete local files of every clip already uploaded — but only once a
    permanent archive/uploaded/ copy exists for it (archiving it first if
    needed). Keeps the upload log (history + dedupe intact); skips anything
    mid-upload, and skips (with a warning) anything that can't be archived
    rather than deleting it with no copy anywhere."""
    import archive
    import upload_scheduler as sched
    from server.routes_library import _clip_dir_from_key, delete_clip_dir
    deleted = 0
    reclaimed = 0
    for key, entry in sched.load_log().get("uploads", {}).items():
        if key_is_uploading(key):
            continue
        try:
            d = _clip_dir_from_key(key)
        except HTTPException:
            continue
        if not d.is_dir():
            continue
        if not archive.ensure_archived(key, entry):
            log.warning("cleanup skipped %s: couldn't archive it first", key)
            continue
        try:
            reclaimed += delete_clip_dir(d)
            deleted += 1
        except OSError as e:
            log.warning("cleanup skipped %s: %s", key, e)
    if deleted:
        invalidate_all_clips_cache()
    return {"deleted": deleted, "reclaimed_bytes": reclaimed}


# ------------------------------------------------------- schedule-ahead --

@router.post("/api/youtube/sync-schedule")
def sync_schedule():
    """Pre-book open publish slots across the horizon with approved clips
    (private + publishAt), so YouTube publishes them with the app closed.
    Stops cleanly at today's quota, the horizon's open slots, or the last
    approved clip — whichever comes first."""
    import upload_scheduler as sched
    import youtube_upload as yt
    if not _authorized():
        raise HTTPException(409, "Connect to YouTube first (one-time).")
    cfg = load_config()
    log_data = sched.load_log()
    try:
        result = sched.sync_schedule(yt.build_service(),
                                     yt.build_analytics_service(), cfg, log_data)
    except Exception as e:  # noqa: BLE001 — includes friendly quota message
        raise HTTPException(502, friendly(e, "Scheduling uploads"))
    if result.get("scheduled"):
        invalidate_all_clips_cache()
    return result


class UnscheduleRequest(BaseModel):
    key: str


@router.post("/api/youtube/unschedule")
def unschedule(req: UnscheduleRequest):
    """Pull a pre-booked clip back before it publishes (deletes the private
    upload on YouTube, frees the slot, makes the clip eligible again)."""
    import upload_scheduler as sched
    import youtube_upload as yt
    from errors import UploadError
    if not _authorized():
        raise HTTPException(409, "Connect to YouTube first (one-time).")
    log_data = sched.load_log()
    try:
        result = sched.unschedule(yt.build_service(), req.key, log_data)
    except UploadError as e:
        raise HTTPException(409, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, friendly(e, "Un-scheduling"))
    invalidate_all_clips_cache()
    return result


# ------------------------------------------------------------- archive --

@router.post("/api/archive/backfill")
def archive_backfill():
    """One-time sweep: archive everything already in the upload log that
    isn't archived yet (best-effort — a clip whose output/ files are already
    gone can't be recovered)."""
    import archive
    import upload_scheduler as sched
    result = archive.backfill_from_log(sched.load_log())
    if result["archived"]:
        invalidate_all_clips_cache()
    return result


@router.post("/api/archive/open/{video_id}")
def archive_open_folder(video_id: str):
    """Open this clip's permanent archive folder in Explorer."""
    import archive
    d = archive.find_archive_dir(video_id)
    if not d:
        raise HTTPException(404, "That clip hasn't been archived yet.")
    try:
        os.startfile(str(d))  # Windows-only app (see CLAUDE.md); path came
    except OSError as e:      # from find_archive_dir, already sandboxed under archive/
        raise HTTPException(500, friendly(e, "Opening that folder"))
    return {"opened": str(d)}


@router.get("/api/archive/zip-status")
def archive_zip_status():
    """How many archived clips aren't backed up into a zip yet, and whether
    that's enough to prompt for a new backup — the Uploaded panel's banner."""
    import archive
    return archive.zip_status()


_ZIP_JOBS: dict[str, dict] = {}
_ZIP_JOBS_LOCK = threading.Lock()


class ZipBackupRequest(BaseModel):
    delete_originals: bool = False


@router.post("/api/archive/zip")
def start_zip_backup(req: ZipBackupRequest):
    """Zip every not-yet-backed-up archived clip into archive/backups/ in a
    background thread (streamed, verified before the manifest updates — see
    archive.create_backup_zip); poll /api/archive/zip/{job_id} for progress."""
    import archive
    job_id = uuid.uuid4().hex[:12]
    with _ZIP_JOBS_LOCK:
        _ZIP_JOBS[job_id] = {"state": "running", "done": 0, "total": 0}

    def on_progress(done: int, total: int) -> None:
        with _ZIP_JOBS_LOCK:
            _ZIP_JOBS[job_id]["done"] = done
            _ZIP_JOBS[job_id]["total"] = total

    def work() -> None:
        try:
            result = archive.create_backup_zip(
                on_progress=on_progress, delete_originals=req.delete_originals)
            # create_backup_zip reports a verify/IO failure by returning an
            # "error" key rather than raising — that must reach the client as
            # state="error" too, not "done" (the frontend only surfaces errors
            # from the "error" state; "done" with zipped=0 reads as the
            # harmless "nothing new to back up" case)
            state = "error" if result.get("error") else "done"
            with _ZIP_JOBS_LOCK:
                _ZIP_JOBS[job_id].update(state=state, **result)
        except Exception as e:  # noqa: BLE001 — job worker must never crash silently
            log.error("zip backup job %s crashed: %s", job_id, e)
            with _ZIP_JOBS_LOCK:
                _ZIP_JOBS[job_id].update(state="error", error=str(e))

    threading.Thread(target=work, daemon=True).start()
    return {"job_id": job_id}


@router.get("/api/archive/zip/{job_id}")
def zip_backup_status(job_id: str):
    with _ZIP_JOBS_LOCK:
        job = _ZIP_JOBS.get(job_id)
        if job is None:
            raise HTTPException(404, "That backup job isn't active anymore.")
        return dict(job)
