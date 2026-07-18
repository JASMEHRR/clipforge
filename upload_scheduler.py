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
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ffutil
from config import ROOT, load_config
from errors import UploadError, UploadQuotaError
from logutil import get_logger

import youtube_upload

log = get_logger("upload")

OUTPUT_DIR = ROOT / "output"
LOG_FILE = ROOT / "cache" / "upload_log.json"
QUEUE_FILE = ROOT / "cache" / "post_queue.json"


def _output_dir() -> Path:
    """The current workspace's output dir. A test/caller that monkeypatches
    module-level OUTPUT_DIR away from its built-in default always wins (an
    explicit override means "use exactly this directory"); otherwise this
    re-reads load_config() so each workspace's own output/_workspaces/<id>/
    folder is what gets scanned for candidates."""
    if OUTPUT_DIR != ROOT / "output":
        return OUTPUT_DIR
    return ROOT / load_config()["paths"]["output_dir"]


def scoped_log(log_data: dict, prefix: str) -> dict:
    """Read-only view of the upload log limited to clip keys under `prefix`
    (a workspace's output_dir, e.g. "output/_workspaces/cooking-channel/") —
    since every key already encodes its workspace's path, this is enough to
    keep one workspace's upload history out of another's. Never pass the
    result to save_log — mutating callers must use the full load_log()."""
    return {"uploads": {k: v for k, v in log_data.get("uploads", {}).items()
                        if k.startswith(prefix)}}

# posting-queue priority: newest uploads from approved channels first, then
# their top performers, then manually queued clips
_SOURCE_RANK = {"channel_new": 0, "channel_top": 1, "manual": 2}

IST = timezone(timedelta(hours=5, minutes=30))
MIN_VIEWS_FOR_ANALYTICS = 500

# YouTube Data API daily quota. A resumable video insert costs ~1600 units, so
# ~6 uploads/day is the hard ceiling regardless of how many slots exist.
QUOTA_DAILY_UNITS = 10000
QUOTA_PER_UPLOAD = 1600



# ============================================================
# Upload log (memory of what's already uploaded)
# ============================================================
def _log_file() -> Path:
    """Active log path. Dry-run keeps a SEPARATE log so simulated uploads never
    pollute the real one's dedupe/quota state — flip CLIPFORGE_DRY_RUN off and
    the real history is exactly as it was."""
    if youtube_upload.dry_run():
        return LOG_FILE.with_name(LOG_FILE.stem + ".dryrun.json")
    return LOG_FILE


def load_log() -> dict:
    path = _log_file()
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            backup = path.with_suffix(".json.corrupt")
            path.rename(backup)
            log.warning("%s was corrupt; moved to %s, starting fresh",
                       path.name, backup.name)
    return {"uploads": {}}


def save_log(log_data: dict) -> None:
    path = _log_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(log_data, indent=2), encoding="utf-8")
    tmp.replace(path)  # atomic on same drive; no half-written logs


def uploads_today(log_data: dict, account: str | None = None) -> int:
    """Uploads that went out today, optionally for one destination account
    (log entries without an `account` field are legacy = 'default')."""
    today = datetime.now(IST).date().isoformat()
    return sum(
        1 for v in log_data["uploads"].values()
        if (v.get("uploaded_at") or "").startswith(today)
        and (account is None or v.get("account", "default") == account)
    )


# ============================================================
# Destination accounts (multi-account posting)
# ============================================================
def list_accounts(cfg: dict) -> list[str]:
    """Configured destination accounts; a config without an accounts block is
    the legacy single-account setup ('default')."""
    accounts = cfg.get("upload", {}).get("accounts") or {}
    return sorted(accounts.keys()) if accounts else ["default"]


def account_cfg(cfg: dict, account: str = "default") -> dict:
    """Effective per-account settings with back-compat: account-level values
    win, flat upload.* keys are the fallback (so pre-multi-account configs
    behave identically)."""
    up = cfg.get("upload", {})
    acc = (up.get("accounts") or {}).get(account) or {}
    avatar_host = bool(acc.get("avatar_host"))
    # avatar-host cadence guard: 1/day unless the account sets max_per_day
    # explicitly (an explicit value always wins = the documented override)
    if avatar_host and "max_per_day" not in acc:
        max_per_day = 1
    else:
        max_per_day = int(acc.get("max_per_day", up.get("max_per_day", 20)))
    return {
        "max_per_day": max_per_day,
        "publish_slots_ist": list(acc.get("publish_slots_ist",
                                          up.get("publish_slots_ist",
                                                 [12, 19]))),
        "slot_spacing_minutes": int(acc.get("slot_spacing_minutes",
                                            up.get("slot_spacing_minutes",
                                                   60))),
        "avatar_host": avatar_host,
        "auto_enabled": bool(acc.get("auto_enabled", up.get("auto_enabled", False))),
    }


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
def _scan_clips(cfg: dict, log_data: dict) -> list[dict]:
    """All not-yet-uploaded clips with final.mp4 + readable metadata, above
    min_virality and not user-excluded, best virality first — the shared scan
    behind the upload queue (find_candidates) and the approvals view
    (find_pending_approval)."""
    upload_cfg = cfg.get("upload", {})
    min_virality = upload_cfg.get("min_virality", 40)
    clips = []
    out_dir = _output_dir()
    if not out_dir.exists():
        return clips

    for meta_path in out_dir.glob("*/clip_*/metadata.json"):
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
        clips.append({"key": key, "dir": clip_dir, "video": video,
                      "meta": meta, "score": score})

    clips.sort(key=lambda c: c["score"], reverse=True)
    return clips


