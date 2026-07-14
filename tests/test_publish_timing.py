"""publish_timing.py — hour scoring, exploration, data gates, recency
weighting, and the config mutations, all offline (no real Analytics API)."""
import json
import random
from datetime import datetime, timedelta

import pytest

import publish_timing as pt

IST = pt.IST


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(pt, "STATS_FILE", tmp_path / "publish_timing_stats.json")
    return tmp_path


def _video(hour, views_24h, days_ago=0, video_id=None, publish_date=None):
    now = datetime.now(IST)
    published = (publish_date or (now - timedelta(days=days_ago))).replace(
        hour=hour, minute=0, second=0, microsecond=0)
    return {"video_id": video_id or f"v{hour}-{days_ago}",
           "publish_at": published.isoformat(), "hour_ist": hour,
           "views_24h": views_24h, "views_72h": None, "impressions": None,
           "ctr": None, "avg_view_pct": None, "virality_score": None, "niche": None}


def _cfg(**pt_overrides):
    return {"upload": {"max_per_day": 5, "publish_slots_ist": [12, 19],
                       "publish_timing": pt_overrides}}


def _log_with_n_uploads(n):
    return {"uploads": {f"output/job{i}/clip_00": {"video_id": f"v{i}",
                                                    "publish_at": "2026-07-01T12:00:00+05:30"}
                        for i in range(n)}}


# ============================================================
# Hour scoring
# ============================================================
def test_score_hours_normalizes_against_median_and_counts_samples():
    videos = [_video(12, 100), _video(12, 300), _video(18, 200)]
    scored = pt.score_hours(videos, now=datetime.now(IST))
    # median of [100, 300, 200] = 200
    assert scored[12]["sample_count"] == 2
    assert scored[18]["sample_count"] == 1
    # hour 12's two videos normalize to 0.5 and 1.5 -> mean 1.0 (equal recency weight)
    assert scored[12]["score"] == pytest.approx(1.0, abs=0.01)
    assert scored[18]["score"] == pytest.approx(1.0, abs=0.01)  # 200/200


def test_score_hours_one_viral_outlier_does_not_poison_other_hours():
    videos = [_video(12, 100), _video(12, 120), _video(18, 100000)]
    scored = pt.score_hours(videos, now=datetime.now(IST))
    # the viral hour scores itself very high, but hour 12's score stays near
    # its own local performance, not dragged toward the outlier
    assert scored[12]["score"] < 1.0
    assert scored[18]["score"] > 100


def test_score_hours_ignores_videos_without_a_captured_24h_window():
    videos = [_video(12, 100), {**_video(18, None), "views_24h": None}]
    scored = pt.score_hours(videos, now=datetime.now(IST))
    assert set(scored.keys()) == {12}


def test_score_hours_empty_input_returns_empty():
    assert pt.score_hours([]) == {}


# ============================================================
# Recency weighting
# ============================================================
def test_recency_weight_half_life():
    now = datetime.now(IST)
    fresh = (now - timedelta(days=0)).isoformat()
    half_life_ago = (now - timedelta(days=30)).isoformat()
    two_half_lives_ago = (now - timedelta(days=60)).isoformat()
    assert pt._recency_weight(fresh, now, 30) == pytest.approx(1.0, abs=0.01)
    assert pt._recency_weight(half_life_ago, now, 30) == pytest.approx(0.5, abs=0.02)
    assert pt._recency_weight(two_half_lives_ago, now, 30) == pytest.approx(0.25, abs=0.02)


def test_recency_weight_invalid_input_is_zero():
    now = datetime.now(IST)
    assert pt._recency_weight("not a date", now, 30) == 0.0
    assert pt._recency_weight(None, now, 30) == 0.0


def test_score_hours_recent_videos_outweigh_old_ones_at_the_same_hour():
    now = datetime.now(IST)
    videos = [_video(12, 1000, days_ago=0), _video(12, 10, days_ago=120)]
    scored = pt.score_hours(videos, half_life_days=30, now=now)
    # median = 505 -> norm_recent = 1.98 (weight ~1.0), norm_old = 0.02
    # (weight 0.5**4 = 0.0625). A naive unweighted mean would land at 1.0;
    # recency weighting should pull it much closer to the recent video's own
    # normalized score than to that simple average.
    assert scored[12]["score"] > 1.5


