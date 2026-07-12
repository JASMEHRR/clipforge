"""Pure recommendation engine over joined YouTube-Analytics + upload-log data.

Zero I/O, zero Google API surface — every function here takes plain dicts/lists
and returns plain dicts, so it's fully testable with synthetic fixtures and no
mocking. `analytics.py` is the only caller; it does the fetching and hands this
module already-joined rows.

Every recommendation dict returned by `recommend()` carries a non-empty
`evidence` sub-dict with the exact numbers backing its `message` — the UI must
never show a claim without the numbers behind it (the "honesty rule")."""
from __future__ import annotations

import statistics
from datetime import datetime
from typing import Any

from upload_scheduler import MIN_VIEWS_FOR_ANALYTICS, _norm_title

MIN_CLIPS_FOR_INSIGHTS = 5

_LENGTH_BUCKETS: list[tuple[float, float | None, str]] = [
    (0, 30, "under 30s"),
    (30, 45, "30-45s"),
    (45, 60, "45-60s"),
    (60, 90, "60-90s"),
    (90, None, "over 90s"),
]
_MIN_CLIPS_PER_BUCKET = 3
_MIN_CLIPS_PER_CLUSTER = 2
_TOPIC_OUTPERFORM_THRESHOLD = 0.20   # 20% above overall median views
_WEAK_HOOK_AVG_PCT = 30.0
_MIN_DISTINCT_HOURS = 3


def join_rows(analytics_rows: list[tuple], upload_log: dict,
             clip_extra_fn=None) -> list[dict[str, Any]]:
    """Join YouTube Analytics `reports().query()` rows (dimensions=video: each
    row is `(video_id, views, estimated_minutes, avg_view_pct, likes,
    subs_gained)`, same shape as `upload_scheduler.report()`) against
    `upload_log["uploads"]` (keyed by clip path, each entry carrying
    `video_id`/`title`/`publish_at`). Only videos ClipForge itself uploaded
    (i.e. present in `upload_log`) are returned — analytics rows for videos
    uploaded outside ClipForge are silently dropped, since this tab is scoped
    to ClipForge's own output. `analytics_rows` itself is capped by the
    caller's `maxResults` (a channel with more ClipForge uploads than that
    cap will have its lowest-viewed clips missing from `analytics_rows`
    entirely, so they're absent here too — not just "outside ClipForge").

    `clip_extra_fn(key) -> dict`, if given, is called per matched clip to add
    `duration_s`/`source_name` (best-effort re-read of that clip's
    metadata.json — injectable so tests never touch the filesystem)."""
    uploads: dict = upload_log.get("uploads", {})
    id_to_key = {v.get("video_id"): k for k, v in uploads.items() if v.get("video_id")}

    videos: list[dict[str, Any]] = []
    for row in analytics_rows:
        vid, views, _minutes, avg_pct, likes, subs = row
        key = id_to_key.get(vid)
        if key is None:
            continue
        entry = uploads[key]
        extra = clip_extra_fn(key) if clip_extra_fn else {}
        videos.append({
            "video_id": vid, "key": key, "title": entry.get("title", "Untitled"),
            "views": int(views), "avg_view_pct": float(avg_pct),
            "likes": int(likes), "subs_gained": int(subs),
            "publish_at": entry.get("publish_at"),
            "duration_s": extra.get("duration_s"),
            "source_name": extra.get("source_name"),
        })
    return videos


def _enough_data(videos: list[dict]) -> bool:
    total_views = sum(v["views"] for v in videos)
    return len(videos) >= MIN_CLIPS_FOR_INSIGHTS and total_views >= MIN_VIEWS_FOR_ANALYTICS


def _insufficient_data_insight(videos: list[dict]) -> dict:
    return {
        "type": "insufficient_data",
        "message": ("Not enough data yet to make recommendations — check back once "
                   "you have more uploads and views."),
        "evidence": {"clip_count": len(videos),
                     "total_views": sum(v["views"] for v in videos),
                     "needs_clips": MIN_CLIPS_FOR_INSIGHTS,
                     "needs_views": MIN_VIEWS_FOR_ANALYTICS},
    }


def _topic_signal(videos: list[dict]) -> list[dict]:
    clusters: dict[str, list[dict]] = {}
    for v in videos:
        key = v.get("source_name") or _norm_title(v.get("title"))
        if not key:
            continue
        clusters.setdefault(key, []).append(v)

    overall_median = statistics.median(v["views"] for v in videos)
    if overall_median <= 0:
        return []

    out = []
    for label, members in clusters.items():
        if len(members) < _MIN_CLIPS_PER_CLUSTER:
            continue
        cluster_median = statistics.median(v["views"] for v in members)
        lift = (cluster_median / overall_median) - 1.0
        if lift < _TOPIC_OUTPERFORM_THRESHOLD:
            continue
        out.append({
            "type": "topic",
            "message": (f"Clips from \"{label}\" outperform your average by "
                       f"{round(lift * 100)}% — make more like this."),
            "evidence": {"cluster": label, "clip_count": len(members),
                         "cluster_median_views": round(cluster_median),
                         "overall_median_views": round(overall_median)},
        })
    out.sort(key=lambda r: r["evidence"]["cluster_median_views"], reverse=True)
    return out


