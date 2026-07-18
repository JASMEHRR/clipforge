"""Approved channels & auto-pull.

`permission_source` (e.g. "clipping program", "creator DM 2026-07-01", a
program page link) and `credit_text` ("clips: @creator") are OPTIONAL but
recommended — both still work when filled (credit is appended to every clip
description). The old hard requirement was removed at PJ's request
(2026-07-15); only `paused` blocks auto-pull now.

Store: cache/channels.json (atomic tmp+replace, corrupt-file quarantine —
same conventions as upload_log.json):
  channels: {id: {url, name, permission_source, credit_text, paused,
                  default_preset, top_n, added_at, last_poll}}
  pool:     {video_id: {channel_id, title, url, views, source: top|new,
                        status: new|processing|processed|failed, added_at,
                        job_dir?, error?}}

Fetching uses yt-dlp flat playlists (keyless, no API quota). The hourly
background poll enqueues new pool entries and processes them sequentially
through pipeline.run_job with the channel's default editing preset. A
video_id is never processed twice: the pool entry (whatever its status)
is the dedupe record and is never deleted by a poll."""
from __future__ import annotations

import datetime as dt
import json
import threading
import time
import uuid
from pathlib import Path

from config import ROOT, apply_run_options, load_config
from errors import ClipForgeError
from logutil import get_logger

log = get_logger("channels")

STORE_PATH = ROOT / "cache" / "channels.json"
_store_lock = threading.Lock()
_poll_thread: threading.Thread | None = None


class ChannelError(ClipForgeError):
    stage = "channels"


# ============================================================
# Store
# ============================================================
def load_store() -> dict:
    if STORE_PATH.exists():
        try:
            data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
            data.setdefault("channels", {})
            data.setdefault("pool", {})
            return data
        except json.JSONDecodeError:
            backup = STORE_PATH.with_suffix(".json.corrupt")
            STORE_PATH.replace(backup)
            log.warning("%s was corrupt; moved to %s, starting fresh",
                        STORE_PATH.name, backup.name)
    return {"channels": {}, "pool": {}}


def save_store(data: dict) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(STORE_PATH)


# ============================================================
# Channel CRUD
# ============================================================
def can_auto_pull(ch: dict) -> bool:
    """Only `paused` blocks auto-pull (permission_source is optional now)."""
    return not ch.get("paused", False)


def add_channel(url: str, permission_source: str = "", credit_text: str = "",
                name: str = "", default_preset: str = "",
                top_n: int | None = None, account: str = "default") -> dict:
    """Register a channel. `permission_source` and `credit_text` are optional
    but recommended; when set, credit_text is appended to every clip
    description made from this channel. `account` is the destination YouTube
    channel (upload account) that clips from this source get posted to."""
    url = (url or "").strip().rstrip("/")
    if not url.startswith("http"):
        raise ChannelError("channel URL must be a link (https://...)")
    cfg = load_config()
    with _store_lock:
        store = load_store()
        if any(c["url"].lower() == url.lower()
               for c in store["channels"].values()):
            raise ChannelError("that channel is already added")
        ch_id = uuid.uuid4().hex[:12]
        ch = {
            "id": ch_id,
            "url": url,
            "name": (name or "").strip() or url.rsplit("/", 1)[-1],
            "permission_source": (permission_source or "").strip(),
            "credit_text": (credit_text or "").strip(),
            "paused": False,
            "default_preset": (default_preset or "").strip(),
            "account": (account or "default").strip() or "default",
            "top_n": int(top_n or cfg.get("channels", {}).get(
                "top_n_default", 10)),
            "added_at": dt.datetime.now().isoformat(timespec="seconds"),
            "last_poll": None,
        }
        store["channels"][ch_id] = ch
        save_store(store)
    log.info("channel added: %s (%s)", ch["name"], url)
    return ch


def update_channel(ch_id: str, fields: dict) -> dict:
    """Patch editable fields (permission_source/credit_text may be blanked —
    they are optional)."""
    editable = {"name", "permission_source", "credit_text", "paused",
                "default_preset", "top_n", "account"}
    with _store_lock:
        store = load_store()
        ch = store["channels"].get(ch_id)
        if ch is None:
            raise ChannelError(f"unknown channel '{ch_id}'")
        for k, v in fields.items():
            if k not in editable:
                continue
            ch[k] = int(v) if k == "top_n" else (
                bool(v) if k == "paused" else str(v).strip())
        ch.setdefault("account", "default")
        save_store(store)
    return ch


def reassign_account(old_account: str, new_account: str = "default") -> int:
    """Point every channel currently uploading to `old_account` at
    `new_account` instead. Used when a destination account is removed so its
    source channels don't silently stop uploading. Returns how many changed."""
    changed = 0
    with _store_lock:
        store = load_store()
        for ch in store["channels"].values():
            if ch.get("account", "default") == old_account:
                ch["account"] = new_account
                changed += 1
        if changed:
            save_store(store)
    return changed


