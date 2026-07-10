"""Scheduling layer on top of youtube_upload.py: dedupe, virality gating,
publish-slot picking, daily/run caps, notifications. Auth and the actual
API call live in youtube_upload.py — this module never touches OAuth.

Ported from the standalone auto_upload.py (behavior preserved); only the
auth/upload primitives were swapped for youtube_upload.py's shared ones."""
from __future__ import annotations

import json
import re
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import ROOT
from errors import UploadError, UploadQuotaError
from logutil import get_logger

import youtube_upload

log = get_logger("upload")

OUTPUT_DIR = ROOT / "output"
LOG_FILE = ROOT / "cache" / "upload_log.json"

IST = timezone(timedelta(hours=5, minutes=30))
MIN_VIEWS_FOR_ANALYTICS = 500

JUNK_TAGS = {
    "know", "next", "thing", "things", "after", "before", "important",
    "really", "actually", "very", "just", "like", "want", "need", "make",
    "made", "good", "best", "this", "that", "what", "when", "where", "how",
    "why", "who", "will", "would", "could", "should", "reels", "tiktok",
}


# ============================================================
# Upload log (memory of what's already uploaded)
# ============================================================
def load_log() -> dict:
    if LOG_FILE.exists():
        try:
            return json.loads(LOG_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            backup = LOG_FILE.with_suffix(".json.corrupt")
            LOG_FILE.rename(backup)
            log.warning("upload_log.json was corrupt; moved to %s, starting fresh",
                       backup.name)
    return {"uploads": {}}


def save_log(log_data: dict) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = LOG_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(log_data, indent=2), encoding="utf-8")
    tmp.replace(LOG_FILE)  # atomic on same drive; no half-written logs


def uploads_today(log_data: dict) -> int:
    today = datetime.now(IST).date().isoformat()
    return sum(
        1 for v in log_data["uploads"].values()
        if (v.get("uploaded_at") or "").startswith(today)
    )


# ============================================================
# Notifications
# ============================================================
def notify(title: str, message: str, ntfy_topic: str = "") -> None:
    """Send a push notification to the phone. Silently skips if not
    configured; never lets a notification failure break an upload."""
    log.info("[NOTIFY] %s: %s", title, message)
    if not ntfy_topic:
        return
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{ntfy_topic}",
            data=message.encode("utf-8"),
            headers={"Title": title.encode("ascii", "ignore").decode()},
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:  # noqa: BLE001 — notification failure must not break upload
        log.warning("notification failed (%s); upload unaffected", e)


def is_settled(video: Path, settle_seconds: int = 90) -> bool:
    """True only if final.mp4 hasn't been modified for settle_seconds.
    Used by `watch` mode, which doesn't get a definitive completion signal
    like the pipeline hook does."""
    try:
        age = time.time() - video.stat().st_mtime
    except OSError:
        return False
    return age >= settle_seconds


# ============================================================
# Clip discovery
# ============================================================
def find_candidates(cfg: dict, log_data: dict) -> list[dict]:
    """All not-yet-uploaded clips with final.mp4 + metadata, best virality first."""
    upload_cfg = cfg.get("upload", {})
    min_virality = upload_cfg.get("min_virality", 40)
    candidates = []
    if not OUTPUT_DIR.exists():
        return candidates

    for meta_path in OUTPUT_DIR.glob("*/clip_*/metadata.json"):
        clip_dir = meta_path.parent
        video = clip_dir / "final.mp4"
        if not video.exists():
            continue
        key = str(clip_dir.relative_to(ROOT)).replace("\\", "/")
        if key in log_data["uploads"]:
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            log.warning("skip %s: metadata unreadable (%s)", key, e)
            continue
        if meta.get("upload", {}).get("exclude"):
            continue  # user opted this clip out of auto-upload
        score = meta.get("virality", {}).get("score", 0)
        if score < min_virality:
            continue
        candidates.append({"key": key, "dir": clip_dir, "video": video,
                           "meta": meta, "score": score})

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates


