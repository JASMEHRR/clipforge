"""Self-learning publish-hour picker.

HONESTY CONSTRAINT: YouTube's Analytics API has no "when your audience is
online" endpoint (confirmed by upload_scheduler.get_peak_hours' own history —
see git blame). This module NEVER pretends otherwise. All it does is score
IST publish hours by the early-window performance of videos ClipForge itself
already published at those hours, explore under-sampled hours while data is
thin, and stay completely out of the way (upload_scheduler falls back to
config.upload.publish_slots_ist) below two honesty gates:
  - min_total_uploads (default 15): the whole system needs this many
    ClipForge uploads on record before it overrides configured slots at all.
  - min_hour_samples (default 3): one hour needs this many videos before its
    score is "trusted" (used for ranking rather than left as an exploration
    candidate).

DAY-GRANULARITY NOTE: the YouTube Analytics Reporting API buckets by
calendar day (IST), not by hour. "24h views" here means "views on the
calendar day the video published"; "72h views" means that day plus the
following two. That's an approximation of a rolling 24/72-hour window, not
a literal one — documented here and surfaced in the panel's honesty copy.

Single scheduling integration point: upload_scheduler.get_peak_hours calls
pick_hours() below and nothing else in the codebase computes publish hours —
the watcher, CLI, schedule-ahead sync, and manual "Upload now" all already
funnel through upload_scheduler.next_publish_times, so there is exactly one
scheduling path, as before."""
from __future__ import annotations

import json
import random
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

import upload_scheduler
import youtube_upload
from config import ROOT
from logutil import get_logger

log = get_logger("publish_timing")

STATS_FILE = ROOT / "cache" / "publish_timing_stats.json"
IST = timezone(timedelta(hours=5, minutes=30))

MIN_HOUR_SAMPLES = 3
MIN_TOTAL_UPLOADS = 15
RECENCY_HALF_LIFE_DAYS = 30
EXPLORE_WINDOW = (8, 23)          # IST hours eligible for exploration
EXPLORE_EPSILON_START = 0.3
EXPLORE_EPSILON_FLOOR = 0.05
EXPLORE_EPSILON_DECAY_PER_UPLOAD = 0.01

_VIDEO_METRICS_FULL = "views,impressions,impressionClickThroughRate,averageViewPercentage"
_VIDEO_METRICS_BASIC = "views,averageViewPercentage"
_CHANGELOG_MAX = 50


# ============================================================
# Stats store (cache/publish_timing_stats.json) — same atomic
# tmp+replace / corrupt-quarantine convention as upload_log.json.
# ============================================================
def load_stats() -> dict:
    if not STATS_FILE.exists():
        return {"videos": {}, "changelog": [], "active_ranking": None}
    try:
        data = json.loads(STATS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        backup = STATS_FILE.with_suffix(".json.corrupt")
        try:
            STATS_FILE.replace(backup)
            log.warning("%s was corrupt; moved to %s, starting fresh",
                       STATS_FILE.name, backup.name)
        except OSError as e:
            log.warning("could not quarantine corrupt %s (%s); deleting it",
                       STATS_FILE.name, e)
            STATS_FILE.unlink(missing_ok=True)
        return {"videos": {}, "changelog": [], "active_ranking": None}
    data.setdefault("videos", {})
    data.setdefault("changelog", [])
    data.setdefault("active_ranking", None)
    return data


def save_stats(data: dict) -> None:
    STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATS_FILE)  # atomic on the same drive; no half-written store


def reset_stats() -> None:
    """Owner control: forget every learned score and the change log. Config
    (enabled/pins/bans/gates) is untouched — only the collected performance
    data resets, so learning starts over from zero samples."""
    save_stats({"videos": {}, "changelog": [], "active_ranking": None})