def approval_state(meta: dict) -> str:
    """'approved' | 'rejected' | 'pending' (an absent field means pending —
    every clip produced before the approval feature reads as pending)."""
    return meta.get("upload", {}).get("approval") or "pending"


def approval_ok(meta: dict, cfg: dict) -> bool:
    """Whether the owner allows this clip to upload. Rejected clips never
    upload. With upload.require_approval on, only explicitly approved clips
    may (pending = not reviewed yet); with it off, pending clips stay
    eligible — the pre-approval fully-automatic behavior."""
    state = approval_state(meta)
    if state == "rejected":
        return False
    if cfg.get("upload", {}).get("require_approval", False):
        return state == "approved"
    return True


def find_candidates(cfg: dict, log_data: dict) -> list[dict]:
    """All uploadable clips (scanned + approval-gated), best virality first,
    near-duplicates collapsed. Every upload path — trigger_after_render,
    watch, the CLI and the UI queue — selects through here, so the approval
    gate holds everywhere by construction."""
    candidates = [c for c in _scan_clips(cfg, log_data)
                  if approval_ok(c["meta"], cfg)]
    return _dedupe_candidates(candidates)


def find_pending_approval(cfg: dict, log_data: dict) -> list[dict]:
    """Clips awaiting the owner's approve/reject decision, best first.
    Deliberately no duplicate collapsing — the owner should see and judge
    every pending clip, not just the best of each near-duplicate group."""
    return [c for c in _scan_clips(cfg, log_data)
            if approval_state(c["meta"]) == "pending"]