# ============================================================
# Title / description / hashtags
# ============================================================
def clean_hashtags(raw: list, max_hashtags: int = 5) -> list[str]:
    tags = []
    for t in raw or []:
        word = t.lstrip("#").strip().lower()
        if not word or word in JUNK_TAGS or len(word) < 3:
            continue
        if not re.fullmatch(r"[a-z0-9]+", word):
            continue
        if word not in tags:
            tags.append(word)
        if len(tags) >= max_hashtags:
            break
    if "shorts" not in tags:
        tags.append("shorts")
    return ["#" + t for t in tags]


def build_snippet(meta: dict) -> dict:
    """Clean title/description/hashtags from ClipMetadata, ready to merge
    into youtube_upload.build_request_body's metadata argument."""
    title = (meta.get("title") or "Untitled Short").strip()
    if len(title) > 90:  # keep headroom under YouTube's 100-char limit
        title = title[:87] + "..."
    hashtags = clean_hashtags(meta.get("hashtags"))
    return {"title": title, "description": (meta.get("description") or "").strip(),
            "hashtags": hashtags}


# ============================================================
# Publish-time selection
# ============================================================
def get_peak_hours(analytics) -> list[int] | None:
    """Ask YouTube Analytics for total views; if the channel has enough
    data, this would derive peak hours, but the Analytics API doesn't expose
    hour-of-day, so it just gates the fallback on whether there's enough
    data to bother trying later. Returns None (use default slots) today."""
    try:
        end = datetime.now(IST).date()
        start = end - timedelta(days=28)
        totals = analytics.reports().query(
            ids="channel==MINE",
            startDate=str(start), endDate=str(end),
            metrics="views",
        ).execute()
        rows = totals.get("rows") or []
        total_views = int(rows[0][0]) if rows else 0
        if total_views < MIN_VIEWS_FOR_ANALYTICS:
            log.info("channel has %d views in last 28 days (< %d); using default slots",
                     total_views, MIN_VIEWS_FOR_ANALYTICS)
        return None
    except Exception as e:  # noqa: BLE001 — analytics is optional context
        log.info("analytics unavailable (%s); using default slots", e)
        return None


def next_publish_times(count: int, analytics, log_data: dict,
                       default_slots: list[int]) -> list[datetime]:
    """Pick the next `count` free publish slots, never in the past,
    never colliding with already-scheduled videos."""
    import random
    hours = get_peak_hours(analytics) or default_slots

    taken = set()
    for entry in log_data["uploads"].values():
        t = entry.get("publish_at")
        if t:
            taken.add(t[:13])  # date+hour granularity

    times = []
    now = datetime.now(IST)
    day = now.date()
    while len(times) < count:
        for h in sorted(hours):
            slot = datetime(day.year, day.month, day.day, h,
                            random.randint(0, 14), 0, tzinfo=IST)
            if slot <= now + timedelta(minutes=30):
                continue
            if slot.isoformat()[:13] in taken:
                continue
            times.append(slot)
            taken.add(slot.isoformat()[:13])
            if len(times) == count:
                break
        day += timedelta(days=1)
    return times


# ============================================================
# UI panel snapshot
# ============================================================
def panel_state(cfg: dict, log_data: dict, authorized: bool) -> dict:
    """Everything the UI's auto-upload panel shows, as plain data. Pure given
    its arguments (no I/O); the caller supplies config, log and auth state."""
    upload_cfg = cfg.get("upload", {})
    today = uploads_today(log_data)
    max_day = upload_cfg.get("max_per_day", 3)
    next_slot = None
    if authorized and upload_cfg.get("auto_enabled") and today < max_day:
        slots = next_publish_times(
            1, None, log_data, upload_cfg.get("publish_slots_ist", [12, 19]))
        next_slot = slots[0].isoformat() if slots else None
    recent = sorted(log_data["uploads"].values(),
                    key=lambda e: e.get("uploaded_at", ""), reverse=True)[:5]
    return {
        "auto_enabled": bool(upload_cfg.get("auto_enabled", False)),
        "authorized": bool(authorized),
        "uploads_today": today,
        "max_per_day": max_day,
        "next_slot_ist": next_slot,
        "recent": [{"title": e.get("title", ""),
                    "video_id": e.get("video_id", ""),
                    "url": f"https://youtu.be/{e.get('video_id', '')}",
                    "publish_at": e.get("publish_at", "")} for e in recent],
    }