# ============================================================
# Data gates
# ============================================================
def test_pick_hours_below_total_upload_gate_returns_none(tmp_path):
    pt.save_stats({"videos": {v["video_id"]: v for v in
                              [_video(h, 500) for h in range(12, 12 + 5)]},
                  "changelog": [], "active_ranking": None})
    cfg = _cfg(enabled=True, min_total_uploads=15)
    result = pt.pick_hours(cfg, _log_with_n_uploads(5), count=1)
    assert result is None


def test_pick_hours_disabled_returns_none_even_with_lots_of_data():
    pt.save_stats({"videos": {v["video_id"]: v for v in
                              [_video(12, 500) for _ in range(10)]},
                  "changelog": [], "active_ranking": None})
    cfg = _cfg(enabled=False, min_total_uploads=5)
    result = pt.pick_hours(cfg, _log_with_n_uploads(20), count=1)
    assert result is None


def test_pick_hours_gate_passed_but_no_scored_hours_returns_none():
    pt.save_stats({"videos": {}, "changelog": [], "active_ranking": None})
    cfg = _cfg(enabled=True, min_total_uploads=5)
    result = pt.pick_hours(cfg, _log_with_n_uploads(10), count=1, rng=random.Random(1))
    assert result is None


def test_pick_hours_returns_best_hours_once_gates_pass():
    videos = ([_video(12, 500, video_id=f"a{i}") for i in range(3)]
             + [_video(18, 100, video_id=f"b{i}") for i in range(3)])
    pt.save_stats({"videos": {v["video_id"]: v for v in videos},
                  "changelog": [], "active_ranking": None})
    cfg = _cfg(enabled=True, min_total_uploads=5, min_hour_samples=3,
              explore_epsilon_start=0.0, explore_epsilon_floor=0.0)
    result = pt.pick_hours(cfg, _log_with_n_uploads(10), count=1, rng=random.Random(1))
    assert result == [12]   # 500 views/clip beats 100 views/clip


# ============================================================
# Exploration
# ============================================================
def test_epsilon_decays_with_total_uploads_and_floors():
    cfg = {}
    assert pt._epsilon(0, cfg) == pytest.approx(0.3)
    assert pt._epsilon(15, cfg) == pytest.approx(0.15)
    assert pt._epsilon(100, cfg) == pytest.approx(0.05)  # floored, not negative


class _AlwaysExplore(random.Random):
    def random(self):
        return 0.0


class _NeverExplore(random.Random):
    def random(self):
        return 1.0


def test_pick_hours_explores_an_under_sampled_hour_when_epsilon_rolls_true():
    videos = [_video(12, 500, video_id=f"a{i}") for i in range(5)]  # well-trusted hour
    pt.save_stats({"videos": {v["video_id"]: v for v in videos},
                  "changelog": [], "active_ranking": None})
    cfg = _cfg(enabled=True, min_total_uploads=5, min_hour_samples=3,
              explore_window_start=8, explore_window_end=23)
    result = pt.pick_hours(cfg, _log_with_n_uploads(10), count=1, rng=_AlwaysExplore())
    # forced exploration swaps the single slot for an under-sampled hour —
    # hour 12 already has 5 samples (>= min_hour_samples), so it's not
    # under-sampled and must not be the result
    assert result != [12]
    assert 8 <= result[0] <= 23


def test_pick_hours_stays_on_best_hour_when_epsilon_rolls_false():
    videos = [_video(12, 500, video_id=f"a{i}") for i in range(5)]
    pt.save_stats({"videos": {v["video_id"]: v for v in videos},
                  "changelog": [], "active_ranking": None})
    cfg = _cfg(enabled=True, min_total_uploads=5, min_hour_samples=3)
    result = pt.pick_hours(cfg, _log_with_n_uploads(10), count=1, rng=_NeverExplore())
    assert result == [12]


# ============================================================
# Pin / ban precedence
# ============================================================
def test_banned_hour_never_picked_even_as_top_scorer():
    videos = [_video(12, 500, video_id=f"a{i}") for i in range(5)]
    pt.save_stats({"videos": {v["video_id"]: v for v in videos},
                  "changelog": [], "active_ranking": None})
    cfg = _cfg(enabled=True, min_total_uploads=5, min_hour_samples=3,
              banned_hours=[12], explore_epsilon_start=0.0, explore_epsilon_floor=0.0)
    result = pt.pick_hours(cfg, _log_with_n_uploads(10), count=1, rng=random.Random(1))
    assert result is None or 12 not in result


