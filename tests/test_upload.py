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
