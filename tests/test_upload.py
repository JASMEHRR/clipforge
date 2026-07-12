"""YouTube upload logic verified ONLY against mocked API responses — no live
auth, no network (spec rule 5)."""
from unittest.mock import MagicMock

import pytest

import youtube_upload as yt
from errors import UploadError, UploadQuotaError

META = {"title": "T" * 120, "description": "Desc.",
        "hashtags": ["#a", "#b", "#c", "#d", "#e", "#f", "#g", "#h"]}


def test_request_body_private_default():
    body = yt.build_request_body(META)
    assert body["status"]["privacyStatus"] == "private"
    assert len(body["snippet"]["title"]) <= 100
    assert body["snippet"]["tags"] == list("abcdefgh")
    assert "#a" in body["snippet"]["description"]


def test_upload_with_mocked_service(tmp_path):
    clip = tmp_path / "final.mp4"
    clip.write_bytes(b"\x00" * 2048)
    service = MagicMock()
    request = service.videos.return_value.insert.return_value
    request.next_chunk.return_value = (None, {"id": "vid123"})
    out = yt.upload_clip(clip, META, service=service)
    assert out == {"video_id": "vid123", "url": "https://youtu.be/vid123"}
    _, kwargs = service.videos.return_value.insert.call_args
    assert kwargs["body"]["status"]["privacyStatus"] == "private"


def test_quota_error_is_friendly(tmp_path):
    clip = tmp_path / "final.mp4"
    clip.write_bytes(b"\x00" * 2048)
    service = MagicMock()
    request = service.videos.return_value.insert.return_value
    request.next_chunk.side_effect = Exception(
        '<HttpError 403 "quotaExceeded">')
    with pytest.raises(UploadQuotaError) as ei:
        yt.upload_clip(clip, META, service=service)
    assert "quota" in str(ei.value).lower()
    assert "midnight" in str(ei.value)  # actionable guidance


def test_auth_expiry_classified(tmp_path):
    clip = tmp_path / "final.mp4"
    clip.write_bytes(b"\x00" * 2048)
    service = MagicMock()
    request = service.videos.return_value.insert.return_value
    request.next_chunk.side_effect = Exception("401 invalid_grant")
    with pytest.raises(UploadError) as ei:
        yt.upload_clip(clip, META, service=service)
    assert not isinstance(ei.value, UploadQuotaError)
    assert "Authorize" in str(ei.value)


def test_missing_file_raises():
    with pytest.raises(UploadError):
        yt.upload_clip("does/not/exist.mp4", META, service=MagicMock())


def test_no_creds_guidance(monkeypatch):
    monkeypatch.delenv("YOUTUBE_CLIENT_SECRETS", raising=False)
    assert not yt.credentials_available()
    with pytest.raises(UploadError) as ei:
        yt.authorize()
    assert "console.cloud.google.com" in (ei.value.detail or "")


# ---- Dry-run guard: sync/upload paths must never reach the real API ----

def test_dry_run_flag_reads_env(monkeypatch):
    monkeypatch.delenv("CLIPFORGE_DRY_RUN", raising=False)
    assert yt.dry_run() is False
    for on in ("1", "true", "YES", "on"):
        monkeypatch.setenv("CLIPFORGE_DRY_RUN", on)
        assert yt.dry_run() is True
    for off in ("0", "false", "no", "", "off"):
        monkeypatch.setenv("CLIPFORGE_DRY_RUN", off)
        assert yt.dry_run() is False


def test_dry_run_never_builds_a_real_client(monkeypatch, tmp_path):
    # any attempt to reach Google (credentials or discovery) is a hard failure
    monkeypatch.setenv("CLIPFORGE_DRY_RUN", "1")
    monkeypatch.setattr(yt, "_load_credentials",
                        lambda: pytest.fail("touched real credentials in dry-run"))
    clip = tmp_path / "final.mp4"
    clip.write_bytes(b"\x00" * 16)

    assert yt.build_service() is yt._DRY_SERVICE
    out = yt.upload_clip(clip, META)              # no service passed
    assert out["video_id"].startswith("DRYRUN")
    assert yt.delete_video("anything") is None    # no-op
    assert yt.video_status(["a", "b"]) == {}      # no live status call


def test_dry_run_upload_is_deterministic(monkeypatch, tmp_path):
    monkeypatch.setenv("CLIPFORGE_DRY_RUN", "1")
    clip = tmp_path / "final.mp4"
    clip.write_bytes(b"\x00" * 16)
    assert yt.upload_clip(clip, META) == yt.upload_clip(clip, META)


def test_dry_run_still_validates_missing_file(monkeypatch, tmp_path):
    monkeypatch.setenv("CLIPFORGE_DRY_RUN", "1")
    with pytest.raises(UploadError):
        yt.upload_clip(tmp_path / "gone.mp4", META)