def test_pinned_hour_always_included_even_unscored():
    videos = [_video(18, 500, video_id=f"a{i}") for i in range(5)]
    pt.save_stats({"videos": {v["video_id"]: v for v in videos},
                  "changelog": [], "active_ranking": None})
    cfg = _cfg(enabled=True, min_total_uploads=5, min_hour_samples=3,
              pinned_hours=[9], explore_epsilon_start=0.0, explore_epsilon_floor=0.0)
    result = pt.pick_hours(cfg, _log_with_n_uploads(10), count=2, rng=random.Random(1))
    assert 9 in result


# ============================================================
# Config-slot / override precedence (Part 4)
# ============================================================
def test_get_peak_hours_returns_none_without_cfg():
    import upload_scheduler as sched
    assert sched.get_peak_hours(None, {"uploads": {}}, 1) is None


def test_next_publish_times_falls_back_to_config_slots_below_gate():
    import upload_scheduler as sched
    cfg = _cfg(enabled=True, min_total_uploads=15)
    log_data = {"uploads": {}}
    times = sched.next_publish_times(1, None, log_data, [17], cfg=cfg)
    assert times[0].hour == 17   # config floor, not overridden — no data at all


# ============================================================
# Daily tweak loop / panel state
# ============================================================
def test_recompute_ranking_logs_a_changelog_entry_on_first_run():
    videos = [_video(12, 500, video_id=f"a{i}") for i in range(3)]
    pt.save_stats({"videos": {v["video_id"]: v for v in videos},
                  "changelog": [], "active_ranking": None})
    cfg = _cfg(enabled=True, min_total_uploads=3, min_hour_samples=3)
    state = pt.recompute_ranking(cfg, _log_with_n_uploads(3))
    assert state["gates_passed"] is True
    assert state["active_hours"] == [12]
    assert len(state["changelog"]) == 1
    assert "12:00" in state["changelog"][0]["message"]


def test_recompute_ranking_no_new_entry_when_nothing_changed():
    videos = [_video(12, 500, video_id=f"a{i}") for i in range(3)]
    pt.save_stats({"videos": {v["video_id"]: v for v in videos},
                  "changelog": [], "active_ranking": None})
    cfg = _cfg(enabled=True, min_total_uploads=3, min_hour_samples=3)
    log_data = _log_with_n_uploads(3)
    pt.recompute_ranking(cfg, log_data)
    state = pt.recompute_ranking(cfg, log_data)
    assert len(state["changelog"]) == 1  # still just the one entry


def test_publish_timing_state_reports_not_enough_data_honestly():
    state = pt.publish_timing_state(_cfg(enabled=True, min_total_uploads=15),
                                    {"uploads": {}})
    assert state["gates_passed"] is False
    assert state["using_learned_hours"] is False
    assert state["total_uploads"] == 0 and state["min_total_uploads"] == 15
    assert "not" in state["note"].lower() or "own" in state["note"].lower()
    assert "audience" in state["note"].lower()  # the honesty disclaimer is present


# ============================================================
# Stats store persistence
# ============================================================
def test_save_and_load_stats_round_trip():
    pt.save_stats({"videos": {"v1": _video(12, 100)}, "changelog": [], "active_ranking": None})
    loaded = pt.load_stats()
    assert loaded["videos"]["v1"]["views_24h"] == 100


def test_load_stats_recovers_from_corrupt_file(tmp_path):
    pt.STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
    pt.STATS_FILE.write_text("{not json", encoding="utf-8")
    loaded = pt.load_stats()
    assert loaded == {"videos": {}, "changelog": [], "active_ranking": None}
    assert pt.STATS_FILE.with_suffix(".json.corrupt").exists()


def test_load_stats_survives_a_preexisting_corrupt_backup(tmp_path):
    pt.STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
    pt.STATS_FILE.with_suffix(".json.corrupt").write_text("stale", encoding="utf-8")
    pt.STATS_FILE.write_text("{not json", encoding="utf-8")
    loaded = pt.load_stats()  # must not raise FileExistsError
    assert loaded == {"videos": {}, "changelog": [], "active_ranking": None}


def test_reset_stats_clears_videos_and_changelog():
    pt.save_stats({"videos": {"v1": _video(12, 100)},
                  "changelog": [{"at": "x", "message": "y", "hours": [12]}],
                  "active_ranking": {"hours": [12]}})
    pt.reset_stats()
    loaded = pt.load_stats()
    assert loaded == {"videos": {}, "changelog": [], "active_ranking": None}