def delete_channel(ch_id: str) -> None:
    """Remove a channel. Its pool entries are kept — they are the dedupe
    memory that stops a re-added channel from re-processing old videos."""
    with _store_lock:
        store = load_store()
        if ch_id not in store["channels"]:
            raise ChannelError(f"unknown channel '{ch_id}'")
        del store["channels"][ch_id]
        save_store(store)


# ============================================================
# Fetching (yt-dlp flat playlists — keyless, quota-free)
# ============================================================
def _fetch_entries(url: str, limit: int) -> list[dict]:
    """Latest/top videos of a channel as flat entries
    [{id, title, url, view_count}]. Separate function = the mock seam for
    tests. Uses the channel's /videos tab (newest first)."""
    try:
        import yt_dlp
    except ImportError as e:
        raise ChannelError("yt-dlp not installed", detail=str(e)) from e
    tab = url if url.rstrip("/").endswith("/videos") else url + "/videos"
    opts = {"extract_flat": "in_playlist", "playlistend": int(limit),
            "quiet": True, "noprogress": True, "skip_download": True}
    import ingest
    opts.update(ingest.ytdlp_network_opts())  # cookies/rate-limit from config
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(tab, download=False)
    except Exception as e:  # noqa: BLE001 — yt-dlp raises many types
        raise ChannelError(f"could not list channel videos: {url}",
                           detail=str(e)[:300]) from e
    out = []
    for e in (info or {}).get("entries") or []:
        if not e or not e.get("id"):
            continue
        out.append({"id": e["id"], "title": e.get("title") or "",
                    "url": e.get("url") or
                    f"https://www.youtube.com/watch?v={e['id']}",
                    "view_count": e.get("view_count")})
    return out


def poll_channel(ch: dict, store: dict, cfg: dict) -> int:
    """Fetch one channel's top-N (by view count, where flat extraction
    provides it — otherwise newest-first order stands in) plus its latest
    uploads, and add anything unseen to the pool. Returns how many videos
    were added. Mutates `store` in place; the caller saves."""
    top_n = int(ch.get("top_n") or cfg.get("channels", {}).get(
        "top_n_default", 10))
    fetch_n = max(top_n * 3, 30)
    entries = _fetch_entries(ch["url"], fetch_n)

    with_views = [e for e in entries if e.get("view_count") is not None]
    ranked = sorted(with_views, key=lambda e: e["view_count"], reverse=True) \
        if with_views else entries
    top = {e["id"] for e in ranked[:top_n]}
    # "new uploads" = ids never fetched on ANY earlier poll (tracked in the
    # channel's `seen` list). The first poll counts as backfill — everything
    # goes into `seen` but only the top-N enter the pool, so adding a channel
    # doesn't dump its whole recent history into the queue.
    seen = set(ch.get("seen", []))
    first_poll = not ch.get("last_poll")

    added = 0
    now = dt.datetime.now().isoformat(timespec="seconds")
    for e in entries:
        if e["id"] in top:
            source = "top"
        elif not first_poll and e["id"] not in seen:
            source = "new"
        else:
            source = None
        if source is None or e["id"] in store["pool"]:
            continue
        store["pool"][e["id"]] = {
            "channel_id": ch["id"], "title": e["title"], "url": e["url"],
            "views": e.get("view_count"), "source": source,
            "status": "new", "added_at": now,
        }
        added += 1
    # remember everything fetched (bounded) so the next poll can tell new from old
    ch["seen"] = list(dict.fromkeys(
        list(seen) + [e["id"] for e in entries]))[-500:]
    ch["last_poll"] = now
    return added


def poll_all(cfg: dict | None = None) -> dict:
    """Poll every eligible channel (permission gate + not paused). Per-channel
    failures are recorded and never abort the rest."""
    cfg = cfg or load_config()
    results: dict = {"added": 0, "errors": {}}
    with _store_lock:
        store = load_store()
        for ch in store["channels"].values():
            if not can_auto_pull(ch):
                continue
            try:
                results["added"] += poll_channel(ch, store, cfg)
            except ChannelError as e:
                results["errors"][ch["id"]] = str(e)
                log.warning("poll failed for %s: %s", ch["name"], e)
        save_store(store)
    if results["added"]:
        log.info("channel poll: %d new video(s) pooled", results["added"])
    return results


