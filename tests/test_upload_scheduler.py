"""upload_scheduler tests — all Google API calls mocked, no network."""
import json
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

import archive
import upload_scheduler as sched
from errors import UploadError


def _write_clip(output_dir, job, clip, score, uploaded=False, exclude=False):
    clip_dir = output_dir / job / clip
    clip_dir.mkdir(parents=True)
    (clip_dir / "final.mp4").write_bytes(b"\x00" * 10)
    meta = {
        "title": f"Title {clip}", "description": "Desc.",
        "hashtags": ["#a", "#b"], "virality": {"score": score},
    }
    if exclude:
        meta["upload"] = {"exclude": True}
    (clip_dir / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    return clip_dir


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    log_file = tmp_path / "cache" / "upload_log.json"
    monkeypatch.setattr(sched, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(sched, "LOG_FILE", log_file)
    monkeypatch.setattr(sched, "QUEUE_FILE", tmp_path / "cache" / "post_queue.json")
    monkeypatch.setattr(sched, "ROOT", tmp_path)
    # a real upload's archive copy must never land under the real repo's
    # archive/uploaded/ during tests
    monkeypatch.setattr(archive, "ARCHIVE_DIR", tmp_path / "archive" / "uploaded")
    return output_dir


CFG = {"upload": {"min_virality": 40, "max_per_day": 3, "max_per_run": 2,
                  "publish_slots_ist": [12, 19]}}


def test_find_candidates_filters_low_virality_and_dedupes(_isolate):
    output_dir = _isolate
    _write_clip(output_dir, "job1", "clip_01", score=80)
    _write_clip(output_dir, "job1", "clip_02", score=10)  # below min_virality
    log_data = {"uploads": {}}
    candidates = sched.find_candidates(CFG, log_data)
    assert len(candidates) == 1
    assert candidates[0]["score"] == 80

    key = candidates[0]["key"]
    log_data["uploads"][key] = {"video_id": "x"}
    candidates2 = sched.find_candidates(CFG, log_data)
    assert candidates2 == []


def _write_meta(output_dir, job, clip, score, title, src=None, start=None, end=None):
    clip_dir = output_dir / job / clip
    clip_dir.mkdir(parents=True)
    (clip_dir / "final.mp4").write_bytes(b"\x00" * 10)
    meta = {"title": title, "description": "Desc.", "hashtags": ["#a", "#b"],
            "virality": {"score": score}}
    if src is not None:
        meta.update({"source_name": src, "original_source_start_s": start,
                     "original_source_end_s": end})
    (clip_dir / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")


def test_dedupe_same_title_across_jobs(_isolate):
    output_dir = _isolate
    _write_meta(output_dir, "job_a", "clip_00", 88, "Best moment ever")
    _write_meta(output_dir, "job_b", "clip_03", 88, "Best moment ever!")  # same title
    _write_meta(output_dir, "job_c", "clip_01", 70, "A different clip")
    cands = sched.find_candidates(CFG, {"uploads": {}})
    # the two identical-title clips collapse to one uploadable entry
    assert len(cands) == 2
    winner = next(c for c in cands if "moment" in c["meta"]["title"].lower())
    assert winner["duplicates"] and len(winner["duplicates"]) == 1
    # the collapsed duplicate is not itself an uploadable candidate
    keys = {c["key"] for c in cands}
    assert winner["duplicates"][0] not in keys


def test_dedupe_same_source_window(_isolate):
    output_dir = _isolate
    _write_meta(output_dir, "job_a", "clip_00", 90, "Title one",
                src="talk.mp4", start=10.0, end=40.0)
    _write_meta(output_dir, "job_b", "clip_00", 80, "Title two",
                src="talk.mp4", start=12.0, end=42.0)  # ~87% overlap, same source
    _write_meta(output_dir, "job_c", "clip_00", 75, "Title three",
                src="talk.mp4", start=200.0, end=230.0)  # different window
    cands = sched.find_candidates(CFG, {"uploads": {}})
    assert len(cands) == 2                         # the overlapping pair collapses
    assert cands[0]["score"] == 90 and cands[0]["duplicates"]  # highest kept


def test_find_candidates_respects_per_clip_exclude(_isolate):
    output_dir = _isolate
    _write_clip(output_dir, "job1", "clip_01", score=80, exclude=True)
    _write_clip(output_dir, "job1", "clip_02", score=70)
    candidates = sched.find_candidates(CFG, {"uploads": {}})
    assert [c["score"] for c in candidates] == [70]


def test_panel_state_counts_and_urls():
    now = datetime.now(sched.IST)
    log_data = {"uploads": {
        "a": {"uploaded_at": now.isoformat(), "video_id": "vid1",
              "title": "First", "publish_at": now.isoformat()},
        "b": {"uploaded_at": (now - timedelta(days=2)).isoformat(),
              "video_id": "vid2", "title": "Old",
              "publish_at": now.isoformat()},
    }}
    cfg = {"upload": {"auto_enabled": True, "max_per_day": 3,
                      "publish_slots_ist": [12, 19]}}
    st = sched.panel_state(cfg, log_data, authorized=True)
    assert st["auto_enabled"] is True
    assert st["authorized"] is True
    assert st["uploads_today"] == 1
    assert st["max_per_day"] == 3
    assert st["next_slot_ist"] is not None
    assert datetime.fromisoformat(st["next_slot_ist"]) > now
    assert st["recent"][0]["url"] == "https://youtu.be/vid1"
    assert st["recent"][0]["title"] == "First"  # newest first
    assert len(st["recent"]) == 2


def test_panel_state_unauthorized_or_disabled_has_no_slot():
    log_data = {"uploads": {}}
    cfg = {"upload": {"auto_enabled": True}}
    assert sched.panel_state(cfg, log_data, authorized=False)["next_slot_ist"] is None
    cfg2 = {"upload": {"auto_enabled": False}}
    assert sched.panel_state(cfg2, log_data, authorized=True)["next_slot_ist"] is None


def test_panel_state_cap_reached_has_no_slot():
    now = datetime.now(sched.IST)
    log_data = {"uploads": {
        k: {"uploaded_at": now.isoformat(), "video_id": k}
        for k in ("a", "b", "c")}}
    cfg = {"upload": {"auto_enabled": True, "max_per_day": 3}}
    assert sched.panel_state(cfg, log_data, authorized=True)["next_slot_ist"] is None


def test_clean_hashtags_filters_junk_and_keeps_shorts():
    tags = sched.clean_hashtags(["#really", "#Cats", "#ok", "#dogs"])
    assert tags == ["#cats", "#dogs", "#shorts"]  # "really"/"ok" dropped (junk/too short)


def test_clean_hashtags_always_appends_shorts_once():
    tags = sched.clean_hashtags(["#shorts", "#cats"])
    assert tags.count("#shorts") == 1


def test_next_publish_times_no_collision_and_not_in_past():
    log_data = {"uploads": {}}
    analytics = MagicMock()
    analytics.reports.return_value.query.return_value.execute.return_value = {"rows": []}
    times = sched.next_publish_times(3, analytics, log_data, [12, 19])
    assert len(times) == 3
    now = datetime.now(sched.IST)
    for t in times:
        assert t > now
    times_sorted = sorted(times)
    for a, b in zip(times_sorted, times_sorted[1:]):
        assert (b - a) >= timedelta(minutes=60)  # default spacing, no two slots collide


def test_next_publish_times_single_hour_reaches_max_per_day():
    # regression: publish_slots_ist with ONE hour used to cap at 1 slot/day
    # forever, no matter how high max_per_day was set (the reported bug).
    # Not asserting same-day placement here: how many of the 5 land today
    # vs spill to tomorrow legitimately depends on the wall-clock hour the
    # test happens to run at (fewer hours remain before midnight late in
    # the evening) — that's not the bug. The bug was being stuck at 1
    # slot/day forever regardless of max_per_day; `len(times) == 5` is
    # the actual regression check.
    log_data = {"uploads": {}}
    analytics = MagicMock()
    analytics.reports.return_value.query.return_value.execute.return_value = {"rows": []}
    times = sched.next_publish_times(5, analytics, log_data, [17])
    assert len(times) == 5
    now = datetime.now(sched.IST)
    for t in times:
        assert t > now + timedelta(minutes=30)
    times_sorted = sorted(times)
    for a, b in zip(times_sorted, times_sorted[1:]):
        assert (b - a) >= timedelta(minutes=60)


def test_next_publish_times_respects_custom_spacing():
    log_data = {"uploads": {}}
    analytics = MagicMock()
    analytics.reports.return_value.query.return_value.execute.return_value = {"rows": []}
    times = sched.next_publish_times(3, analytics, log_data, [17],
                                     slot_spacing_minutes=90)
    times_sorted = sorted(times)
    for a, b in zip(times_sorted, times_sorted[1:]):
        assert (b - a) >= timedelta(minutes=90)


def test_next_publish_times_skips_slot_too_close_to_now():
    # the reported scenario: single configured hour whose only chance today
    # falls inside the 30-min safety buffer must still fill the REST of
    # today from later spaced slots, not jump straight to tomorrow.
    log_data = {"uploads": {}}
    analytics = MagicMock()
    analytics.reports.return_value.query.return_value.execute.return_value = {"rows": []}
    now = datetime.now(sched.IST)
    near_hour = (now + timedelta(minutes=20)).hour
    times = sched.next_publish_times(1, analytics, log_data, [near_hour])
    assert len(times) == 1
    assert times[0] > now + timedelta(minutes=30)
    if now.hour < 23:  # avoid the rare midnight-wrap edge in this assertion
        assert times[0].date() == now.date()


def test_uploads_today_counts_only_today(_isolate):
    now = datetime.now(sched.IST)
    yesterday = now - timedelta(days=1)
    log_data = {"uploads": {
        "a": {"uploaded_at": now.isoformat()},
        "b": {"uploaded_at": yesterday.isoformat()},
    }}
    assert sched.uploads_today(log_data) == 1


def test_load_log_recovers_from_corrupt_file(_isolate, tmp_path):
    sched.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    sched.LOG_FILE.write_text("{not json", encoding="utf-8")
    log_data = sched.load_log()
    assert log_data == {"uploads": {}}
    assert sched.LOG_FILE.with_suffix(".json.corrupt").exists()


def test_save_log_is_atomic(_isolate):
    log_data = {"uploads": {"k": {"video_id": "v"}}}
    sched.save_log(log_data)
    assert sched.LOG_FILE.exists()
    assert not sched.LOG_FILE.with_suffix(".json.tmp").exists()
    assert json.loads(sched.LOG_FILE.read_text(encoding="utf-8")) == log_data


def test_trigger_after_render_noop_when_disabled(_isolate, monkeypatch):
    called = []
    monkeypatch.setattr("youtube_upload.credentials_available", lambda: True)
    monkeypatch.setattr("youtube_upload.has_cached_token", lambda: True)
    monkeypatch.setattr(sched, "upload_batch", lambda *a, **k: called.append(1))
    sched.trigger_after_render(_isolate / "job1" / "clip_01", {"upload": {"auto_enabled": False}})
    assert called == []


def test_trigger_after_render_noop_when_not_authorized(_isolate, monkeypatch):
    called = []
    monkeypatch.setattr("youtube_upload.credentials_available", lambda: False)
    monkeypatch.setattr(sched, "upload_batch", lambda *a, **k: called.append(1))
    sched.trigger_after_render(_isolate / "job1" / "clip_01", {"upload": {"auto_enabled": True}})
    assert called == []


def test_trigger_after_render_noop_when_cap_reached(_isolate, monkeypatch):
    called = []
    monkeypatch.setattr("youtube_upload.credentials_available", lambda: True)
    monkeypatch.setattr("youtube_upload.has_cached_token", lambda: True)
    monkeypatch.setattr(sched, "upload_batch", lambda *a, **k: called.append(1))
    now = datetime.now(sched.IST)
    sched.save_log({"uploads": {"a": {"uploaded_at": now.isoformat()},
                                "b": {"uploaded_at": now.isoformat()},
                                "c": {"uploaded_at": now.isoformat()}}})
    sched.trigger_after_render(_isolate / "job1" / "clip_01",
                               {"upload": {"auto_enabled": True, "max_per_day": 3}})
    assert called == []


# ---------------------------------------------------------- upload now ---

def test_select_candidates_top_n_by_score(_isolate):
    output_dir = _isolate
    _write_clip(output_dir, "job1", "clip_00", score=90)
    _write_clip(output_dir, "job1", "clip_01", score=70)
    _write_clip(output_dir, "job1", "clip_02", score=50)
    candidates = sched.find_candidates(CFG, {"uploads": {}})
    top2 = sched.select_candidates(candidates, "top", count=2)
    assert [c["score"] for c in top2] == [90, 70]

    assert sched.select_candidates(candidates, "top", count=0) == []
    assert len(sched.select_candidates(candidates, "top", count=99)) == 3


def test_select_candidates_manual_keys(_isolate):
    output_dir = _isolate
    d1 = _write_clip(output_dir, "job1", "clip_00", score=90)
    _write_clip(output_dir, "job1", "clip_01", score=70)
    candidates = sched.find_candidates(CFG, {"uploads": {}})
    key0 = str(d1.relative_to(_isolate.parent)).replace("\\", "/")
    picked = sched.select_candidates(candidates, "manual", keys=[key0])
    assert len(picked) == 1 and picked[0]["key"] == key0

    # unknown/stale keys are dropped silently, not errored
    assert sched.select_candidates(candidates, "manual", keys=["nope"]) == []
    assert sched.select_candidates(candidates, "manual", keys=[]) == []


def test_cap_warning_only_when_over_max_per_day():
    cfg = {"upload": {"max_per_day": 3}}
    assert sched.cap_warning(cfg, {"uploads": {}}, requested_count=3) is None
    warning = sched.cap_warning(cfg, {"uploads": {}}, requested_count=5)
    assert warning is not None
    assert "3" in warning and "2" in warning  # limit and overflow count


def test_cap_warning_counts_todays_uploads_already_sent():
    now = datetime.now(sched.IST)
    cfg = {"upload": {"max_per_day": 3}}
    log_data = {"uploads": {"a": {"uploaded_at": now.isoformat()}}}
    # 1 already sent today + 3 requested = 4, one over the cap of 3
    warning = sched.cap_warning(cfg, log_data, requested_count=3)
    assert warning is not None and "1" in warning


def test_upload_now_publishes_public_with_no_publish_at(_isolate, monkeypatch):
    output_dir = _isolate
    _write_clip(output_dir, "job1", "clip_00", score=90)
    candidates = sched.find_candidates(CFG, {"uploads": {}})

    calls = []

    def fake_upload_clip(video, snippet, privacy="private", service=None,
                         publish_at=None, category_id=None):
        calls.append({"privacy": privacy, "publish_at": publish_at})
        return {"video_id": "vid1", "url": "https://youtu.be/vid1"}

    monkeypatch.setattr(sched.youtube_upload, "upload_clip", fake_upload_clip)
    log_data = {"uploads": {}}
    results = sched.upload_now(object(), CFG, log_data, candidates)

    assert len(calls) == 1
    assert calls[0]["privacy"] == "public"
    assert calls[0]["publish_at"] is None
    assert results[0]["status"] == "done"
    assert log_data["uploads"][candidates[0]["key"]]["video_id"] == "vid1"


def test_upload_now_continues_past_failures(_isolate, monkeypatch):
    output_dir = _isolate
    _write_clip(output_dir, "job1", "clip_00", score=90)
    _write_clip(output_dir, "job1", "clip_01", score=80)
    candidates = sched.find_candidates(CFG, {"uploads": {}})

    def flaky_upload_clip(video, snippet, privacy="private", service=None,
                          publish_at=None, category_id=None):
        if "clip_00" in str(video):
            raise UploadError("boom")
        return {"video_id": "vid2", "url": "https://youtu.be/vid2"}

    monkeypatch.setattr(sched.youtube_upload, "upload_clip", flaky_upload_clip)
    log_data = {"uploads": {}}
    progress = []
    results = sched.upload_now(object(), CFG, log_data, candidates,
                               on_progress=progress.append)

    statuses = {r["key"]: r["status"] for r in results}
    assert len(results) == 2  # both attempted despite the first failing
    assert "failed" in statuses.values() and "done" in statuses.values()
    assert len(progress) == 2  # on_progress fired for every clip, pass or fail
    # only the successful upload is recorded in the log
    assert len(log_data["uploads"]) == 1


# ============================================================
# Approval gate (Part 4)
# ============================================================
def _set_approval(clip_dir, approval):
    meta = json.loads((clip_dir / "metadata.json").read_text(encoding="utf-8"))
    meta.setdefault("upload", {})["approval"] = approval
    (clip_dir / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")


def test_approval_state_defaults_to_pending():
    assert sched.approval_state({}) == "pending"
    assert sched.approval_state({"upload": {}}) == "pending"
    assert sched.approval_state({"upload": {"approval": "approved"}}) == "approved"


def test_approval_ok_respects_require_approval():
    on = {"upload": {"require_approval": True}}
    off = {"upload": {"require_approval": False}}
    # rejected never uploads, in either mode
    assert not sched.approval_ok({"upload": {"approval": "rejected"}}, on)
    assert not sched.approval_ok({"upload": {"approval": "rejected"}}, off)
    # pending: gated only when approval is required
    assert not sched.approval_ok({"upload": {"approval": "pending"}}, on)
    assert sched.approval_ok({"upload": {"approval": "pending"}}, off)
    # approved uploads regardless
    assert sched.approval_ok({"upload": {"approval": "approved"}}, on)


def test_find_candidates_gates_on_approval(_isolate):
    output_dir = _isolate
    a = _write_clip(output_dir, "job1", "clip_00", score=90)
    _write_clip(output_dir, "job1", "clip_01", score=80)  # stays pending
    _set_approval(a, "approved")
    cfg = {"upload": {"min_virality": 40, "require_approval": True}}
    cands = sched.find_candidates(cfg, {"uploads": {}})
    # only the explicitly approved clip is uploadable when approval is required
    assert [c["dir"].name for c in cands] == ["clip_00"]
    # with approval off, both pending clips are eligible again
    cfg["upload"]["require_approval"] = False
    assert len(sched.find_candidates(cfg, {"uploads": {}})) == 2


def test_find_pending_approval_lists_only_pending(_isolate):
    output_dir = _isolate
    _write_clip(output_dir, "job1", "clip_00", score=90)          # pending
    approved = _write_clip(output_dir, "job1", "clip_01", score=85)
    rejected = _write_clip(output_dir, "job1", "clip_02", score=80)
    _set_approval(approved, "approved")
    _set_approval(rejected, "rejected")
    cfg = {"upload": {"min_virality": 40, "require_approval": True}}
    pending = sched.find_pending_approval(cfg, {"uploads": {}})
    assert [c["dir"].name for c in pending] == ["clip_00"]


# ============================================================
# Schedule-ahead (Part 3)
# ============================================================
SYNC_CFG = {"upload": {"min_virality": 40, "max_per_day": 5,
                       "publish_slots_ist": [12, 19], "schedule_ahead_days": 2,
                       "slot_spacing_minutes": 60, "category_id": "22"}}


def _fake_upload_clip(*a, **k):
    _fake_upload_clip.n += 1
    return {"video_id": f"v{_fake_upload_clip.n}", "url": "u"}
_fake_upload_clip.n = 0


def test_next_publish_times_slots_per_day_spreads_across_days():
    times = sched.next_publish_times(3, None, {"uploads": {}}, [12], 60,
                                     slots_per_day=1)
    assert len({t.date() for t in times}) == 3   # one slot/day -> three days


def test_quota_status_counts_today():
    today = datetime.now(sched.IST).isoformat()
    log_data = {"uploads": {f"k{i}": {"uploaded_at": today} for i in range(2)}}
    q = sched.quota_status(SYNC_CFG, log_data)
    assert q["uploads_today"] == 2
    assert q["can_schedule_now"] == min(6, 5) - 2   # quota ceiling vs max_per_day


def test_sync_schedule_fills_horizon_bounded_by_slots(_isolate, monkeypatch):
    output_dir = _isolate
    for i in range(6):
        _write_clip(output_dir, "job1", f"clip_{i:02d}", score=90)
    _fake_upload_clip.n = 0
    monkeypatch.setattr(sched.youtube_upload, "upload_clip", _fake_upload_clip)
    log_data = {"uploads": {}}
    r = sched.sync_schedule(object(), None, SYNC_CFG, log_data)
    # 2 slots/day * 2-day horizon = 4 clips, and no day gets more than the
    # configured 2 slots (spread across days, never stacked into one)
    assert r["scheduled"] == 4
    assert len(log_data["uploads"]) == 4
    from collections import Counter
    per_day = Counter(datetime.fromisoformat(e["publish_at"]).date()
                      for e in log_data["uploads"].values())
    assert max(per_day.values()) <= 2


def test_sync_schedule_stops_at_quota(_isolate, monkeypatch):
    output_dir = _isolate
    for i in range(6):
        _write_clip(output_dir, "job1", f"clip_{i:02d}", score=90)
    monkeypatch.setattr(sched.youtube_upload, "upload_clip", _fake_upload_clip)
    today = datetime.now(sched.IST).isoformat()
    # 5 uploads already done today -> min(6,5) quota used up
    log_data = {"uploads": {f"done{i}": {"uploaded_at": today, "video_id": f"d{i}"}
                            for i in range(5)}}
    r = sched.sync_schedule(object(), None, SYNC_CFG, log_data)
    assert r["scheduled"] == 0 and r["can_schedule_now"] == 0


def test_unschedule_deletes_and_frees_slot(_isolate, monkeypatch):
    deleted = []
    monkeypatch.setattr(sched.youtube_upload, "delete_video",
                        lambda vid, service=None: deleted.append(vid))
    future = (datetime.now(sched.IST) + timedelta(hours=2)).isoformat()
    log_data = {"uploads": {"output/j/clip_00": {"video_id": "vX",
                                                 "publish_at": future}}}
    r = sched.unschedule(object(), "output/j/clip_00", log_data)
    assert deleted == ["vX"] and r["video_id"] == "vX"
    assert "output/j/clip_00" not in log_data["uploads"]   # slot freed


def test_unschedule_refuses_already_published(_isolate):
    past = (datetime.now(sched.IST) - timedelta(hours=1)).isoformat()
    log_data = {"uploads": {"k": {"video_id": "v", "publish_at": past}}}
    with pytest.raises(UploadError):
        sched.unschedule(object(), "k", log_data)


def test_classify_uploads_time_split():
    now = datetime.now(sched.IST)
    log_data = {"uploads": {
        "a": {"video_id": "va", "publish_at": (now + timedelta(hours=2)).isoformat()},
        "b": {"video_id": "vb", "publish_at": (now - timedelta(hours=2)).isoformat()},
    }}
    split = sched.classify_uploads(log_data)
    assert [r["video_id"] for r in split["scheduled"]] == ["va"]
    assert [r["video_id"] for r in split["published"]] == ["vb"]


def test_classify_uploads_live_status_overrides_clock():
    now = datetime.now(sched.IST)
    log_data = {"uploads": {
        "a": {"video_id": "va", "publish_at": (now + timedelta(hours=2)).isoformat()},
        "b": {"video_id": "vb", "publish_at": (now + timedelta(hours=3)).isoformat()},
        "c": {"video_id": "vc", "publish_at": (now + timedelta(hours=4)).isoformat()},
    }}
    # va went public early (override), vb private (override), vc unknown to the
    # status call -> falls back to its future publishAt = still scheduled
    split = sched.classify_uploads(log_data, {"va": "public", "vb": "private"})
    assert [r["video_id"] for r in split["published"]] == ["va"]
    assert [r["video_id"] for r in split["scheduled"]] == ["vb", "vc"]
