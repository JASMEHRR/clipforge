"""Approved channels & auto-pull: CRUD, pause/resume, manual poll, pool view.
Thin layer over channels.py. permission_source/credit_text are optional
(recommended); credit still threads into descriptions when set."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from logutil import get_logger
from server.copy import friendly

log = get_logger("server")
router = APIRouter()


class ChannelCreate(BaseModel):
    url: str
    permission_source: str = ""   # optional but recommended
    credit_text: str = ""
    name: str = ""
    default_preset: str = ""
    top_n: int | None = None
    account: str = "default"      # destination YouTube upload account


class ChannelPatch(BaseModel):
    name: str | None = None
    permission_source: str | None = None
    credit_text: str | None = None
    paused: bool | None = None
    default_preset: str | None = None
    top_n: int | None = None
    account: str | None = None


@router.get("/api/channels")
def list_channels():
    import channels
    return {"channels": channels.channel_stats()}


@router.post("/api/channels")
def create_channel(body: ChannelCreate):
    import channels
    try:
        return channels.add_channel(
            body.url, body.permission_source, body.credit_text,
            name=body.name, default_preset=body.default_preset,
            top_n=body.top_n, account=body.account)
    except channels.ChannelError as e:
        raise HTTPException(422, friendly(e, "Adding this channel"))


@router.patch("/api/channels/{ch_id}")
def patch_channel(ch_id: str, body: ChannelPatch):
    import channels
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        return channels.update_channel(ch_id, fields)
    except channels.ChannelError as e:
        raise HTTPException(422, friendly(e, "Updating this channel"))


@router.delete("/api/channels/{ch_id}")
def remove_channel(ch_id: str):
    import channels
    try:
        channels.delete_channel(ch_id)
    except channels.ChannelError:
        raise HTTPException(404, "That channel doesn't exist anymore.")
    return {"deleted": ch_id}


@router.post("/api/channels/poll")
def poll_now():
    """Manual 'check channels now' — the same poll the hourly loop runs.
    Also kicks processing of anything newly pooled (on a worker thread so the
    request returns promptly with the poll result)."""
    import threading

    import channels
    try:
        result = channels.poll_all()
    except Exception as e:  # noqa: BLE001 — a poll failure must not 500 opaquely
        raise HTTPException(502, friendly(e, "Checking your channels"))

    def drain() -> None:
        try:
            while channels.process_next():
                pass
        except Exception as e:  # noqa: BLE001
            log.warning("pool processing failed: %s", e)

    threading.Thread(target=drain, name="channel-drain", daemon=True).start()
    return result


@router.get("/api/channels/{ch_id}/pool")
def channel_pool(ch_id: str):
    import channels
    store = channels.load_store()
    if ch_id not in store["channels"]:
        raise HTTPException(404, "That channel doesn't exist anymore.")
    pool = [{"video_id": vid, **e} for vid, e in store["pool"].items()
            if e["channel_id"] == ch_id]
    pool.sort(key=lambda e: e.get("added_at", ""), reverse=True)
    return {"pool": pool}