# ============================================================
# Processing (pool → pipeline, sequential)
# ============================================================
def _claim_next() -> tuple[dict, dict] | None:
    """Atomically claim the oldest 'new' pool entry (newest uploads first,
    then top performers — the queue priority rule). Marks it 'processing'."""
    with _store_lock:
        store = load_store()
        pending = [(vid, e) for vid, e in store["pool"].items()
                   if e["status"] == "new"
                   and not store["channels"].get(e["channel_id"], {}).get("paused", True)]
        if not pending:
            return None
        pending.sort(key=lambda x: (x[1]["source"] != "new", x[1]["added_at"]))
        vid, entry = pending[0]
        entry["status"] = "processing"
        save_store(store)
        return vid, {**entry, "video_id": vid}


def _finish(vid: str, status: str, job_dir: str = "", error: str = "") -> None:
    with _store_lock:
        store = load_store()
        if vid in store["pool"]:
            store["pool"][vid].update({"status": status, "job_dir": job_dir,
                                       "error": error[:300]})
            save_store(store)


def process_next(cfg: dict | None = None) -> bool:
    """Process ONE pooled video end-to-end (never parallel renders): expand
    the channel's default editing preset, run the pipeline, append the
    channel's credit text to every clip description. Returns False when the
    pool has nothing pending."""
    claimed = _claim_next()
    if claimed is None:
        return False
    vid, entry = claimed
    cfg = cfg or load_config()
    store = load_store()
    ch = store["channels"].get(entry["channel_id"], {})
    try:
        import pipeline
        opts: dict = {}
        preset_name = ch.get("default_preset") or ""
        if preset_name:
            import presets
            try:
                opts = presets.expand(presets.load_preset(preset_name))
            except Exception as e:  # noqa: BLE001 — a broken preset falls back to defaults
                log.warning("channel %s preset '%s' unusable (%s); using "
                            "defaults", ch.get("name"), preset_name, e)
        opts["credit_text"] = ch.get("credit_text", "")
        run_cfg = apply_run_options(cfg, opts)
        # rendered clips enter the posting queue as channel content, at the
        # channel's destination account, with new uploads ahead of top hits
        run_cfg.setdefault("upload", {})["queue_source"] = \
            "channel_new" if entry.get("source") == "new" else "channel_top"
        run_cfg["upload"]["queue_account"] = ch.get("account", "default")
        log.info("auto-pull processing %s (%s) from %s", vid,
                 entry.get("title", "")[:60], ch.get("name"))
        job = pipeline.run_job(
            entry["url"], run_cfg,
            preset=opts.get("preset"),
            aspect=opts.get("aspect", "9:16"),
            music=opts.get("music"),
            music_volume_db=float(opts.get("music_volume_db", -18.0)),
            edit_preset=preset_name or None)
        _finish(vid, "processed", job_dir=str(job.get("job_dir", "")))
    except Exception as e:  # noqa: BLE001 — one bad video never stalls the pool
        log.warning("auto-pull failed for %s: %s", vid, e)
        _finish(vid, "failed", error=str(e))
    return True


# ============================================================
# Background poll loop
# ============================================================
def start_background_poll() -> None:
    """Hourly channel poll + sequential processing, mirroring
    analytics.start_background_refresh. Failures never kill the loop."""
    global _poll_thread
    if _poll_thread is not None and _poll_thread.is_alive():
        return

    def loop() -> None:
        while True:
            try:
                cfg = load_config()
                interval = int(cfg.get("channels", {}).get(
                    "poll_interval_s", 3600))
                if any(can_auto_pull(c)
                       for c in load_store()["channels"].values()):
                    poll_all(cfg)
                    while process_next(cfg):
                        pass
            except Exception as e:  # noqa: BLE001 — the loop must survive anything
                log.warning("channel poll loop error (will retry): %s", e)
                interval = 3600
            time.sleep(interval)

    _poll_thread = threading.Thread(target=loop, name="channel-poll",
                                    daemon=True)
    _poll_thread.start()
    log.info("channel auto-pull poll started")


# ============================================================
# Stats (dashboard)
# ============================================================
def channel_stats(store: dict | None = None) -> list[dict]:
    """Per-channel pool counts for the dashboard, pure given a store."""
    store = store or load_store()
    stats = []
    for ch in store["channels"].values():
        pool = [e for e in store["pool"].values()
                if e["channel_id"] == ch["id"]]
        stats.append({
            "id": ch["id"], "name": ch["name"], "url": ch["url"],
            "paused": ch.get("paused", False),
            "permission_source": ch.get("permission_source", ""),
            "default_preset": ch.get("default_preset", ""),
            "account": ch.get("account", "default"),
            "last_poll": ch.get("last_poll"),
            "videos_pulled": len(pool),
            "pending": sum(1 for e in pool if e["status"] == "new"),
            "processed": sum(1 for e in pool if e["status"] == "processed"),
            "failed": sum(1 for e in pool if e["status"] == "failed"),
        })
    return stats
