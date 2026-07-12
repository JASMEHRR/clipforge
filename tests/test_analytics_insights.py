"""analytics_insights tests — pure logic, synthetic fixtures, zero mocking
(no Google API surface exists in this module at all)."""
from __future__ import annotations

import analytics_insights as ai


def _video(key, title, views, avg_view_pct, likes=10, subs_gained=1,
          publish_at="2026-07-01T12:00:00+05:30", duration_s=40.0,
          source_name=None, video_id=None):
    return {"video_id": video_id or f"vid_{key}", "key": key, "title": title,
           "views": views, "avg_view_pct": avg_view_pct, "likes": likes,
           "subs_gained": subs_gained, "publish_at": publish_at,
           "duration_s": duration_s, "source_name": source_name}


# --------------------------------------------------------- insufficient ---

def test_insufficient_data_returns_honesty_default():
    videos = [_video("job/clip_00", "A", 10, 40), _video("job/clip_01", "B", 20, 40)]
    out = ai.recommend(videos)
    assert len(out) == 1
    assert out[0]["type"] == "insufficient_data"
    assert out[0]["evidence"]["clip_count"] == 2


# --------------------------------------------------------------- topic ---

def test_topic_signal_flags_outperforming_source_cluster():
    videos = [
        _video("job/clip_00", "Big moment 1", 500, 40, source_name="Interview A"),
        _video("job/clip_01", "Big moment 2", 520, 42, source_name="Interview A"),
        _video("job/clip_02", "Other 1", 100, 30, source_name="Interview B"),
        _video("job/clip_03", "Other 2", 110, 30, source_name="Interview C"),
        _video("job/clip_04", "Other 3", 120, 30, source_name="Interview D"),
    ]
    out = ai._topic_signal(videos)
    assert len(out) == 1
    assert out[0]["type"] == "topic"
    assert out[0]["evidence"]["cluster"] == "Interview A"
    assert out[0]["evidence"]["clip_count"] == 2


def test_topic_signal_ignores_singleton_clusters():
    videos = [
        _video("job/clip_00", "A", 500, 40, source_name="Source A"),
        _video("job/clip_01", "B", 100, 30, source_name="Source B"),
        _video("job/clip_02", "C", 110, 30, source_name="Source C"),
    ]
    assert ai._topic_signal(videos) == []


# -------------------------------------------------------------- length ---

def test_length_signal_picks_best_retention_bucket():
    short = [_video(f"job/s{i}", f"Short {i}", 100, pct, duration_s=20.0)
            for i, pct in enumerate([70, 72, 68])]
    long_ = [_video(f"job/l{i}", f"Long {i}", 100, pct, duration_s=75.0)
            for i, pct in enumerate([30, 32, 28])]
    out = ai._length_signal(short + long_)
    assert len(out) == 1
    assert out[0]["evidence"]["best_bucket"] == "under 30s"
    assert out[0]["evidence"]["best_bucket_avg_pct"] == 70.0


def test_length_signal_needs_two_qualifying_buckets():
    # only one bucket has >= 3 clips -> nothing to compare against
    only_bucket = [_video(f"job/s{i}", f"Short {i}", 100, 70, duration_s=20.0)
                  for i in range(3)]
    assert ai._length_signal(only_bucket) == []


# -------------------------------------------------------------- timing ---

def test_timing_signal_requires_spread_before_suggesting():
    # only 2 distinct hours -> below _MIN_DISTINCT_HOURS
    videos = [
        _video("job/a", "A", 100, 40, publish_at="2026-07-01T09:00:00+05:30"),
        _video("job/b", "B", 110, 40, publish_at="2026-07-02T09:00:00+05:30"),
        _video("job/c", "C", 500, 40, publish_at="2026-07-03T20:00:00+05:30"),
        _video("job/d", "D", 520, 40, publish_at="2026-07-04T20:00:00+05:30"),
    ]
    assert ai._timing_signal(videos) == []


def test_timing_signal_surfaces_best_hour_once_spread_exists():
    videos = [
        _video("job/a", "A", 100, 40, publish_at="2026-07-01T09:00:00+05:30"),
        _video("job/b", "B", 110, 40, publish_at="2026-07-02T09:00:00+05:30"),
        _video("job/c", "C", 90, 40, publish_at="2026-07-03T14:00:00+05:30"),
        _video("job/d", "D", 95, 40, publish_at="2026-07-04T14:00:00+05:30"),
        _video("job/e", "E", 500, 40, publish_at="2026-07-05T20:00:00+05:30"),
        _video("job/f", "F", 520, 40, publish_at="2026-07-06T20:00:00+05:30"),
    ]
    out = ai._timing_signal(videos)
    assert len(out) == 1
    assert out[0]["evidence"]["hour_ist"] == 20
    assert out[0]["action"] == {"kind": "apply_publish_slot", "hour": 20}