def _length_bucket(duration_s: float) -> str | None:
    for lo, hi, label in _LENGTH_BUCKETS:
        if duration_s >= lo and (hi is None or duration_s < hi):
            return label
    return None


def _length_signal(videos: list[dict]) -> list[dict]:
    buckets: dict[str, list[dict]] = {}
    for v in videos:
        d = v.get("duration_s")
        if d is None:
            continue
        label = _length_bucket(float(d))
        if label:
            buckets.setdefault(label, []).append(v)

    qualifying = {label: members for label, members in buckets.items()
                 if len(members) >= _MIN_CLIPS_PER_BUCKET}
    if len(qualifying) < 2:
        return []

    stats = {label: statistics.mean(v["avg_view_pct"] for v in members)
             for label, members in qualifying.items()}
    best_label = max(stats, key=stats.get)
    best_mean = stats[best_label]
    others_mean = statistics.mean(v for k, v in stats.items() if k != best_label)
    if best_mean <= others_mean:
        return []

    return [{
        "type": "length",
        "message": (f"Clips {best_label} hold retention best — average "
                   f"{round(best_mean, 1)}% watched vs {round(others_mean, 1)}% "
                   "for other lengths."),
        "evidence": {"best_bucket": best_label, "best_bucket_avg_pct": round(best_mean, 1),
                     "best_bucket_clip_count": len(qualifying[best_label]),
                     "other_buckets_avg_pct": round(others_mean, 1),
                     "buckets": {label: {"clip_count": len(members),
                                        "avg_pct": round(stats[label], 1)}
                                for label, members in qualifying.items()}},
    }]


def _publish_hour_ist(publish_at: str | None) -> int | None:
    if not publish_at:
        return None
    try:
        return datetime.fromisoformat(publish_at).hour
    except ValueError:
        return None


def _timing_signal(videos: list[dict]) -> list[dict]:
    by_hour: dict[int, list[dict]] = {}
    for v in videos:
        hour = _publish_hour_ist(v.get("publish_at"))
        if hour is not None:
            by_hour.setdefault(hour, []).append(v)

    if len(by_hour) < _MIN_DISTINCT_HOURS:
        return []

    qualifying = {h: members for h, members in by_hour.items() if len(members) >= 2}
    if not qualifying:
        return []

    means = {h: statistics.mean(v["views"] for v in members)
             for h, members in qualifying.items()}
    best_hour = max(means, key=means.get)
    overall_mean = statistics.mean(v["views"] for v in videos)
    lift = (means[best_hour] / overall_mean - 1.0) if overall_mean > 0 else 0.0
    if lift <= 0:
        return []

    return [{
        "type": "timing",
        "message": (f"Clips published around {best_hour:02d}:00 IST get "
                   f"{round(lift * 100)}% more views than your average."),
        "evidence": {"hour_ist": best_hour, "clip_count": len(qualifying[best_hour]),
                     "hour_avg_views": round(means[best_hour]),
                     "overall_avg_views": round(overall_mean)},
        "action": {"kind": "apply_publish_slot", "hour": best_hour},
    }]


def _hook_signal(videos: list[dict]) -> list[dict]:
    if len(videos) < 4:
        return []
    views = [v["views"] for v in videos]
    pcts = [v["avg_view_pct"] for v in videos]
    median_views = statistics.median(views)
    views_p75 = statistics.quantiles(views, n=4)[2]
    pct_p75 = statistics.quantiles(pcts, n=4)[2]

    out = []
    weak = [v for v in videos
           if v["views"] > median_views and v["avg_view_pct"] < _WEAK_HOOK_AVG_PCT]
    for v in sorted(weak, key=lambda v: v["views"], reverse=True)[:3]:
        out.append({
            "type": "hook_weak",
            "message": (f"\"{v['title']}\" got {v['views']} views but only "
                       f"{round(v['avg_view_pct'], 1)}% average watch — the hook "
                       "likely isn't landing in the first couple seconds."),
            "evidence": {"key": v["key"], "views": v["views"],
                         "avg_view_pct": round(v["avg_view_pct"], 1),
                         "median_views": round(median_views)},
        })
    weak_keys = {v["key"] for v in weak}
    strong = [v for v in videos
             if v["key"] not in weak_keys
             and v["views"] >= views_p75 and v["avg_view_pct"] >= pct_p75]
    for v in sorted(strong, key=lambda v: v["views"], reverse=True)[:3]:
        out.append({
            "type": "hook_strong",
            "message": (f"\"{v['title']}\" is a strong reference: {v['views']} views "
                       f"at {round(v['avg_view_pct'], 1)}% average watch."),
            "evidence": {"key": v["key"], "views": v["views"],
                         "avg_view_pct": round(v["avg_view_pct"], 1),
                         "views_p75": round(views_p75), "avg_pct_p75": round(pct_p75, 1)},
        })
    return out


def recommend(videos: list[dict]) -> list[dict]:
    """Derive recommendations strictly from `videos` (the output of
    `join_rows`). Returns exactly one honesty-default insight when data is
    thin; otherwise the concatenation of every signal that found something,
    each carrying its own `evidence`."""
    if not _enough_data(videos):
        return [_insufficient_data_insight(videos)]
    return [
        *_topic_signal(videos),
        *_length_signal(videos),
        *_timing_signal(videos),
        *_hook_signal(videos),
    ]