# ============================================================
# Part 1 — performance data collection
# ============================================================
def _fetch_window(analytics, video_id: str, start_date, end_date) -> dict | None:
    """One video's totals over [start_date, end_date] (inclusive calendar
    days, no `dimensions` — filtered to a single video the same way
    analytics._fetch_overview filters to the whole channel). Tries the full
    metric set first; impressions/CTR aren't available on every channel, so
    a failure there falls back to the basic set rather than losing the
    whole window over one optional metric. Returns None only if both
    attempts fail outright (network/auth error, not just missing metrics)."""
    for metrics in (_VIDEO_METRICS_FULL, _VIDEO_METRICS_BASIC):
        try:
            res = analytics.reports().query(
                ids="channel==MINE", startDate=str(start_date), endDate=str(end_date),
                metrics=metrics, filters=f"video=={video_id}",
            ).execute()
        except Exception as e:  # noqa: BLE001 — try the cheaper metric set next
            log.info("analytics window fetch failed for %s with %s (%s)",
                    video_id, metrics, e)
            continue
        rows = res.get("rows") or []
        if not rows:
            return {"views": 0, "impressions": None, "ctr": None, "avg_view_pct": 0.0}
        if metrics is _VIDEO_METRICS_FULL:
            views, impressions, ctr, avg_pct = rows[0]
            return {"views": int(views), "impressions": int(impressions),
                   "ctr": round(float(ctr), 2), "avg_view_pct": round(float(avg_pct), 1)}
        views, avg_pct = rows[0]
        return {"views": int(views), "impressions": None, "ctr": None,
               "avg_view_pct": round(float(avg_pct), 1)}
    return None


