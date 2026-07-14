"""Posting queue + multi-account quota + calendar: priority insertion, drag
reorder, per-account caps/back-compat, drain selection, token paths. All
Google calls mocked / never made — offline."""
import copy
import json
from datetime import datetime, timedelta

import pytest

import upload_scheduler as sched
import youtube_upload
from config import load_config as _load


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(sched, "QUEUE_FILE", tmp_path / "post_queue.json")
    monkeypatch.setattr(sched, "LOG_FILE", tmp_path / "upload_log.json")
    monkeypatch.setenv("CLIPFORGE_DRY_RUN", "")


def _cfg(**upload_over) -> dict:
    cfg = copy.deepcopy(_load())
    cfg["upload"] = {**cfg["upload"], **upload_over}
    return cfg


# --- account config back-compat -----------------------------------------------

def test_account_cfg_falls_back_to_flat_keys():
    cfg = _cfg(max_per_day=4, publish_slots_ist=[9, 14], accounts=None)
    acc = sched.account_cfg(cfg, "default")
    assert acc["max_per_day"] == 4 and acc["publish_slots_ist"] == [9, 14]
    assert sched.list_accounts(cfg) == ["default"]


def test_account_cfg_account_values_win():
    cfg = _cfg(max_per_day=4, accounts={
        "default": {"max_per_day": 2},
        "second": {"max_per_day": 7, "publish_slots_ist": [8]},
    })
    assert sched.account_cfg(cfg, "default")["max_per_day"] == 2
    assert sched.account_cfg(cfg, "second")["publish_slots_ist"] == [8]
    assert sched.list_accounts(cfg) == ["default", "second"]


def test_uploads_today_per_account():
    today = datetime.now(sched.IST).isoformat()
    log = {"uploads": {
        "a": {"uploaded_at": today},                        # legacy = default
        "b": {"uploaded_at": today, "account": "second"},
    }}
    assert sched.uploads_today(log, "default") == 1
    assert sched.uploads_today(log, "second") == 1
    assert sched.uploads_today(log) == 2


def test_token_path_per_account():
    assert youtube_upload.token_path("default") == youtube_upload.TOKEN_PATH
    p = youtube_upload.token_path("Second Brand!")
    assert p.name == "youtube_token_secondbrand.json"


# --- queue priority + reorder ---------------------------------------------------

def test_queue_priority_insertion():
    sched.queue_add("output/j1/clip_01", source="manual")
    sched.queue_add("output/j2/clip_01", source="channel_top")
    sched.queue_add("output/j3/clip_01", source="channel_new")
    sched.queue_add("output/j4/clip_01", source="channel_new")
    keys = [e["clip_key"] for e in sched.load_queue()["queue"]]
    assert keys == ["output/j3/clip_01", "output/j4/clip_01",
                    "output/j2/clip_01", "output/j1/clip_01"]


def test_queue_add_is_idempotent():
    sched.queue_add("output/j1/clip_01")
    sched.queue_add("output/j1/clip_01")
    assert len(sched.load_queue()["queue"]) == 1


def test_queue_reorder_persists_manual_order():
    for k in ("a", "b", "c"):
        sched.queue_add(f"output/{k}/clip_01", source="manual")
    sched.queue_reorder(["output/c/clip_01", "output/a/clip_01"])
    keys = [e["clip_key"] for e in sched.load_queue()["queue"]]
    assert keys == ["output/c/clip_01", "output/a/clip_01", "output/b/clip_01"]


def test_queue_remove():
    sched.queue_add("output/a/clip_01")
    sched.queue_remove("output/a/clip_01")
    assert sched.load_queue()["queue"] == []


# --- drain ------------------------------------------------------------------------

def _fake_candidate(key):
    return {"key": key, "dir": None, "video": None, "score": 80,
            "meta": {"title": f"T {key}", "description": "d",
                     "hashtags": ["#x"]}, "duplicates": []}


def test_drain_respects_per_account_cap(monkeypatch):
    cfg = _cfg(accounts={"default": {"max_per_day": 1,
                                     "publish_slots_ist": [12, 19]}})
    for k in ("a", "b"):
        sched.queue_add(f"output/{k}/clip_01", source="manual")
    monkeypatch.setattr(sched, "find_candidates", lambda c, l: [
        _fake_candidate("output/a/clip_01"), _fake_candidate("output/b/clip_01")])
    monkeypatch.setattr(sched.youtube_upload, "authorized", lambda a="default": True)
    monkeypatch.setattr(sched.youtube_upload, "build_service",
                        lambda service=None, account="default": object())
    uploaded = []

    def fake_upload_one(youtube, clip, publish_at, category_id, cfg=None,
                        service=None):
        uploaded.append(clip["key"])
        return {"video_id": f"vid_{len(uploaded)}", "url": "u",
                "uploaded_at": datetime.now(sched.IST).isoformat()}

    monkeypatch.setattr(sched, "upload_one", fake_upload_one)
    n = sched.drain_queue(cfg)
    assert n == 1 and uploaded == ["output/a/clip_01"]
    # second clip stays queued for tomorrow; first is logged with its account
    q = [e["clip_key"] for e in sched.load_queue()["queue"]]
    assert q == ["output/b/clip_01"]
    assert sched.load_log()["uploads"]["output/a/clip_01"]["account"] == "default"


