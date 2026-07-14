"""Read-only Analytics tab: channel overview + per-video table for uploads
made through ClipForge, plus recommendations derived from that data. Never
writes to YouTube — the one write path here (`apply_publish_slot`) only
touches ClipForge's own config."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config import load_config, save_config
from logutil import get_logger
from server.copy import friendly

log = get_logger("server")
router = APIRouter()


@router.get("/api/analytics/state")
def analytics_state(refresh: bool = False):
    import youtube_upload as yt
    state = {"configured": yt.credentials_available(),
             "authorized": yt.authorized(),
             "setup_instructions": yt.SETUP_INSTRUCTIONS}
    if not state["authorized"]:
        return state

    import analytics
    try:
        data = analytics.refresh(force=refresh)
    except Exception as e:  # noqa: BLE001 — includes friendly quota/network message
        raise HTTPException(502, friendly(e, "Fetching YouTube analytics"))

    import analytics_insights
    state.update(overview=data["overview"], videos=data["videos"],
                 fetched_at=data["fetched_at"],
                 recommendations=analytics_insights.recommend(data["videos"]))
    return state


class ApplySlotRequest(BaseModel):
    hour: int


@router.put("/api/analytics/publish-slot")
def apply_publish_slot(req: ApplySlotRequest):
    if not 0 <= req.hour <= 23:
        raise HTTPException(422, "That's not a valid hour.")
    cfg = load_config()
    slots = sorted(set(cfg.get("upload", {}).get("publish_slots_ist", [])) | {req.hour})
    try:
        cfg = save_config({"upload": {"publish_slots_ist": slots}})
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, friendly(e, "Saving that setting"))
    return {"publish_slots_ist": cfg["upload"]["publish_slots_ist"]}


# ------------------------------------------------------- schedule intelligence --
# "Self-learning" publish hours (publish_timing.py). Reads only local files
# (upload_log.json, its own stats store, config) — no YouTube auth required,
# so the panel shows real state (gates, sample counts) even before the
# channel is connected.

@router.get("/api/analytics/publish-timing")
def publish_timing_panel():
    import publish_timing
    import upload_scheduler as sched
    return publish_timing.publish_timing_state(load_config(), sched.load_log())


@router.post("/api/analytics/publish-timing/recompute")
def publish_timing_recompute():
    """The daily tweak loop, on demand — recomputes the active hour ranking
    and logs a changelog entry when it changes."""
    import publish_timing
    import upload_scheduler as sched
    return publish_timing.recompute_ranking(load_config(), sched.load_log())


class PublishTimingEnabledRequest(BaseModel):
    enabled: bool


@router.put("/api/analytics/publish-timing/enabled")
def set_publish_timing_enabled(req: PublishTimingEnabledRequest):
    import publish_timing
    try:
        publish_timing.set_enabled(bool(req.enabled))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, friendly(e, "Saving that setting"))
    return {"enabled": bool(req.enabled)}


class PublishTimingHourRequest(BaseModel):
    hour: int


@router.post("/api/analytics/publish-timing/pin")
def pin_publish_hour(req: PublishTimingHourRequest):
    import publish_timing
    try:
        pinned = publish_timing.toggle_pin(load_config(), req.hour)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"pinned_hours": pinned}


@router.post("/api/analytics/publish-timing/ban")
def ban_publish_hour(req: PublishTimingHourRequest):
    import publish_timing
    try:
        banned = publish_timing.toggle_ban(load_config(), req.hour)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"banned_hours": banned}


@router.post("/api/analytics/publish-timing/reset")
def reset_publish_timing():
    """Owner control: forget every learned score (config — enabled state,
    pins/bans, gates — is untouched)."""
    import publish_timing
    publish_timing.reset_stats()
    return {"reset": True}