# ---------------------------------------------------------------- hook ---

def test_hook_signal_flags_weak_and_strong_examples():
    videos = [
        _video("job/v1", "Great hook", 1000, 60),
        _video("job/v2", "Also good", 900, 55),
        _video("job/v3", "Weak hook here", 800, 20),   # high views, low retention
        _video("job/v4", "Mid 1", 700, 45),
        _video("job/v5", "Mid 2", 600, 40),
        _video("job/v6", "Mid 3", 500, 35),
        _video("job/v7", "Mid 4", 400, 30),
        _video("job/v8", "Mid 5", 300, 25),
    ]
    out = ai._hook_signal(videos)
    weak_keys = {r["evidence"]["key"] for r in out if r["type"] == "hook_weak"}
    strong_keys = {r["evidence"]["key"] for r in out if r["type"] == "hook_strong"}
    assert "job/v3" in weak_keys
    assert "job/v1" in strong_keys


def test_hook_signal_weak_and_strong_are_mutually_exclusive():
    # a low-retention channel where the 75th-percentile avg% falls below the
    # absolute "weak" floor (30) — before the fix, a high-views/low-pct clip
    # could satisfy both the absolute weak threshold and the relative strong
    # one, producing two contradictory recommendations for the same clip.
    videos = [
        _video("job/v1", "Top clip", 1000, 26),
        _video("job/v2", "B", 300, 20),
        _video("job/v3", "C", 200, 18),
        _video("job/v4", "D", 150, 15),
    ]
    out = ai._hook_signal(videos)
    keys_by_type: dict[str, set] = {}
    for r in out:
        keys_by_type.setdefault(r["type"], set()).add(r["evidence"]["key"])
    overlap = keys_by_type.get("hook_weak", set()) & keys_by_type.get("hook_strong", set())
    assert not overlap


# ------------------------------------------------------------- join_rows --

def test_join_rows_matches_video_id_to_upload_log_key():
    analytics_rows = [
        ("YT123", 500, 120.0, 45.5, 20, 3),
        ("YT999", 10, 1.0, 10.0, 0, 0),   # not in the upload log -> dropped
    ]
    upload_log = {"uploads": {
        "output/job1/clip_00": {"video_id": "YT123", "title": "Best clip",
                                "publish_at": "2026-07-01T18:00:00+05:30"},
    }}
    videos = ai.join_rows(analytics_rows, upload_log,
                          clip_extra_fn=lambda key: {"duration_s": 42.0,
                                                    "source_name": "Podcast 1"})
    assert len(videos) == 1
    v = videos[0]
    assert v["key"] == "output/job1/clip_00"
    assert v["video_id"] == "YT123"
    assert v["title"] == "Best clip"
    assert v["views"] == 500
    assert v["avg_view_pct"] == 45.5
    assert v["duration_s"] == 42.0
    assert v["source_name"] == "Podcast 1"


def test_join_rows_without_clip_extra_fn_leaves_extras_none():
    analytics_rows = [("YT1", 50, 5.0, 30.0, 1, 0)]
    upload_log = {"uploads": {"output/job1/clip_00": {"video_id": "YT1", "title": "T"}}}
    videos = ai.join_rows(analytics_rows, upload_log)
    assert videos[0]["duration_s"] is None
    assert videos[0]["source_name"] is None


# ---------------------------------------------------------- evidence rule --

def test_every_recommendation_carries_evidence_numbers():
    videos = [
        _video("job/a", "A", 500, 40, source_name="X", publish_at="2026-07-01T09:00:00+05:30",
              duration_s=20.0),
        _video("job/b", "B", 520, 42, source_name="X", publish_at="2026-07-02T09:00:00+05:30",
              duration_s=22.0),
        _video("job/c", "C", 100, 30, source_name="Y", publish_at="2026-07-03T14:00:00+05:30",
              duration_s=75.0),
        _video("job/d", "D", 110, 32, source_name="Z", publish_at="2026-07-04T14:00:00+05:30",
              duration_s=78.0),
        _video("job/e", "E", 120, 20, source_name="W", publish_at="2026-07-05T20:00:00+05:30",
              duration_s=80.0),
        _video("job/f", "F", 900, 65, source_name="V", publish_at="2026-07-06T20:00:00+05:30",
              duration_s=25.0),
    ]
    out = ai.recommend(videos)
    assert out  # something was found
    for rec in out:
        assert rec.get("evidence"), f"{rec['type']} is missing evidence"
