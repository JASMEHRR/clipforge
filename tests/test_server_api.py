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
from errors import JobCancelled
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


def test_upload_size_cap(client, monkeypatch):
    import server.routes_run as rr
    monkeypatch.setattr(rr, "load_config",
                        lambda: {"ui": {"max_upload_mb": 0}})
    r = client.post("/api/uploads",
                    files={"file": ("big.mp4", b"x" * 2048, "video/mp4")})
    assert r.status_code == 413
