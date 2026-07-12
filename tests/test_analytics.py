"""analytics.py tests — the fetch/cache layer. Google API objects are
MagicMock()s with .execute() stubbed directly, same pattern as
test_upload_scheduler.py. No real network, no real Google client."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

import analytics
import upload_scheduler as sched
from errors import UploadError


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(analytics, "CACHE_FILE", tmp_path / "analytics_cache.json")
    monkeypatch.setattr(sched, "LOG_FILE", tmp_path / "upload_log.json")
    monkeypatch.setattr(analytics, "ROOT", tmp_path)


def _mock_analytics(overview_28d=None, overview_90d=None, video_rows=None):
    """Mocked youtubeAnalytics client: refresh() calls .reports().query().execute()
    three times in order — 28d overview, 90d overview, video rows."""
    overview_28d = overview_28d or [[100, 20.0, 40.0, 5, 1]]
    overview_90d = overview_90d or [[300, 60.0, 41.0, 15, 3]]
    video_rows = video_rows if video_rows is not None else []
    client = MagicMock()
    execute = client.reports.return_value.query.return_value.execute
    execute.side_effect = [
        {"rows": overview_28d}, {"rows": overview_90d}, {"rows": video_rows},
    ]
    return client


def test_refresh_uses_cache_within_ttl(monkeypatch):
    client = _mock_analytics()
    monkeypatch.setattr("youtube_upload.build_analytics_service", lambda: client)

    first = analytics.refresh()
    assert first["overview"]["28d"]["views"] == 100
    execute = client.reports.return_value.query.return_value.execute
    assert execute.call_count == 3

    second = analytics.refresh()  # still within TTL -> no new query
    assert second == first
    assert execute.call_count == 3


def test_refresh_force_bypasses_ttl(monkeypatch):
    client = _mock_analytics()
    monkeypatch.setattr("youtube_upload.build_analytics_service", lambda: client)

    analytics.refresh()
    execute = client.reports.return_value.query.return_value.execute
    execute.side_effect = [
        {"rows": [[200, 30.0, 45.0, 8, 2]]}, {"rows": [[400, 70.0, 46.0, 20, 4]]},
        {"rows": []},
    ]
    second = analytics.refresh(force=True)
    assert second["overview"]["28d"]["views"] == 200
    assert execute.call_count == 6


def test_refresh_survives_analytics_error_with_no_prior_cache(monkeypatch):
    client = MagicMock()
    client.reports.return_value.query.return_value.execute.side_effect = RuntimeError("boom")
    monkeypatch.setattr("youtube_upload.build_analytics_service", lambda: client)

    with pytest.raises(UploadError):
        analytics.refresh()
    assert not analytics.CACHE_FILE.exists()


def test_refresh_falls_back_to_stale_cache_on_error(monkeypatch):
    client = _mock_analytics()
    monkeypatch.setattr("youtube_upload.build_analytics_service", lambda: client)
    good = analytics.refresh()

    failing_client = MagicMock()
    failing_client.reports.return_value.query.return_value.execute.side_effect = \
        RuntimeError("quota exceeded")
    monkeypatch.setattr("youtube_upload.build_analytics_service", lambda: failing_client)

    result = analytics.refresh(force=True)
    assert result == good


def test_refresh_joins_video_rows_against_upload_log(monkeypatch):
    sched.save_log({"uploads": {
        "output/job1/clip_00": {"video_id": "YT1", "title": "Best clip",
                                "publish_at": "2026-07-01T18:00:00+05:30"},
    }})
    client = _mock_analytics(video_rows=[["YT1", 500, 90.0, 44.0, 12, 2]])
    monkeypatch.setattr("youtube_upload.build_analytics_service", lambda: client)

    data = analytics.refresh()
    assert len(data["videos"]) == 1
    assert data["videos"][0]["key"] == "output/job1/clip_00"
    assert data["videos"][0]["views"] == 500


def test_load_cache_recovers_from_corrupt_file():
    analytics.CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    analytics.CACHE_FILE.write_text("{not json", encoding="utf-8")
    assert analytics.load_cache() is None
    assert analytics.CACHE_FILE.with_suffix(".json.corrupt").exists()