def _norm_title(t: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def _same_source_window(a: dict, b: dict) -> bool:
    """True if two clips cover essentially the same source window (same source
    file and >=50% overlap of their [start, end] ranges) — a re-run of the
    same moment from a different job folder."""
    ma, mb = a["meta"], b["meta"]
    src = ma.get("source_name")
    if not src or src != mb.get("source_name"):
        return False
    a0, a1 = ma.get("original_source_start_s"), ma.get("original_source_end_s")
    b0, b1 = mb.get("original_source_start_s"), mb.get("original_source_end_s")
    if None in (a0, a1, b0, b1):
        return False
    inter = max(0.0, min(a1, b1) - max(a0, b0))
    union = max(a1, b1) - min(a0, b0)
    return union > 0 and (inter / union) >= 0.5


def _dedupe_candidates(candidates: list[dict]) -> list[dict]:
    """Collapse near-identical clips across job folders. Two clips are the same
    if their titles match OR they cover the same source window. The
    highest-score one (candidates are pre-sorted) stays uploadable; the rest
    are recorded on its `duplicates` list and dropped from the queue, so a
    duplicate can never be uploaded even from a stale UI (select_candidates
    re-runs this). Winners keep the best-first order."""
    winners: list[dict] = []
    for c in candidates:
        nt = _norm_title(c["meta"].get("title"))
        dup_of = next(
            (w for w in winners
             if (nt and nt == _norm_title(w["meta"].get("title")))
             or _same_source_window(c, w)), None)
        if dup_of is not None:
            dup_of["duplicates"].append(c["key"])
            continue
        c["duplicates"] = []
        winners.append(c)
    return winners


def select_candidates(candidates: list[dict], mode: str, count: int = 0,
                      keys: list[str] | None = None) -> list[dict]:
    """Subset of `candidates` (already sorted best-first by find_candidates)
    for a manual 'Upload now' batch: 'top' takes the first `count`; 'manual'
    keeps only the requested keys, in their find_candidates order (unknown
    keys — already uploaded/deleted since the UI last fetched — are dropped
    silently rather than erroring, since the caller re-fetches candidates
    fresh right before this)."""
    if mode == "manual":
        wanted = set(keys or [])
        return [c for c in candidates if c["key"] in wanted]
    return candidates[:max(0, int(count))]


def cap_warning(cfg: dict, log_data: dict, requested_count: int) -> str | None:
    """Plain-language warning when an immediate batch would push today's
    count past max_per_day; None when it fits. Never blocks by itself — an
    explicit 'Upload now' is a deliberate manual override, the caller decides
    whether to proceed after showing this."""
    max_day = cfg.get("upload", {}).get("max_per_day", 3)
    over = uploads_today(log_data) + requested_count - max_day
    if over <= 0:
        return None
    return (f"This is more than today's usual limit of {max_day} — the extra "
           f"{over} clip{'s' if over != 1 else ''} will publish today anyway "
           f"if you continue.")


# ============================================================
# Title / description / hashtags
# ============================================================
def clean_hashtags(raw: list, max_hashtags: int = 5) -> list[str]:
    """Upload-time cap: the best `max_hashtags` topic tags plus #shorts, using
    the same junk filter as metadata (metadata.clean_tag) so there's one
    policy. Input is already topic-first from metadata.topic_hashtags, so the
    leading `max_hashtags` are the strongest tags."""
    from metadata import clean_tag
    tags: list[str] = []
    for t in raw or []:
        word = clean_tag(t)
        if word and word != "shorts" and word not in tags:
            tags.append(word)
        if len(tags) >= max_hashtags:
            break
    tags.append("shorts")  # always present, always last
    return ["#" + t for t in tags]


def build_snippet(meta: dict) -> dict:
    """Clean title/description/hashtags from ClipMetadata, ready to merge
    into youtube_upload.build_request_body's metadata argument."""
    title = (meta.get("title") or "Untitled Short").strip()
    if len(title) > 90:  # keep headroom under YouTube's 100-char limit
        title = title[:87] + "..."
    hashtags = clean_hashtags(meta.get("hashtags"))
    return {"title": title, "description": (meta.get("description") or "").strip(),
            "hashtags": hashtags,
            # avatar-host clips: youtube_upload sets the altered/synthetic
            # content disclosure from this flag
            "synthetic": bool((meta.get("upload") or {}).get("synthetic"))}


# ============================================================
# Publish-time selection
# ============================================================
def get_peak_hours(cfg: dict | None, log_data: dict, count: int) -> list[int] | None:
    """Self-learning publish hours (publish_timing.pick_hours), replacing the
    old always-None stub. Returns None (use publish_slots_ist) whenever
    `cfg` is absent, learning is disabled, or the honesty gates in
    publish_timing haven't been met yet — the same "stay out of the way
    below the gates" contract this function has always had. Never raises
    into scheduling: any failure in the learning layer just falls back to
    configured slots, same as a real Analytics-API failure always has."""
    if cfg is None:
        return None
    try:
        import publish_timing
        return publish_timing.pick_hours(cfg, log_data, count)
    except Exception as e:  # noqa: BLE001 — learning must never block scheduling
        log.info("publish-time learning unavailable (%s); using default slots", e)
        return None


def next_publish_times(count: int, analytics, log_data: dict,
                       default_slots: list[int],
                       slot_spacing_minutes: int = 60,
                       slots_per_day: int | None = None,
                       cfg: dict | None = None) -> list[datetime]:
    """Pick the next `count` free publish slots, never in the past, never
    within slot_spacing_minutes of an already-scheduled video.

    Configured hours are the preferred slots for a day; if more slots are
    needed than there are configured hours (e.g. a single publish_slots_ist
    hour with max_per_day > 1), extra slots are packed after the last
    configured hour, spaced by slot_spacing_minutes, so max_per_day is
    actually reachable instead of silently capping at len(hours)/day.

    Slots land exactly on their computed minute (no jitter): jitter would
    let adjacent slots' effective gap shrink below slot_spacing_minutes,
    occasionally rejecting a legitimate same-day slot and spilling into
    the next day for no real reason.

    `cfg`, when given, lets self-learned hours (get_peak_hours) substitute
    for `default_slots` once its own gates pass; `analytics` is unused today
    (learning reads its own cached stats, not a live API call at scheduling
    time) but kept in the signature — every existing call site already
    passes it, and a live-API path may want it again later."""
    learned = get_peak_hours(cfg, log_data, len(default_slots) or 1)
    hours = sorted(set(learned or default_slots)) or [12]
    gap = timedelta(minutes=max(1, int(slot_spacing_minutes)))

    taken: list[datetime] = []
    for entry in log_data["uploads"].values():
        t = entry.get("publish_at")
        if not t:
            continue
        try:
            taken.append(datetime.fromisoformat(t))
        except ValueError:
            continue

    def free(candidate: datetime) -> bool:
        return all(abs(candidate - t) >= gap for t in taken)

    times: list[datetime] = []
    now = datetime.now(IST)
    day = now.date()
    while len(times) < count:
        day_bases = [datetime(day.year, day.month, day.day, h, 0, 0, tzinfo=IST)
                    for h in hours]
        if slots_per_day is not None:
            # schedule-ahead: exactly the configured slots per day, no packing —
            # so N clips spread across days instead of stacking into one day.
            day_bases = day_bases[:max(1, slots_per_day)]
        else:
            cursor = day_bases[-1] + gap
            while cursor.date() == day and len(day_bases) < 48:  # per-day ceiling
                day_bases.append(cursor)
                cursor += gap

        for candidate in day_bases:
            if len(times) == count:
                break
            if candidate <= now + timedelta(minutes=30) or not free(candidate):
                continue
            times.append(candidate)
            taken.append(candidate)
        day += timedelta(days=1)
    return times


# ============================================================
# UI panel snapshot
# ============================================================
def panel_state(cfg: dict, log_data: dict, authorized: bool,
                account: str = "default") -> dict:
    """Everything the UI's auto-upload panel shows, as plain data. Pure given
    its arguments (no I/O); the caller supplies config, log and auth state.
    `log_data` should already be scoped to the calling workspace (scoped_log)
    so "recent"/counts never leak another workspace's uploads."""
    acc = account_cfg(cfg, account)
    today = uploads_today(log_data, account)
    max_day = acc["max_per_day"]
    next_slot = None
    if authorized and acc["auto_enabled"] and today < max_day:
        slots = next_publish_times(
            1, None, log_data, acc["publish_slots_ist"],
            acc["slot_spacing_minutes"], cfg=cfg)
        next_slot = slots[0].isoformat() if slots else None
    recent = sorted(log_data["uploads"].values(),
                    key=lambda e: e.get("uploaded_at", ""), reverse=True)[:5]
    return {
        "auto_enabled": acc["auto_enabled"],
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
# End watermark (branded outro applied ONLY to the uploaded copy)
# ============================================================
BRAND_FONT = ROOT / "web" / "fonts" / "Doto-Variable.ttf"
_END_TMP = ROOT / "cache" / "upload_end"


def _safe_drawtext(s: str) -> str:
    """Injection-proof drawtext text: letters/digits/space only."""
    s = "".join(c for c in (s or "") if c.isalnum() or c == " ").strip()
    return s[:40] or "ClipForge"


def apply_end_watermark(video_path, cfg: dict) -> tuple[str, bool]:
    """When `upload.end_watermark.enabled`, return (temp_mp4, True): a copy of
    the clip with a short branded end card appended, so the UPLOADED file is
    branded while the archived render stays clean. Otherwise (video_path,
    False). Never raises into an upload — any probe/ffmpeg failure logs and
    falls back to the original clean file. Caller deletes the temp when True."""
    wm = cfg.get("upload", {}).get("end_watermark", {})
    if not wm.get("enabled"):
        return str(video_path), False
    try:
        return str(_render_end_card(Path(video_path), wm, cfg)), True
    except Exception as e:  # noqa: BLE001 — branding must never block an upload
        log.warning("end watermark skipped for %s (%s) — uploading clean file",
                    video_path, e)
        return str(video_path), False


def _render_end_card(src: Path, wm: dict, cfg: dict) -> Path:
    """Append a Doto-wordmark end card (matched to the clip's resolution/fps)
    and return the temp mp4. One ffmpeg pass: the outro is a `color` filter
    source with drawtext, concatenated after the (re-encoded) clip. Composes
    over any existing render-time watermark without doubling it — the brand
    card is on its own frames after the content, not overlaid on it."""
    if not BRAND_FONT.exists():
        raise UploadError(f"brand font missing: {BRAND_FONT}")
    info = ffutil.probe(src)
    w, h, fps = info["width"], info["height"], max(1.0, info["fps"])
    dur = min(3.0, max(0.5, float(wm.get("duration_s", 1.2))))
    text = _safe_drawtext(wm.get("text", "ClipForge"))
    font = ffutil.filter_path(BRAND_FONT)
    fade = max(0.2, dur * 0.4)

    _END_TMP.mkdir(parents=True, exist_ok=True)
    out = _END_TMP / f"{uuid.uuid4().hex[:12]}.mp4"

    drawtext = (f"drawtext=fontfile='{font}':text='{text}':fontcolor=0xd71921:"
                f"fontsize={max(12, int(h * 0.09))}:x=(w-text_w)/2:"
                f"y=(h-text_h)/2:alpha='min(1,t/{fade:.2f})'")
    graph = (
        f"color=c=0x0b0b0c:s={w}x{h}:d={dur:.3f}:r={fps:.3f},{drawtext},"
        "setsar=1,format=yuv420p[outv];"
        f"[0:v]scale={w}:{h},setsar=1,fps={fps:.3f},format=yuv420p[clipv];")
    args = ["-i", str(src), "-filter_complex", ""]
    if info["has_audio"]:
        graph += (
            "[0:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo[clipa];"
            f"anullsrc=r=44100:cl=stereo,atrim=0:{dur:.3f},"
            "aformat=sample_fmts=fltp:channel_layouts=stereo[outa];"
            "[clipv][clipa][outv][outa]concat=n=2:v=1:a=1[v][a]")
        maps = ["-map", "[v]", "-map", "[a]", "-c:a", "aac",
                "-b:a", cfg["render"]["audio_bitrate"]]
    else:
        graph += "[clipv][outv]concat=n=2:v=1:a=0[v]"
        maps = ["-map", "[v]"]
    args[-1] = graph
    args += maps + ffutil.video_encode_args(cfg, final=True) \
        + ["-movflags", "+faststart", str(out)]
    ffutil.run_ffmpeg(args, progress_label="end watermark")
    return out


# ============================================================
# Upload
# ============================================================
def _archive_after_upload(video_path, clip: dict, result: dict, snippet: dict,
                          uploaded_at: str, publish_at: str | None) -> None:
    """Best-effort permanent copy into archive/uploaded/ — must run while
    `video_path` still exists (the caller deletes a watermarked temp copy
    right after upload) and must never raise: archiving failing is never
    allowed to break an upload or take down the rest of a batch."""
    try:
        import archive
        archive.archive_clip(
            Path(video_path), clip["dir"], result.get("video_id"),
            result.get("url"), snippet, niche=clip["meta"].get("niche"),
            virality_score=clip.get("score"), uploaded_at=uploaded_at,
            publish_at=publish_at)
    except Exception as e:  # noqa: BLE001 — archiving must never break an upload
        log.warning("archiving failed for %s (%s); only the output/ copy "
                    "exists for now", clip["key"], e)


def upload_one(youtube, clip: dict, publish_at: datetime, category_id: str,
               service=None, cfg: dict | None = None) -> dict:
    snippet = build_snippet(clip["meta"])
    publish_at_iso = publish_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    log.info("uploading %s -> '%s' (publishes %s)", clip["key"], snippet["title"],
             publish_at.strftime("%d %b %H:%M IST"))
    video, is_temp = apply_end_watermark(clip["video"], cfg or {})
    try:
        result = youtube_upload.upload_clip(
            video, snippet, privacy="private", service=service or youtube,
            publish_at=publish_at_iso, category_id=category_id)
        result["uploaded_at"] = datetime.now(IST).isoformat()
        _archive_after_upload(video, clip, result, snippet,
                              result["uploaded_at"], publish_at.isoformat())
        return result
    finally:
        if is_temp:
            Path(video).unlink(missing_ok=True)


def upload_batch(youtube, analytics, cfg: dict, log_data: dict, limit: int,
                 slots_per_day: int | None = None,
                 account: str = "default") -> int:
    """Upload up to `limit` eligible, fully-rendered clips.
    Returns how many were uploaded. Safe to call repeatedly.
    `slots_per_day` caps publishes per calendar day (schedule-ahead spreads a
    batch across days instead of stacking it into one)."""
    upload_cfg = cfg.get("upload", {})
    if limit <= 0:
        return 0
    candidates = find_candidates(cfg, log_data)
    if not candidates:
        return 0

    batch = candidates[:limit]
    times = next_publish_times(len(batch), analytics, log_data,
                               upload_cfg.get("publish_slots_ist", [12, 19]),
                               upload_cfg.get("slot_spacing_minutes", 60),
                               slots_per_day=slots_per_day, cfg=cfg)
    category_id = upload_cfg.get("category_id", "22")
    ntfy_topic = upload_cfg.get("ntfy_topic", "")

    done = 0
    for clip, publish_at in zip(batch, times):
        try:
            result = upload_one(youtube, clip, publish_at, category_id, cfg=cfg)
        except UploadError as e:
            log.error("upload failed for %s: %s", clip["key"], e)
            notify("ClipForge upload FAILED", f"{clip['key']}: {e}\nWill retry next cycle.",
                  ntfy_topic)
            break
        log_data["uploads"][clip["key"]] = {
            "video_id": result["video_id"],
            "uploaded_at": result["uploaded_at"],
            "publish_at": publish_at.isoformat(),
            "title": build_snippet(clip["meta"])["title"],
            "virality_score": clip["score"],
            "account": account,
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
# Schedule-ahead: pre-book slots so the laptop needn't stay on
# ============================================================
def _slots_per_day(cfg: dict) -> int:
    up = cfg.get("upload", {})
    return max(1, min(len(up.get("publish_slots_ist", [12, 19])) or 1,
                      up.get("max_per_day", 20)))


def quota_status(cfg: dict, log_data: dict,
                 account: str = "default") -> dict:
    """Honest quota math for the UI, per destination account. Each upload
    spends QUOTA_PER_UPLOAD units against a QUOTA_DAILY_UNITS/day ceiling, so
    only so many clips can be *pushed to YouTube* per day;
    `can_schedule_now` is how many more today, counting what's already gone
    out (survives restarts via the log)."""
    acc = account_cfg(cfg, account)
    today = uploads_today(log_data, account)
    by_quota = QUOTA_DAILY_UNITS // QUOTA_PER_UPLOAD
    max_day = acc["max_per_day"]
    can_now = max(0, min(by_quota, max_day) - today)
    return {"account": account, "uploads_today": today,
            "uploads_per_day_by_quota": by_quota,
            "max_per_day": max_day, "can_schedule_now": can_now,
            "quota_per_upload": QUOTA_PER_UPLOAD,
            "quota_daily": QUOTA_DAILY_UNITS}


def _future_scheduled_count(log_data: dict, within_days: int | None = None) -> int:
    """How many uploads are booked to publish in the future (optionally only
    within `within_days`) — i.e. slots already filled in the horizon."""
    now = datetime.now(IST)
    end = now + timedelta(days=within_days) if within_days else None
    n = 0
    for e in log_data["uploads"].values():
        pa = e.get("publish_at")
        if not pa:
            continue
        try:
            t = datetime.fromisoformat(pa)
        except ValueError:
            continue
        if t > now and (end is None or t <= end):
            n += 1
    return n


def sync_schedule(youtube, analytics, cfg: dict, log_data: dict,
                  horizon_days: int | None = None,
                  account: str = "default") -> dict:
    """Fill open publish slots across the horizon with approved clips, uploading
    each as private + publishAt so YouTube publishes them with the app closed.
    Bounded by (a) today's remaining quota, (b) open slots left in the horizon,
    (c) available approved candidates — stops cleanly at the first limit.
    Single-sourced: selection is find_candidates, placement is upload_batch."""
    up = cfg.get("upload", {})
    horizon_days = int(horizon_days or up.get("schedule_ahead_days", 3))
    spd = _slots_per_day(cfg)
    capacity = spd * horizon_days
    open_slots = max(0, capacity - _future_scheduled_count(log_data, horizon_days))
    q = quota_status(cfg, log_data, account)
    candidates = find_candidates(cfg, log_data)
    n = min(q["can_schedule_now"], open_slots, len(candidates))
    done = upload_batch(youtube, analytics, cfg, log_data, limit=n,
                        slots_per_day=spd, account=account) if n > 0 else 0
    return {"scheduled": done, "open_slots": open_slots,
            "candidates": len(candidates), "horizon_days": horizon_days,
            "slots_per_day": spd, **quota_status(cfg, log_data, account)}


def classify_uploads(log_data: dict, live_status: dict | None = None) -> dict:
    """Split the upload log into clips still scheduled (publishAt in the future)
    versus already published. `live_status` maps video_id -> privacyStatus (from
    youtube_upload.video_status) and *refines* the split for the ids it knows: a
    'public' video counts as published even before its publishAt, a private one
    as still scheduled. Ids it doesn't cover (partial/failed status call) fall
    back to the publishAt clock, so a flaky status lookup never hides a clip."""
    now = datetime.now(IST)
    scheduled, published = [], []
    for key, e in log_data["uploads"].items():
        vid = e.get("video_id", "")
        row = {"key": key, "video_id": vid, "title": e.get("title", "Untitled"),
               "url": f"https://youtu.be/{vid}", "publish_at": e.get("publish_at", ""),
               "uploaded_at": e.get("uploaded_at", ""),
               "score": e.get("virality_score")}
        try:
            future = bool(e.get("publish_at")) and \
                datetime.fromisoformat(e["publish_at"]) > now
        except ValueError:
            future = False
        status = (live_status or {}).get(vid)
        if status == "public":
            published.append(row)
        elif status in ("private", "unlisted"):
            scheduled.append(row)
        else:                            # unknown -> trust the publishAt clock
            (scheduled if future else published).append(row)
    scheduled.sort(key=lambda r: r.get("publish_at", ""))
    published.sort(key=lambda r: r.get("uploaded_at", ""), reverse=True)
    return {"scheduled": scheduled, "published": published}


def unschedule(youtube, key: str, log_data: dict) -> dict:
    """Pull a pre-booked clip back before it publishes: delete the private
    upload on YouTube and drop its log entry, so the local clip becomes
    eligible again. Refuses a clip whose publish time has already passed
    (it may be live — deleting a public video is not an 'un-schedule')."""
    entry = log_data["uploads"].get(key)
    if not entry:
        raise UploadError("That clip isn't scheduled.")
    pa = entry.get("publish_at")
    try:
        if pa and datetime.fromisoformat(pa) <= datetime.now(IST):
            raise UploadError("That clip has already published — un-schedule "
                              "only works before its publish time.")
    except ValueError:
        pass
    vid = entry.get("video_id")
    if vid:
        youtube_upload.delete_video(vid, service=youtube)
    del log_data["uploads"][key]
    save_log(log_data)
    return {"unscheduled": key, "video_id": vid}


def upload_now(youtube, cfg: dict, log_data: dict, clips: list[dict],
               on_progress=None, account: str = "default") -> list[dict]:
    """Publish `clips` immediately (public, no publish_at) — the manual
    'Upload now' override, distinct from upload_batch's scheduled path.
    Unlike upload_batch, a failed clip does NOT stop the rest of the batch
    (this is an explicit one-click action the owner is watching; the rest
    should still go out). Calls on_progress(result) after each clip, if
    given, so a caller can stream live status. Returns one result dict per
    clip: {key, title, status: 'done'|'failed', url?, error?}."""
    upload_cfg = cfg.get("upload", {})
    category_id = upload_cfg.get("category_id", "22")
    ntfy_topic = upload_cfg.get("ntfy_topic", "")

    results = []
    for clip in clips:
        snippet = build_snippet(clip["meta"])
        video, is_temp = apply_end_watermark(clip["video"], cfg)
        try:
            result = youtube_upload.upload_clip(
                video, snippet, privacy="public", service=youtube,
                publish_at=None, category_id=category_id)
            now_iso = datetime.now(IST).isoformat()
            # archive while `video` (the watermarked temp copy, if any) still
            # exists — the finally below deletes it right after this. This is
            # an immediate publish (publish_at=None above), so the archive
            # record should say so too, not repeat uploaded_at as a fake slot.
            _archive_after_upload(video, clip, result, snippet, now_iso, None)
        except UploadError as e:
            log.error("upload now failed for %s: %s", clip["key"], e)
            notify("ClipForge upload FAILED", f"{clip['key']}: {e}", ntfy_topic)
            item = {"key": clip["key"], "title": snippet["title"],
                    "status": "failed", "error": str(e)}
            results.append(item)
            if on_progress:
                on_progress(item)
            continue
        finally:
            if is_temp:
                Path(video).unlink(missing_ok=True)

        log_data["uploads"][clip["key"]] = {
            "video_id": result["video_id"], "uploaded_at": now_iso,
            "publish_at": now_iso, "title": snippet["title"],
            "virality_score": clip["score"], "account": account,
        }
        save_log(log_data)
        notify("Short published",
              f"'{snippet['title']}' (virality {clip['score']}) is live now\n"
              f"{result['url']}", ntfy_topic)
        item = {"key": clip["key"], "title": snippet["title"],
                "status": "done", "url": result["url"]}
        results.append(item)
        if on_progress:
            on_progress(item)
    return results


# ============================================================
# Posting queue (per-account, priority-ordered, drag-reorderable)
# ============================================================
def load_queue() -> dict:
    if QUEUE_FILE.exists():
        try:
            data = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
            data.setdefault("queue", [])
            return data
        except json.JSONDecodeError:
            backup = QUEUE_FILE.with_suffix(".json.corrupt")
            QUEUE_FILE.replace(backup)
            log.warning("%s was corrupt; moved to %s, starting fresh",
                        QUEUE_FILE.name, backup.name)
    return {"queue": []}


def save_queue(data: dict) -> None:
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = QUEUE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(QUEUE_FILE)


def queue_add(clip_key: str, account: str = "default",
              source: str = "manual", qdata: dict | None = None) -> dict:
    """Insert a clip into the posting queue at its priority position
    (channel_new > channel_top > manual; FIFO within a rank). Re-adding an
    already-queued clip is a no-op. Manual drag order is respected: insertion
    only skips past lower-priority entries, never reorders existing ones."""
    data = qdata if qdata is not None else load_queue()
    if any(e["clip_key"] == clip_key for e in data["queue"]):
        return data
    entry = {"clip_key": clip_key, "account": account or "default",
             "source": source if source in _SOURCE_RANK else "manual",
             "added_at": datetime.now(IST).isoformat()}
    rank = _SOURCE_RANK[entry["source"]]
    pos = len(data["queue"])
    for i, e in enumerate(data["queue"]):
        if _SOURCE_RANK.get(e.get("source", "manual"), 2) > rank:
            pos = i
            break
    data["queue"].insert(pos, entry)
    if qdata is None:
        save_queue(data)
    return data


def queue_reorder(clip_keys: list[str]) -> dict:
    """Persist a manual drag order: listed keys first (in the given order),
    any unlisted entries keep their relative order after them."""
    data = load_queue()
    by_key = {e["clip_key"]: e for e in data["queue"]}
    ordered = [by_key[k] for k in clip_keys if k in by_key]
    rest = [e for e in data["queue"] if e["clip_key"] not in set(clip_keys)]
    data["queue"] = ordered + rest
    save_queue(data)
    return data


def queue_remove(clip_key: str) -> dict:
    data = load_queue()
    data["queue"] = [e for e in data["queue"] if e["clip_key"] != clip_key]
    save_queue(data)
    return data


def drain_queue(cfg: dict, service=None) -> int:
    """Upload queued clips in queue order, per account, respecting each
    account's daily cap and publish slots. Entries whose clip no longer
    qualifies (deleted, already uploaded, rejected, below virality) are
    dropped. Returns how many uploads went out. Never raises."""
    done = 0
    try:
        data = load_queue()
        if not data["queue"]:
            return 0
        log_data = load_log()
        candidates = {c["key"]: c for c in find_candidates(cfg, log_data)}
        for account in list_accounts(cfg):
            mine = [e for e in data["queue"] if
                    e.get("account", "default") == account]
            if not mine:
                continue
            if not youtube_upload.authorized(account) and service is None:
                log.info("queue: account '%s' not authorized; %d clip(s) wait",
                         account, len(mine))
                continue
            remaining = quota_status(cfg, log_data, account)["can_schedule_now"]
            acc = account_cfg(cfg, account)
            batch = []
            for e in mine:
                if len(batch) >= remaining:
                    break
                c = candidates.get(e["clip_key"])
                if c is None:
                    # gone for good (uploaded/deleted) → drop; merely
                    # ineligible right now (e.g. awaiting approval) → wait
                    if e["clip_key"] in log_data["uploads"] or \
                            not (ROOT / e["clip_key"] / "final.mp4").exists():
                        data["queue"].remove(e)
                    continue
                batch.append((e, c))
            if not batch:
                continue
            youtube = youtube_upload.build_service(service, account=account)
            times = next_publish_times(
                len(batch), None, log_data, acc["publish_slots_ist"],
                acc["slot_spacing_minutes"], cfg=cfg)
            category_id = cfg.get("upload", {}).get("category_id", "22")
            ntfy = cfg.get("upload", {}).get("ntfy_topic", "")
            for (entry, clip), publish_at in zip(batch, times):
                try:
                    result = upload_one(youtube, clip, publish_at,
                                        category_id, cfg=cfg)
                except UploadError as e:
                    log.error("queue upload failed for %s: %s",
                              clip["key"], e)
                    notify("ClipForge upload FAILED",
                           f"{clip['key']}: {e}\nStays queued.", ntfy)
                    break
                log_data["uploads"][clip["key"]] = {
                    "video_id": result["video_id"],
                    "uploaded_at": result["uploaded_at"],
                    "publish_at": publish_at.isoformat(),
                    "title": build_snippet(clip["meta"])["title"],
                    "virality_score": clip["score"],
                    "account": account,
                }
                save_log(log_data)
                data["queue"].remove(entry)
                done += 1
                notify("Short scheduled",
                       f"'{build_snippet(clip['meta'])['title']}' → "
                       f"{account}, publishes "
                       f"{publish_at.strftime('%d %b, %I:%M %p IST')}",
                       ntfy)
        save_queue(data)
    except Exception as e:  # noqa: BLE001 — queue drain must never kill a caller
        log.warning("queue drain failed: %s", e)
    return done


def calendar_view(cfg: dict, log_data: dict, account: str | None = None,
                  days: int = 7) -> dict:
    """What's going out when: scheduled publishes grouped by date (today
    onward, `days` ahead) plus the still-unscheduled queue. Pure given its
    arguments."""
    start = datetime.now(IST).date()
    day_map: dict[str, list] = {
        (start + timedelta(days=i)).isoformat(): [] for i in range(days)}
    for key, e in log_data["uploads"].items():
        if account and e.get("account", "default") != account:
            continue
        pa = e.get("publish_at")
        try:
            t = datetime.fromisoformat(pa) if pa else None
        except ValueError:
            continue
        if t is None:
            continue
        d = t.date().isoformat()
        if d in day_map:
            day_map[d].append({"key": key, "title": e.get("title", ""),
                               "time": t.strftime("%H:%M"),
                               "account": e.get("account", "default"),
                               "video_id": e.get("video_id", "")})
    for entries in day_map.values():
        entries.sort(key=lambda x: x["time"])
    queued = [e for e in load_queue()["queue"]
              if not account or e.get("account", "default") == account]
    return {"days": [{"date": d, "posts": v} for d, v in day_map.items()],
            "queued": queued}


# ============================================================
# Pipeline hook — the primary, event-driven path
# ============================================================
def trigger_after_render(clip_dir: Path, cfg: dict) -> None:
    """Called right after a clip's final.mp4 + metadata.json are finalized.
    Routes the clip INTO the posting queue (single code path for pacing and
    quota), then drains the queue. No-ops unless upload.auto_enabled. Never
    raises into the caller — a failed/unqualified upload must never fail a
    clip."""
    upload_cfg = cfg.get("upload", {})
    if not upload_cfg.get("auto_enabled", False):
        return
    try:
        key = str(Path(clip_dir).resolve().relative_to(ROOT)).replace("\\", "/")
        queue_add(key,
                  account=upload_cfg.get("queue_account", "default"),
                  source=upload_cfg.get("queue_source", "manual"))
        if not youtube_upload.credentials_available():
            log.info("auto-upload enabled but not configured; %s queued",
                     clip_dir.name)
            return
        drain_queue(cfg)
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
    max_per_day = upload_cfg.get("max_per_day", 20)
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
