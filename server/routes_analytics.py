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
