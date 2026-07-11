"""API-layer tests: mocked pipeline, real ProgressTracker, offline.

The fake run_job drives a real tracker through a couple of stages so the
WebSocket stream and snapshot plumbing are exercised end to end without
touching ffmpeg or the network.
"""
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

import pipeline
import progress
from errors import JobCancelled, UploadError
from server import create_app
from server.copy import STAGE_LABELS


@pytest.fixture()
def client():
    with TestClient(create_app()) as c:   # context manager runs lifespan
        yield c


def _wait_state(client, run_id, want, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = client.get(f"/api/runs/{run_id}").json()
        if st["state"] == want:
            return st
        time.sleep(0.02)
    raise AssertionError(f"run never reached state {want!r}: {st}")


def _fake_job(job_dir):
    return {"job_id": "abc123", "created": "2026-07-11T00:00:00",
            "source": "x", "status": "done", "settings": {}, "stages": {},
            "clips": [], "notes": [], "job_dir": str(job_dir)}


def test_stage_labels_cover_all_pipeline_stages():
    missing = [k for k, _, _ in progress.STAGES if k not in STAGE_LABELS]
    assert not missing, f"stages without plain-language labels: {missing}"


def test_run_lifecycle_ws_and_status(client, tmp_path, monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def fake_run_job(source, cfg=None, tracker=None, job_dir=None,
                     cancel=None, **kw):
        tracker.start("init", "loading configuration")
        tracker.finish("init")
        tracker.start("ingest", "downloading")
        tracker.update("ingest", 0.5, "halfway")
        started.set()
        release.wait(timeout=5)
        tracker.finish("ingest")
        tracker.finish("done", "completed")
        return _fake_job(job_dir)

    monkeypatch.setattr(pipeline, "run_job", fake_run_job)
    d = tmp_path / "20260711-000000_test"
    d.mkdir()
    monkeypatch.setattr(pipeline, "new_job_dir", lambda cfg, src: d)

    r = client.post("/api/runs", json={"source": "https://example.com/v"})
    assert r.status_code == 200
    run_id = r.json()["run_id"]
    assert started.wait(timeout=5)

    # status endpoint sees the live snapshot with plain-language labels
    st = client.get(f"/api/runs/{run_id}").json()
    assert st["state"] == "running"
    rows = {s["key"]: s for s in st["snapshot"]["stages"]}
    assert rows["ingest"]["label"] == "Preparing your video"

    # WebSocket: first frame is a snapshot, terminal frame is 'done'
    with client.websocket_connect(f"/ws/runs/{run_id}") as ws:
        first = ws.receive_json()
        assert first["type"] == "snapshot"
        release.set()
        msg = first
        for _ in range(50):
            msg = ws.receive_json()
            if msg["type"] != "snapshot":
                break
        assert msg["type"] == "done"
        assert msg["result"]["job_id"] == "abc123"

    st = _wait_state(client, run_id, "done")
    assert st["result"]["job_dir"].endswith("_test")


def test_list_runs_shows_in_flight_run(client, tmp_path, monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def fake_run_job(source, cfg=None, tracker=None, job_dir=None,
                     cancel=None, **kw):
        tracker.start("ingest", "downloading")
        tracker.update("ingest", 0.5, "halfway")
        started.set()
        release.wait(timeout=5)
        tracker.finish("ingest")
        tracker.finish("done", "completed")
        return _fake_job(job_dir)

    monkeypatch.setattr(pipeline, "run_job", fake_run_job)
    d = tmp_path / "20260711-000001_activity"
    d.mkdir()
    monkeypatch.setattr(pipeline, "new_job_dir", lambda cfg, src: d)

    run_id = client.post("/api/runs",
                         json={"source": "https://example.com/v"}).json()["run_id"]
    assert started.wait(timeout=5)

    runs = client.get("/api/runs").json()["runs"]
    mine = next(r for r in runs if r["run_id"] == run_id)
    assert mine["state"] == "running"
    assert mine["stage"] == "Preparing your video"   # plain-language label
    assert 0.0 <= mine["overall"] <= 1.0

    release.set()
    _wait_state(client, run_id, "done")
    # still listed after finishing (registry keeps it for the session)
    done = next(r for r in client.get("/api/runs").json()["runs"]
                if r["run_id"] == run_id)
    assert done["state"] == "done"


def test_run_cancel(client, tmp_path, monkeypatch):
    started = threading.Event()

    def fake_run_job(source, cfg=None, tracker=None, job_dir=None,
                     cancel=None, **kw):
        tracker.start("ingest", "downloading")
        started.set()
        for _ in range(200):
            if cancel.is_set():
                raise JobCancelled("cancelled by user")
            time.sleep(0.02)
        raise AssertionError("cancel never arrived")

    monkeypatch.setattr(pipeline, "run_job", fake_run_job)
    d = tmp_path / "20260711-000001_test"
    d.mkdir()
    monkeypatch.setattr(pipeline, "new_job_dir", lambda cfg, src: d)

    run_id = client.post("/api/runs",
                         json={"source": "https://e.com/v"}).json()["run_id"]
    assert started.wait(timeout=5)
    r = client.post(f"/api/runs/{run_id}/cancel")
    assert r.status_code == 200
    st = _wait_state(client, run_id, "cancelled")
    assert st["error"] is None


def test_run_error_is_friendly(client, tmp_path, monkeypatch):
    def fake_run_job(source, **kw):
        raise RuntimeError("ffmpeg exploded: some internal detail")

    monkeypatch.setattr(pipeline, "run_job", fake_run_job)
    d = tmp_path / "20260711-000002_test"
    d.mkdir()
    monkeypatch.setattr(pipeline, "new_job_dir", lambda cfg, src: d)

    run_id = client.post("/api/runs",
                         json={"source": "https://e.com/v"}).json()["run_id"]
    st = _wait_state(client, run_id, "error")
    assert "didn't work" in st["error"]
    assert "cache/logs/ui.log" in st["error"]


def test_run_rejects_empty_and_missing_source(client):
    assert client.post("/api/runs", json={"source": "  "}).status_code == 422
    assert client.post(
        "/api/runs",
        json={"source": "E:/nope/definitely-missing.mp4"}).status_code == 422


def test_unknown_run_404(client):
    assert client.get("/api/runs/nope").status_code == 404
    assert client.post("/api/runs/nope/cancel").status_code == 404


def test_files_path_traversal_blocked(client, tmp_path, monkeypatch):
    import server.routes_library as lib
    monkeypatch.setattr(lib, "output_root", lambda: tmp_path.resolve())
    (tmp_path / "job1" / "clip_00").mkdir(parents=True)
    (tmp_path / "job1" / "clip_00" / "final.mp4").write_bytes(b"vid")
    secret = tmp_path.parent / "secret.txt"
    secret.write_text("no")

    assert client.get("/api/files/job1/clip_00/final.mp4").status_code == 200
    assert client.get("/api/files/..%2f/clip_00/secret.txt").status_code == 404
    assert client.get(
        "/api/files/job1/clip_00/..%2f..%2f..%2fsecret.txt").status_code == 404
    # suffix whitelist: an .exe next to the clip is never served
    (tmp_path / "job1" / "clip_00" / "x.exe").write_bytes(b"mz")
    assert client.get("/api/files/job1/clip_00/x.exe").status_code == 404


def test_jobs_listing_and_detail(client, tmp_path, monkeypatch):
    import server.routes_library as lib
    monkeypatch.setattr(lib, "output_root", lambda: tmp_path.resolve())
    jd = tmp_path / "20260711-000003_demo"
    (jd / "clip_00").mkdir(parents=True)
    (jd / "job.json").write_text(json.dumps(
        {"job_id": "j1", "created": "2026-07-11", "source": "s",
         "status": "done", "settings": {}, "stages": {},
         "clips": [{"index": 0, "kept": True}], "notes": []}),
        encoding="utf-8")
    (jd / "clip_00" / "metadata.json").write_text(
        json.dumps({"title": "t", "upload": {"exclude": True}}),
        encoding="utf-8")

    jobs = client.get("/api/jobs").json()["jobs"]
    assert jobs[0]["name"] == jd.name and jobs[0]["kept"] == 1
    detail = client.get(f"/api/jobs/{jd.name}").json()
    assert detail["clips"][0]["upload_excluded"] is True
    assert client.get("/api/jobs/nope").status_code == 404


def test_exclude_round_trip(client, tmp_path, monkeypatch):
    import server.routes_library as lib
    monkeypatch.setattr(lib, "output_root", lambda: tmp_path.resolve())
    jd = tmp_path / "20260711-000004_demo"
    (jd / "clip_01").mkdir(parents=True)
    (jd / "job.json").write_text("{}", encoding="utf-8")
    meta = jd / "clip_01" / "metadata.json"
    meta.write_text(json.dumps({"title": "t"}), encoding="utf-8")

    r = client.put(f"/api/jobs/{jd.name}/clips/1/exclude",
                   json={"exclude": True})
    assert r.status_code == 200
    assert json.loads(meta.read_text())["upload"]["exclude"] is True
    client.put(f"/api/jobs/{jd.name}/clips/1/exclude", json={"exclude": False})
    assert json.loads(meta.read_text())["upload"]["exclude"] is False


def test_kept_round_trip(client, tmp_path, monkeypatch):
    import server.routes_library as lib
    monkeypatch.setattr(lib, "output_root", lambda: tmp_path.resolve())
    jd = tmp_path / "20260711-000005_demo"
    jd.mkdir(parents=True)
    jp = jd / "job.json"
    jp.write_text(json.dumps({"clips": [{"index": 2, "kept": True}]}),
                  encoding="utf-8")

    r = client.put(f"/api/jobs/{jd.name}/clips/2/kept", json={"kept": False})
    assert r.status_code == 200
    assert json.loads(jp.read_text())["clips"][0]["kept"] is False
    client.put(f"/api/jobs/{jd.name}/clips/2/kept", json={"kept": True})
    assert json.loads(jp.read_text())["clips"][0]["kept"] is True
    # unknown clip index and missing job both 404
    assert client.put(f"/api/jobs/{jd.name}/clips/9/kept",
                      json={"kept": True}).status_code == 404
    assert client.put("/api/jobs/nope/clips/0/kept",
                      json={"kept": True}).status_code == 404


def test_settings_round_trip_touches_only_local(client, monkeypatch, tmp_path):
    import config as config_mod
    local = tmp_path / "config.local.yaml"
    monkeypatch.setattr(config_mod, "LOCAL_PATH", local)
    config_mod._cached = None

    before = client.get("/api/settings").json()
    assert "provider" in before
    r = client.put("/api/settings", json={
        "compute": "cpu", "whisper_model": "small", "provider": "mock",
        "gemini_model": "", "groq_model": "", "ollama_model": ""})
    assert r.status_code == 200
    assert r.json()["compute"] == "cpu"
    assert local.exists()          # wrote the local overlay, not config.yaml
    config_mod._cached = None      # don't leak the tmp overlay to other tests

    assert client.put("/api/settings",
                      json={"compute": "warp"}).status_code == 422


def test_presets_and_music_endpoints(client):
    presets = client.get("/api/presets").json()
    assert presets["default"] in presets["presets"]
    tracks = client.get("/api/music").json()["tracks"]
    assert isinstance(tracks, list)


def test_batch_zip_empty_queue_404(client):
    assert client.get("/api/batch/zip").status_code == 404


def _write_candidate_clip(output_dir, job, clip, score, title="A clip"):
    clip_dir = output_dir / job / clip
    clip_dir.mkdir(parents=True)
    (clip_dir / "final.mp4").write_bytes(b"\x00" * 10)
    meta = {"title": title, "description": "Desc.", "hashtags": ["#a", "#shorts"],
            "virality": {"score": score, "band": "Strong"},
            "original_source_start_s": 10.0, "original_source_end_s": 40.0,
            "source_name": "video.mp4"}
    (clip_dir / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    return clip_dir


def _wait_batch(client, batch_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = client.get(f"/api/youtube/queue/upload/{batch_id}").json()
        if st["state"] == "done":
            return st
        time.sleep(0.02)
    raise AssertionError(f"batch never finished: {st}")


def _isolate_upload_scheduler(monkeypatch, tmp_path):
    import upload_scheduler as sched
    import server.routes_upload as ru
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    monkeypatch.setattr(sched, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(sched, "ROOT", tmp_path)
    monkeypatch.setattr(sched, "LOG_FILE", tmp_path / "cache" / "upload_log.json")
    monkeypatch.setattr(ru, "ROOT", tmp_path)
    return output_dir


def test_youtube_queue_lists_candidates_and_serves_video(client, monkeypatch, tmp_path):
    output_dir = _isolate_upload_scheduler(monkeypatch, tmp_path)
    _write_candidate_clip(output_dir, "job1", "clip_00", score=90, title="Best clip")

    r = client.get("/api/youtube/queue").json()
    assert len(r["candidates"]) == 1
    cand = r["candidates"][0]
    assert cand["title"] == "Best clip" and cand["score"] == 90
    assert cand["band"] == "Strong" and cand["duration"] == 30.0

    video = client.get(cand["video_url"])
    assert video.status_code == 200

    # path traversal via the video route is blocked
    assert client.get("/api/youtube/queue/video/..%2f..%2fsecret").status_code == 404


def test_youtube_queue_select_top_and_manual(client, monkeypatch, tmp_path):
    output_dir = _isolate_upload_scheduler(monkeypatch, tmp_path)
    _write_candidate_clip(output_dir, "job1", "clip_00", score=90, title="High")
    _write_candidate_clip(output_dir, "job1", "clip_01", score=60, title="Low")

    top = client.post("/api/youtube/queue/select", json={"mode": "top", "count": 1}).json()
    assert len(top["items"]) == 1 and top["items"][0]["title"] == "High"

    all_keys = [c["key"] for c in client.get("/api/youtube/queue").json()["candidates"]]
    manual = client.post("/api/youtube/queue/select",
                         json={"mode": "manual", "keys": all_keys}).json()
    assert len(manual["items"]) == 2

    bad_mode = client.post("/api/youtube/queue/select", json={"mode": "nope"})
    assert bad_mode.status_code == 422


def test_youtube_queue_select_cap_warning(client, monkeypatch, tmp_path):
    output_dir = _isolate_upload_scheduler(monkeypatch, tmp_path)
    for i in range(5):
        _write_candidate_clip(output_dir, "job1", f"clip_{i:02d}", score=90)
    # real config.yaml's upload.max_per_day is 5; requesting all 5 fresh fits
    fits = client.post("/api/youtube/queue/select", json={"mode": "top", "count": 5}).json()
    assert fits["warning"] is None


def test_youtube_queue_upload_requires_authorization(client, monkeypatch, tmp_path):
    _isolate_upload_scheduler(monkeypatch, tmp_path)
    monkeypatch.setattr("youtube_upload.credentials_available", lambda: False)
    r = client.post("/api/youtube/queue/upload", json={"mode": "top", "count": 1})
    assert r.status_code == 409


def test_youtube_queue_upload_confirm_then_submit_flow(client, monkeypatch, tmp_path):
    output_dir = _isolate_upload_scheduler(monkeypatch, tmp_path)
    _write_candidate_clip(output_dir, "job1", "clip_00", score=90, title="Good clip")
    _write_candidate_clip(output_dir, "job1", "clip_01", score=80, title="Bad clip")

    monkeypatch.setattr("youtube_upload.credentials_available", lambda: True)
    monkeypatch.setattr("youtube_upload.has_cached_token", lambda: True)
    monkeypatch.setattr("youtube_upload.build_service", lambda service=None: object())

    def fake_upload_clip(video, snippet, privacy="private", service=None,
                         publish_at=None, category_id=None):
        assert privacy == "public" and publish_at is None   # immediate, not scheduled
        if "clip_01" in str(video):
            raise UploadError("quota hit")
        return {"video_id": "vidX", "url": "https://youtu.be/vidX"}

    monkeypatch.setattr("youtube_upload.upload_clip", fake_upload_clip)

    # confirm step: see exactly what would be sent, before submitting
    preview = client.post("/api/youtube/queue/select", json={"mode": "top", "count": 2}).json()
    assert len(preview["items"]) == 2

    run = client.post("/api/youtube/queue/upload", json={"mode": "top", "count": 2})
    assert run.status_code == 200
    batch_id = run.json()["batch_id"]

    final = _wait_batch(client, batch_id)
    statuses = {it["title"]: it["status"] for it in final["items"]}
    assert statuses["Good clip"] == "done"
    assert statuses["Bad clip"] == "failed"

    # the failure didn't stop the batch, and only the success was logged
    log_data = json.loads((tmp_path / "cache" / "upload_log.json").read_text())
    assert len(log_data["uploads"]) == 1

    # the uploaded clip no longer appears in a fresh queue fetch; the failed
    # one is still eligible (it was never logged as uploaded)
    remaining = client.get("/api/youtube/queue").json()["candidates"]
    assert [c["title"] for c in remaining] == ["Bad clip"]


def test_upload_size_cap(client, monkeypatch):
    import server.routes_run as rr
    monkeypatch.setattr(rr, "load_config",
                        lambda: {"ui": {"max_upload_mb": 0}})
    r = client.post("/api/uploads",
                    files={"file": ("big.mp4", b"x" * 2048, "video/mp4")})
    assert r.status_code == 413