# ============================================================
# Owner controls (config mutation)
# ============================================================
def test_toggle_pin_and_ban_are_mutually_exclusive(monkeypatch, tmp_path):
    import config as config_mod
    monkeypatch.setattr(config_mod, "LOCAL_PATH", tmp_path / "config.local.yaml")
    config_mod._cached = None

    pinned = pt.toggle_pin({}, 12)
    assert pinned == [12]
    cfg_now = config_mod.load_config()
    assert cfg_now["upload"]["publish_timing"]["pinned_hours"] == [12]

    banned = pt.toggle_ban({}, 12)
    assert banned == [12]
    cfg_now = config_mod.load_config()
    assert cfg_now["upload"]["publish_timing"]["pinned_hours"] == []  # ban un-pins
    assert cfg_now["upload"]["publish_timing"]["banned_hours"] == [12]

    unpinned = pt.toggle_ban({}, 12)  # toggling again un-bans
    assert unpinned == []
    config_mod._cached = None


def test_toggle_hour_rejects_out_of_range(tmp_path, monkeypatch):
    import config as config_mod
    monkeypatch.setattr(config_mod, "LOCAL_PATH", tmp_path / "config.local.yaml")
    config_mod._cached = None
    with pytest.raises(ValueError):
        pt.toggle_pin({}, 24)
    config_mod._cached = None


def test_set_enabled_round_trip(tmp_path, monkeypatch):
    import config as config_mod
    monkeypatch.setattr(config_mod, "LOCAL_PATH", tmp_path / "config.local.yaml")
    config_mod._cached = None
    pt.set_enabled(False)
    assert config_mod.load_config()["upload"]["publish_timing"]["enabled"] is False
    config_mod._cached = None


# ============================================================
# Fetch layer (mocked Analytics API)
# ============================================================
def test_refresh_stats_captures_closed_windows_and_freezes_them(tmp_path, monkeypatch):
    import upload_scheduler as sched
    from unittest.mock import MagicMock
    monkeypatch.setattr(sched, "LOG_FILE", tmp_path / "upload_log.json")
    published = (datetime.now(IST) - timedelta(days=5)).isoformat()
    sched.save_log({"uploads": {"output/job1/clip_00": {
        "video_id": "vidX", "publish_at": published, "virality_score": 50}}})

    analytics = MagicMock()
    analytics.reports.return_value.query.return_value.execute.return_value = {
        "rows": [[123, 900, 4.1, 42.0]]}

    stats = pt.refresh_stats(analytics=analytics)
    assert stats["videos"]["vidX"]["views_24h"] == 123
    assert stats["videos"]["vidX"]["views_72h"] == 123  # both windows closed (5 days old)

    # a second call must not refetch an already-captured window
    call_count_before = analytics.reports.call_count
    pt.refresh_stats(analytics=analytics)
    assert analytics.reports.call_count == call_count_before


def test_refresh_stats_skips_videos_whose_window_hasnt_closed(tmp_path, monkeypatch):
    import upload_scheduler as sched
    from unittest.mock import MagicMock
    monkeypatch.setattr(sched, "LOG_FILE", tmp_path / "upload_log.json")
    published = datetime.now(IST).isoformat()  # published today — window not closed
    sched.save_log({"uploads": {"output/job1/clip_00": {
        "video_id": "vidY", "publish_at": published}}})

    analytics = MagicMock()
    stats = pt.refresh_stats(analytics=analytics)
    assert stats["videos"]["vidY"]["views_24h"] is None
    analytics.reports.assert_not_called()


def test_refresh_stats_one_video_failure_does_not_block_the_rest(tmp_path, monkeypatch):
    import upload_scheduler as sched
    from unittest.mock import MagicMock
    monkeypatch.setattr(sched, "LOG_FILE", tmp_path / "upload_log.json")
    old = (datetime.now(IST) - timedelta(days=5)).isoformat()
    sched.save_log({"uploads": {
        "output/job1/clip_00": {"video_id": "bad", "publish_at": old},
        "output/job2/clip_00": {"video_id": "good", "publish_at": old},
    }})

    analytics = MagicMock()
    def query_side_effect(**kwargs):
        m = MagicMock()
        if "bad" in kwargs.get("filters", ""):
            m.execute.side_effect = Exception("boom")
        else:
            m.execute.return_value = {"rows": [[50, 10, 1.0, 20.0]]}
        return m
    analytics.reports.return_value.query.side_effect = query_side_effect

    stats = pt.refresh_stats(analytics=analytics)
    assert stats["videos"]["good"]["views_24h"] == 50
    assert stats["videos"]["bad"]["views_24h"] is None
