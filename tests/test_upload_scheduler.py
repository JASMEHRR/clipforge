"""upload_scheduler tests — all Google API calls mocked, no network."""
import json
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

import upload_scheduler as sched


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
    monkeypatch.setattr(sched, "ROOT", tmp_path)
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
    hours = [(t.date(), t.hour) for t in times]
    assert len(hours) == len(set(hours))  # no two slots same day+hour


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