# ============================================================
# Upload
# ============================================================
def upload_one(youtube, clip: dict, publish_at: datetime, category_id: str,
               service=None) -> dict:
    snippet = build_snippet(clip["meta"])
    publish_at_iso = publish_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    log.info("uploading %s -> '%s' (publishes %s)", clip["key"], snippet["title"],
             publish_at.strftime("%d %b %H:%M IST"))
    return youtube_upload.upload_clip(
        clip["video"], snippet, privacy="private", service=service or youtube,
        publish_at=publish_at_iso, category_id=category_id)


def upload_batch(youtube, analytics, cfg: dict, log_data: dict, limit: int) -> int:
    """Upload up to `limit` eligible, fully-rendered clips.
    Returns how many were uploaded. Safe to call repeatedly."""
    upload_cfg = cfg.get("upload", {})
    if limit <= 0:
        return 0
    candidates = find_candidates(cfg, log_data)
    if not candidates:
        return 0

    batch = candidates[:limit]
    times = next_publish_times(len(batch), analytics, log_data,
                               upload_cfg.get("publish_slots_ist", [12, 19]))
    category_id = upload_cfg.get("category_id", "22")
    ntfy_topic = upload_cfg.get("ntfy_topic", "")

    done = 0
    for clip, publish_at in zip(batch, times):
        try:
            result = upload_one(youtube, clip, publish_at, category_id)
        except UploadError as e:
            log.error("upload failed for %s: %s", clip["key"], e)
            notify("ClipForge upload FAILED", f"{clip['key']}: {e}\nWill retry next cycle.",
                  ntfy_topic)
            break
        log_data["uploads"][clip["key"]] = {
            "video_id": result["video_id"],
            "uploaded_at": datetime.now(IST).isoformat(),
            "publish_at": publish_at.isoformat(),
            "title": build_snippet(clip["meta"])["title"],
            "virality_score": clip["score"],
        }
        save_log(log_data)
        done += 1
        notify(
            "Short scheduled",
            f"'{build_snippet(clip['meta'])['title']}' (virality {clip['score']}) "
            f"publishes {publish_at.strftime('%d %b, %I:%M %p IST')}\n{result['url']}",
            ntfy_topic,
        )
    return done


# ============================================================
# Pipeline hook — the primary, event-driven path
# ============================================================
def trigger_after_render(clip_dir: Path, cfg: dict) -> None:
    """Called right after a clip's final.mp4 + metadata.json are finalized.
    No-ops unless upload.auto_enabled and YouTube is authorized. Never raises
    into the caller — a failed/unqualified upload must never fail a clip."""
    upload_cfg = cfg.get("upload", {})
    if not upload_cfg.get("auto_enabled", False):
        return
    if not youtube_upload.credentials_available() or not youtube_upload.has_cached_token():
        log.info("auto-upload enabled but not authorized yet; skipping %s", clip_dir.name)
        return
    try:
        log_data = load_log()
        remaining = upload_cfg.get("max_per_day", 3) - uploads_today(log_data)
        if remaining <= 0:
            log.info("daily upload cap reached; %s waits for tomorrow", clip_dir.name)
            return
        youtube = youtube_upload.build_service()
        analytics = youtube_upload.build_analytics_service()
        upload_batch(youtube, analytics, cfg, log_data, limit=1)
    except Exception as e:  # noqa: BLE001 — upload is never allowed to kill a render
        log.warning("auto-upload trigger failed for %s: %s", clip_dir.name, e)