def test_drain_keeps_pending_drops_uploaded(monkeypatch, tmp_path):
    cfg = _cfg()
    sched.queue_add("output/gone/clip_01")      # no dir, not uploaded → drop
    sched.queue_add("output/wait/clip_01")      # exists but not candidate → wait
    (sched.ROOT / "output/wait/clip_01").mkdir(parents=True, exist_ok=True)
    try:
        (sched.ROOT / "output/wait/clip_01/final.mp4").write_bytes(b"x")
        monkeypatch.setattr(sched, "find_candidates", lambda c, l: [])
        monkeypatch.setattr(sched.youtube_upload, "authorized",
                            lambda a="default": True)
        monkeypatch.setattr(sched.youtube_upload, "build_service",
                            lambda service=None, account="default": object())
        sched.drain_queue(cfg)
        keys = [e["clip_key"] for e in sched.load_queue()["queue"]]
        assert keys == ["output/wait/clip_01"]
    finally:
        import shutil
        shutil.rmtree(sched.ROOT / "output/wait", ignore_errors=True)


def test_drain_skips_unauthorized_account(monkeypatch):
    cfg = _cfg(accounts={"second": {"max_per_day": 3}})
    sched.queue_add("output/a/clip_01", account="second")
    monkeypatch.setattr(sched.youtube_upload, "authorized",
                        lambda a="default": False)
    called = []
    monkeypatch.setattr(sched, "find_candidates",
                        lambda c, l: called.append(1) or [])
    assert sched.drain_queue(cfg) == 0
    assert len(sched.load_queue()["queue"]) == 1   # still queued, not dropped


# --- calendar ----------------------------------------------------------------------

def test_calendar_groups_by_day_and_account():
    now = datetime.now(sched.IST)
    log = {"uploads": {
        "k1": {"publish_at": (now + timedelta(days=1)).replace(
            hour=9, minute=0).isoformat(), "title": "One",
            "account": "default", "video_id": "v1"},
        "k2": {"publish_at": (now + timedelta(days=1)).replace(
            hour=18, minute=0).isoformat(), "title": "Two",
            "account": "second", "video_id": "v2"},
    }}
    sched.queue_add("output/q/clip_01", account="second")
    cal = sched.calendar_view(_cfg(), log, account=None, days=3)
    tomorrow = next(d for d in cal["days"]
                    if d["date"] == (now + timedelta(days=1)).date().isoformat())
    assert [p["title"] for p in tomorrow["posts"]] == ["One", "Two"]
    only_second = sched.calendar_view(_cfg(), log, account="second", days=3)
    t2 = next(d for d in only_second["days"] if d["date"] == tomorrow["date"])
    assert [p["title"] for p in t2["posts"]] == ["Two"]
    assert len(only_second["queued"]) == 1


# --- trigger routes through the queue -----------------------------------------------

def test_trigger_after_render_queues(monkeypatch):
    cfg = _cfg(auto_enabled=True, queue_source="channel_new")
    drained = []
    monkeypatch.setattr(sched, "drain_queue", lambda c: drained.append(1))
    monkeypatch.setattr(sched.youtube_upload, "credentials_available",
                        lambda: True)
    clip_dir = sched.ROOT / "output" / "job" / "clip_00"
    sched.trigger_after_render(clip_dir, cfg)
    q = sched.load_queue()["queue"]
    assert q and q[0]["clip_key"] == "output/job/clip_00"
    assert q[0]["source"] == "channel_new"
    assert drained == [1]


def test_trigger_disabled_noop():
    sched.trigger_after_render(sched.ROOT / "output/x/clip_00",
                               _cfg(auto_enabled=False))
    assert sched.load_queue()["queue"] == []


# --- dashboard endpoints (Phase 6) ---------------------------------------------

def test_dashboard_endpoints(monkeypatch, tmp_path):
    import channels
    from fastapi.testclient import TestClient
    from server import create_app
    monkeypatch.setattr(channels, "STORE_PATH", tmp_path / "channels.json")
    ch = channels.add_channel("https://youtube.com/@x", "program", "credit")
    client = TestClient(create_app())
    r = client.get("/api/analytics/channels").json()
    assert r["channels"][0]["name"] == "@x"
    assert {"clips_made", "clips_posted", "videos_pulled",
            "pending"} <= set(r["channels"][0])
    assert r["accounts"][0]["account"] == "default"
    p = client.get("/api/analytics/presets").json()
    assert "presets" in p and "no_preset_jobs" in p
