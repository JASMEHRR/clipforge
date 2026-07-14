"""YouTube Analytics fetch + cache layer. Auth and the raw API client live in
youtube_upload.py (same split as upload_scheduler.py vs youtube_upload.py);
this module only fetches, caches, and joins. All recommendation math lives in
analytics_insights.py — kept separate so it stays pure and mockable-free.

Read-only: this module never writes to YouTube, only reads analytics data and
caches it locally to avoid hammering the Analytics API on every tab visit."""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone

import analytics_insights
import publish_timing
import upload_scheduler
import youtube_upload
from config import ROOT, load_config
from errors import UploadError
from logutil import get_logger

log = get_logger("analytics")

CACHE_FILE = ROOT / "cache" / "analytics_cache.json"
IST = timezone(timedelta(hours=5, minutes=30))
TTL_S = 20 * 3600
REFRESH_INTERVAL_S = 24 * 3600
_VIDEO_METRICS = "views,estimatedMinutesWatched,averageViewPercentage,likes,subscribersGained"

_refresh_lock = threading.Lock()


def load_cache() -> dict | None:
    if not CACHE_FILE.exists():
        return None
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        backup = CACHE_FILE.with_suffix(".json.corrupt")
        CACHE_FILE.rename(backup)
        log.warning("analytics_cache.json was corrupt; moved to %s, starting fresh",
                   backup.name)
        return None


def save_cache(data: dict) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(CACHE_FILE)  # atomic on same drive; no half-written cache


def _fetch_overview(analytics, days: int) -> dict:
    """Channel totals over the trailing `days` days: no `dimensions`, so the
    API returns a single row of channel-wide sums/averages."""
    end = datetime.now(IST).date()
    start = end - timedelta(days=days)
    res = analytics.reports().query(
        ids="channel==MINE", startDate=str(start), endDate=str(end),
        metrics=_VIDEO_METRICS,
    ).execute()
    rows = res.get("rows") or []
    if not rows:
        return {"views": 0, "watch_minutes": 0, "avg_view_pct": 0.0, "subs_gained": 0}
    views, minutes, avg_pct, likes, subs = rows[0]
    return {"views": int(views), "watch_minutes": round(float(minutes)),
           "avg_view_pct": round(float(avg_pct), 1), "subs_gained": int(subs)}


def _fetch_video_rows(analytics, days: int = 90, max_results: int = 200) -> list[tuple]:
    """Per-video rows over the trailing `days` days — same query shape as
    upload_scheduler.report(), with a higher maxResults for the full table."""
    end = datetime.now(IST).date()
    start = end - timedelta(days=days)
    res = analytics.reports().query(
        ids="channel==MINE", startDate=str(start), endDate=str(end),
        metrics=_VIDEO_METRICS, dimensions="video", sort="-views",
        maxResults=max_results,
    ).execute()
    return res.get("rows") or []


def _clip_extra(key: str) -> dict:
    """Best-effort re-read of a clip's own metadata.json for fields the
    upload log doesn't carry (duration_s, source_name). Returns {} if the
    clip folder or metadata has since been deleted/moved."""
    meta_path = ROOT / key / "metadata.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    start_s = meta.get("original_source_start_s")
    end_s = meta.get("original_source_end_s")
    duration = (end_s - start_s) if start_s is not None and end_s is not None else None
    return {"duration_s": duration, "source_name": meta.get("source_name")}


def refresh(force: bool = False) -> dict:
    """Cache-or-fetch. Returns {fetched_at, fetched_at_epoch, overview, videos}.
    Raises UploadError on a real fetch failure (never on a cache hit)."""
    with _refresh_lock:
        cached = load_cache()
        fresh = (cached is not None and "fetched_at_epoch" in cached
                and (time.time() - cached["fetched_at_epoch"]) < TTL_S)
        if not force and fresh:
            return cached
        try:
            analytics = youtube_upload.build_analytics_service()
            log_data = upload_scheduler.load_log()
            overview = {"28d": _fetch_overview(analytics, 28),
                       "90d": _fetch_overview(analytics, 90)}
            rows = _fetch_video_rows(analytics)
            videos = analytics_insights.join_rows(rows, log_data,
                                                  clip_extra_fn=_clip_extra)
        except Exception as e:  # noqa: BLE001 — classify as a friendly upload-style error
            if cached is not None:
                log.warning("analytics refresh failed (%s); serving stale cache", e)
                return cached
            raise UploadError(f"could not fetch analytics: {e}") from e
        # Piggyback the publish-time learning collection + daily tweak loop on
        # this same daily/on-open refresh cycle (Parts 1 & 3's "daily job") —
        # best-effort and isolated from the main refresh above: a failure here
        # must never turn a successful analytics fetch into a stale response.
        try:
            publish_timing.refresh_stats(analytics)
            publish_timing.recompute_ranking(load_config(), log_data)
        except Exception as e:  # noqa: BLE001 — never let this block analytics
            log.warning("publish-timing refresh/recompute failed (%s); continuing", e)
        data = {"fetched_at": datetime.now(IST).isoformat(),
               "fetched_at_epoch": time.time(),
               "overview": overview, "videos": videos}
        save_cache(data)
        return data


def start_background_refresh() -> None:
    """Daemon thread: refresh once a day, forever. Never raises out — a
    failed background refresh just leaves the existing cache in place and
    retries on the next interval. Started once from server lifespan."""
    def loop() -> None:
        while True:
            try:
                if youtube_upload.authorized():
                    refresh(force=True)
            except Exception as e:  # noqa: BLE001 — background job must never die
                log.warning("background analytics refresh failed: %s", e)
            time.sleep(REFRESH_INTERVAL_S)

    threading.Thread(target=loop, daemon=True).start()