# ============================================================
# Report mode
# ============================================================
def report(analytics, log_data: dict) -> str:
    end = datetime.now(IST).date()
    start = end - timedelta(days=28)
    try:
        res = analytics.reports().query(
            ids="channel==MINE",
            startDate=str(start), endDate=str(end),
            metrics="views,estimatedMinutesWatched,averageViewPercentage,likes,subscribersGained",
            dimensions="video",
            sort="-views",
            maxResults=25,
        ).execute()
    except Exception as e:
        raise UploadError(f"could not fetch analytics: {e}") from e

    rows = res.get("rows") or []
    if not rows:
        return ("No analytics data yet — channel is too new. "
                "Check back after your first uploads have been live a few days.")

    id_to_key = {v.get("video_id"): k for k, v in log_data["uploads"].items()
                 if v.get("video_id")}

    lines = [f"=== Last 28 days ({start} to {end}) ===\n",
             f"{'Video':<40} {'Views':>7} {'AvgWatch%':>10} {'Likes':>6} {'Subs+':>6}"]
    for vid, views, mins, avg_pct, likes, subs in rows:
        label = id_to_key.get(vid, vid)[:38]
        lines.append(f"{label:<40} {views:>7} {avg_pct:>9.1f}% {likes:>6} {subs:>6}")

    views_list = [r[1] for r in rows]
    avg_list = [r[3] for r in rows]
    lines.append("\n=== Recommendations ===")
    if max(avg_list) - min(avg_list) > 20:
        lines.append("- Big retention spread between clips: compare your best and worst "
                     "avg-watch% clips' hooks — the difference is almost always the first 2 seconds.")
    if len(views_list) >= 5 and views_list[0] > 5 * (sum(views_list[1:]) / max(len(views_list) - 1, 1)):
        lines.append("- One clip is massively outperforming: make 3-5 more on that exact "
                     "topic/format while it's hot.")
    lines.append("- Clips with avg watch % under 40 are being swiped away: raise "
                 "min_virality or tighten hooks.")
    return "\n".join(lines)


# ============================================================
# Watch mode (polling fallback)
# ============================================================
def watch(cfg: dict) -> None:
    """Full-auto fallback: scan on an interval, upload new clips, respect
    the daily cap. Primary path is the pipeline hook (trigger_after_render);
    use this when clips were rendered by a run that predates this feature or
    while ClipForge wasn't running."""
    upload_cfg = cfg.get("upload", {})
    interval = upload_cfg.get("watch_interval_s", 60)
    max_per_day = upload_cfg.get("max_per_day", 3)
    ntfy_topic = upload_cfg.get("ntfy_topic", "")

    youtube = youtube_upload.build_service()
    analytics = youtube_upload.build_analytics_service()
    log.info("watching %s every %ds (daily cap: %d)", OUTPUT_DIR, interval, max_per_day)
    notify("ClipForge watcher started", f"Auto-upload is live. Cap {max_per_day}/day.", ntfy_topic)

    announced_cap = False
    while True:
        try:
            log_data = load_log()
            remaining = max_per_day - uploads_today(log_data)
            if remaining > 0:
                announced_cap = False
                n = upload_batch(youtube, analytics, cfg, log_data, limit=remaining)
                if n:
                    log.info("uploaded %d clip(s); %d left today", n,
                            max_per_day - uploads_today(log_data))
            elif not announced_cap:
                log.info("daily cap (%d) reached; resuming tomorrow", max_per_day)
                announced_cap = True
        except KeyboardInterrupt:
            log.info("watcher stopped")
            return
        except Exception as e:  # noqa: BLE001 — network blip, expired token, etc.
            log.warning("watcher error (will retry): %s", e)
            notify("ClipForge watcher error", f"{e}\nRetrying in {interval}s.", ntfy_topic)
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            log.info("watcher stopped")
            return