def _clip_niche(key: str) -> str | None:
    """Best-effort re-read of a clip's own metadata.json for its niche (not
    stored in upload_log.json). None if the clip folder is gone/moved."""
    try:
        meta = json.loads((ROOT / key / "metadata.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return meta.get("niche")


def refresh_stats(analytics=None, now: datetime | None = None) -> dict:
    """Incremental collection: for every ClipForge-uploaded video whose 24h
    (or 72h) window has fully closed and hasn't been captured yet, fetch and
    freeze it — a closed window is a historical fact, never refetched, so
    repeated calls (the daily job, an app-open sync) do no wasted work once
    the backlog is caught up. One video's fetch failure never blocks the
    rest. `analytics`/`now` are injectable for tests."""
    now = now or datetime.now(IST)
    stats = load_stats()
    videos = stats["videos"]
    log_data = upload_scheduler.load_log()
    uploads = log_data.get("uploads", {})
    if not uploads:
        return stats

    if analytics is None:
        analytics = youtube_upload.build_analytics_service()

    changed = False
    for key, entry in uploads.items():
        video_id = entry.get("video_id")
        publish_at = entry.get("publish_at")
        if not video_id or not publish_at:
            continue
        try:
            published = datetime.fromisoformat(publish_at)
        except ValueError:
            continue
        record = videos.setdefault(video_id, {
            "key": key, "publish_at": publish_at, "hour_ist": published.hour,
            "views_24h": None, "views_72h": None, "impressions": None,
            "ctr": None, "avg_view_pct": None,
            "virality_score": entry.get("virality_score"), "niche": None,
        })
        pub_date = published.date()

        if record["views_24h"] is None and now.date() > pub_date:
            data = _fetch_window(analytics, video_id, pub_date, pub_date)
            if data:
                record.update(views_24h=data["views"], impressions=data["impressions"],
                              ctr=data["ctr"], avg_view_pct=data["avg_view_pct"])
                changed = True

        if record["views_72h"] is None and now.date() > pub_date + timedelta(days=2):
            data = _fetch_window(analytics, video_id, pub_date, pub_date + timedelta(days=2))
            if data:
                record["views_72h"] = data["views"]
                changed = True

        if record["niche"] is None:
            niche = _clip_niche(key)
            if niche is not None:
                record["niche"] = niche
                changed = True

    if changed:
        save_stats(stats)
    return stats


# ============================================================
# Part 2 — hour scoring + exploration
# ============================================================
def _recency_weight(publish_at_iso: str, now: datetime, half_life_days: float) -> float:
    try:
        published = datetime.fromisoformat(publish_at_iso)
    except (ValueError, TypeError):
        return 0.0
    days = max(0.0, (now - published).total_seconds() / 86400)
    return 0.5 ** (days / max(1.0, half_life_days))


def score_hours(videos: list[dict], half_life_days: float = RECENCY_HALF_LIFE_DAYS,
                now: datetime | None = None) -> dict[int, dict]:
    """{hour_ist: {"score": normalized recency-weighted mean performance,
    "sample_count": raw video count}} over every video with a captured 24h
    window. Normalization is early_views / channel_median_early_views (the
    median recomputed fresh over all currently-scored videos, so it rolls
    forward as the store grows) — one viral outlier scores as an outlier,
    it doesn't drag every other video's score up with it. Weight decays by
    half `half_life_days` days old, so the last 30-60 days dominate without
    a hard cutoff."""
    now = now or datetime.now(IST)
    usable = [v for v in videos if v.get("views_24h") is not None
             and v.get("hour_ist") is not None]
    if not usable:
        return {}
    median = statistics.median(v["views_24h"] for v in usable) or 1

    by_hour: dict[int, list[tuple[float, float]]] = {}
    for v in usable:
        norm = v["views_24h"] / median
        weight = _recency_weight(v["publish_at"], now, half_life_days)
        by_hour.setdefault(v["hour_ist"], []).append((norm, weight))

    out = {}
    for hour, pairs in by_hour.items():
        total_w = sum(w for _, w in pairs)
        score = sum(n * w for n, w in pairs) / total_w if total_w > 0 else 0.0
        out[hour] = {"score": round(score, 4), "sample_count": len(pairs)}
    return out


def _epsilon(total_uploads: int, pt_cfg: dict) -> float:
    """Exploration fraction: starts high while data is thin, decays linearly
    with total upload count, never below a small floor (so the system keeps
    checking in on under-sampled hours indefinitely, just rarely)."""
    start = pt_cfg.get("explore_epsilon_start", EXPLORE_EPSILON_START)
    floor = pt_cfg.get("explore_epsilon_floor", EXPLORE_EPSILON_FLOOR)
    decay = pt_cfg.get("explore_epsilon_decay_per_upload", EXPLORE_EPSILON_DECAY_PER_UPLOAD)
    return max(floor, start - decay * total_uploads)


def _ranked_hours(scored: dict[int, dict], banned: set[int],
                  min_hour_samples: int) -> tuple[list[int], list[int]]:
    """(trusted, any_scored) hour lists, both best-first, banned excluded.
    `trusted` only has hours meeting min_hour_samples; `any_scored` is every
    scored hour regardless — used as a lower-confidence fallback so a system
    past the total-uploads gate but with samples spread thin across many
    hours still has *something* to exploit instead of going empty."""
    any_scored = sorted((h for h in scored if h not in banned),
                        key=lambda h: scored[h]["score"], reverse=True)
    trusted = [h for h in any_scored if scored[h]["sample_count"] >= min_hour_samples]
    return trusted, any_scored


def pick_hours(cfg: dict, log_data: dict, count: int,
               rng: random.Random | None = None,
               now: datetime | None = None) -> list[int] | None:
    """The self-learning entry point upload_scheduler.get_peak_hours calls.
    Returns None (use config.upload.publish_slots_ist) when learning is off,
    the total-uploads gate isn't met, or there's simply nothing scored yet —
    otherwise a list of up to `count` IST hours: the best-known (trusted, or
    failing that any-scored) hours, pinned hours always included, banned
    hours always excluded, with a `rng`-driven chance (decaying epsilon) of
    swapping in one under-sampled hour from the 08:00-23:00 exploration
    window so the system keeps learning instead of freezing on day-1 data."""
    now = now or datetime.now(IST)
    upload_cfg = cfg.get("upload", {})
    pt_cfg = upload_cfg.get("publish_timing", {})
    if not pt_cfg.get("enabled", True):
        return None

    total_uploads = len(log_data.get("uploads", {}))
    min_total = pt_cfg.get("min_total_uploads", MIN_TOTAL_UPLOADS)
    if total_uploads < min_total:
        return None

    stats = load_stats()
    scored = score_hours(list(stats["videos"].values()),
                         half_life_days=pt_cfg.get("recency_half_life_days",
                                                   RECENCY_HALF_LIFE_DAYS), now=now)
    min_hour_samples = pt_cfg.get("min_hour_samples", MIN_HOUR_SAMPLES)
    banned = set(pt_cfg.get("banned_hours", []))
    pinned = [h for h in pt_cfg.get("pinned_hours", []) if h not in banned]

    trusted, any_scored = _ranked_hours(scored, banned, min_hour_samples)
    exploit_pool = trusted or any_scored
    picked = list(dict.fromkeys(pinned + exploit_pool))
    if not picked:
        return None  # gate passed but nothing scored/pinned yet — stay on config slots

    lo, hi = pt_cfg.get("explore_window_start", EXPLORE_WINDOW[0]), \
        pt_cfg.get("explore_window_end", EXPLORE_WINDOW[1])
    under_sampled = sorted(
        (h for h in range(lo, hi + 1) if h not in banned
         and scored.get(h, {"sample_count": 0})["sample_count"] < min_hour_samples),
        key=lambda h: scored.get(h, {"sample_count": 0})["sample_count"])

    rng = rng or random
    if under_sampled and rng.random() < _epsilon(total_uploads, pt_cfg):
        explore_hour = under_sampled[0]
        if explore_hour not in picked:
            if len(picked) >= max(1, count):
                picked[-1] = explore_hour   # swap out the weakest exploit hour
            else:
                picked.append(explore_hour)

    return picked[:max(1, count)] or None


# ============================================================
# Part 3 — daily tweak loop + panel state
# ============================================================
def _fmt_hours(hours: list[int]) -> str:
    return ", ".join(f"{h:02d}:00" for h in hours) if hours else "(none)"


def _describe_change(previous: dict | None, active: list[int],
                     scored: dict[int, dict]) -> str:
    if not previous or not previous.get("hours"):
        return f"Learned enough to suggest active hours: {_fmt_hours(active)}."
    prev_hours = previous["hours"]
    added = [h for h in active if h not in prev_hours]
    removed = [h for h in prev_hours if h not in active]
    if not added and not removed:
        return f"No change — active hours: {_fmt_hours(active)}."
    if len(added) == 1 and len(removed) == 1:
        old_h, new_h = removed[0], added[0]
        old_score = scored.get(old_h, {}).get("score", 0)
        new_stats = scored.get(new_h, {"score": 0, "sample_count": 0})
        if old_score > 0 and new_stats["score"] > 0:
            ratio = new_stats["score"] / old_score
            n = new_stats["sample_count"]
            return (f"Moved slot {old_h:02d}:00 → {new_h:02d}:00 — {new_h:02d}:00 "
                    f"median normalized 24h views {ratio:.1f}x higher over "
                    f"{n} video{'s' if n != 1 else ''}.")
        return f"Moved slot {old_h:02d}:00 → {new_h:02d}:00."
    return f"Active hours updated: {_fmt_hours(active)}."


def recompute_ranking(cfg: dict, log_data: dict, now: datetime | None = None) -> dict:
    """The daily/on-demand tweak loop: recomputes the best-known hour ranking
    (exploit-only — no exploration noise, so the log/panel reflect the
    system's actual confidence, not a single call's random slot swap) and
    appends a changelog entry when the active set changes. Called by the
    daily analytics refresh and the panel's manual 'Recompute now'. Returns
    the fresh panel state (see publish_timing_state)."""
    now = now or datetime.now(IST)
    stats = load_stats()
    upload_cfg = cfg.get("upload", {})
    pt_cfg = upload_cfg.get("publish_timing", {})
    total_uploads = len(log_data.get("uploads", {}))
    min_total = pt_cfg.get("min_total_uploads", MIN_TOTAL_UPLOADS)
    min_hour_samples = pt_cfg.get("min_hour_samples", MIN_HOUR_SAMPLES)
    banned = set(pt_cfg.get("banned_hours", []))
    pinned = [h for h in pt_cfg.get("pinned_hours", []) if h not in banned]

    scored = score_hours(list(stats["videos"].values()),
                         half_life_days=pt_cfg.get("recency_half_life_days",
                                                   RECENCY_HALF_LIFE_DAYS), now=now)
    trusted, _any = _ranked_hours(scored, banned, min_hour_samples)
    ranked = list(dict.fromkeys(pinned + trusted))

    # slot count follows max_per_day / publish_slots_ist, unaffected by
    # learning — same formula as upload_scheduler._slots_per_day (kept in
    # sync deliberately rather than duplicated as a public export)
    slots = upload_scheduler._slots_per_day(cfg)
    gates_passed = bool(pt_cfg.get("enabled", True) and total_uploads >= min_total and ranked)
    active = ranked[:slots] if gates_passed else list(
        upload_cfg.get("publish_slots_ist", [12, 19]))[:slots] or [12]

    previous = stats.get("active_ranking")
    if previous is None or previous.get("hours") != active:
        message = _describe_change(previous, active, scored)
        stats["changelog"].append({"at": now.isoformat(), "message": message,
                                   "hours": active})
        stats["changelog"] = stats["changelog"][-_CHANGELOG_MAX:]
    stats["active_ranking"] = {"hours": active, "as_of": now.isoformat(),
                               "gates_passed": gates_passed}
    save_stats(stats)
    return publish_timing_state(cfg, log_data, stats=stats, now=now)


def publish_timing_state(cfg: dict, log_data: dict, stats: dict | None = None,
                         now: datetime | None = None) -> dict:
    """Everything the Analytics tab's 'Schedule intelligence' panel shows, as
    plain data — pure given its arguments plus one stats-file read (no
    network I/O)."""
    now = now or datetime.now(IST)
    stats = stats if stats is not None else load_stats()
    upload_cfg = cfg.get("upload", {})
    pt_cfg = upload_cfg.get("publish_timing", {})
    total_uploads = len(log_data.get("uploads", {}))
    min_total = pt_cfg.get("min_total_uploads", MIN_TOTAL_UPLOADS)
    min_hour_samples = pt_cfg.get("min_hour_samples", MIN_HOUR_SAMPLES)

    scored = score_hours(list(stats["videos"].values()),
                         half_life_days=pt_cfg.get("recency_half_life_days",
                                                   RECENCY_HALF_LIFE_DAYS), now=now)
    ranking = sorted(scored.items(), key=lambda kv: kv[1]["score"], reverse=True)
    active = stats.get("active_ranking") or {}

    return {
        "enabled": bool(pt_cfg.get("enabled", True)),
        "gates_passed": bool(active.get("gates_passed", False)),
        "total_uploads": total_uploads, "min_total_uploads": min_total,
        "min_hour_samples": min_hour_samples,
        "active_hours": active.get("hours") or list(
            upload_cfg.get("publish_slots_ist", [12, 19])),
        "using_learned_hours": bool(active.get("gates_passed", False)),
        "ranking": [{"hour": h, "score": s["score"], "sample_count": s["sample_count"],
                    "trusted": s["sample_count"] >= min_hour_samples}
                   for h, s in ranking],
        "pinned_hours": sorted(pt_cfg.get("pinned_hours", [])),
        "banned_hours": sorted(pt_cfg.get("banned_hours", [])),
        "changelog": list(reversed(stats.get("changelog", [])))[:10],
        "note": ("Learns from ClipForge's own upload history and view counts "
                "at each hour it has actually published — this is NOT "
                "YouTube's \"when your audience is online\" chart; the "
                "Analytics API doesn't expose that."),
    }


# ============================================================
# Owner controls (config mutations — routes call these, not config
# directly, so the read-modify-write list semantics live in one place)
# ============================================================
def set_enabled(enabled: bool) -> None:
    from config import save_config
    save_config({"upload": {"publish_timing": {"enabled": bool(enabled)}}})


def _toggle_hour(cfg: dict, hour: int, field: str, other_field: str) -> list[int]:
    from config import load_config, save_config
    if not 0 <= hour <= 23:
        raise ValueError("hour must be 0-23")
    # Re-read persisted config: the caller's cfg snapshot may predate an
    # earlier toggle in the same process, and toggling against stale state
    # re-adds instead of removing.
    pt_cfg = load_config().get("upload", {}).get("publish_timing", {})
    current = set(pt_cfg.get(field, []))
    other = set(pt_cfg.get(other_field, []))
    if hour in current:
        current.discard(hour)
    else:
        current.add(hour)
    other.discard(hour)  # pinning un-bans and vice versa — an hour can't be both
    save_config({"upload": {"publish_timing": {
        field: sorted(current), other_field: sorted(other)}}})
    return sorted(current)


def toggle_pin(cfg: dict, hour: int) -> list[int]:
    return _toggle_hour(cfg, hour, "pinned_hours", "banned_hours")


def toggle_ban(cfg: dict, hour: int) -> list[int]:
    return _toggle_hour(cfg, hour, "banned_hours", "pinned_hours")
