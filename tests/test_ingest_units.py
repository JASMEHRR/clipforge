"""Ingest normalization decision logic + duration-scaled ffmpeg timeout
(regression tests for the 'ffmpeg timed out after 1800s' YouTube-job bug)."""
import pytest

from ingest import demo_cap_seconds, ffmpeg_timeout, normalize_plan


def _info(vcodec="h264", pix_fmt="yuv420p", acodec="aac", has_audio=True,
          duration=600.0):
    return {"vcodec": vcodec, "pix_fmt": pix_fmt, "acodec": acodec,
            "has_audio": has_audio, "duration": duration,
            "width": 1920, "height": 1080, "fps": 30.0}


# ------------------------------------------------------------ normalize_plan

def test_clean_mp4_is_remuxed():
    assert normalize_plan(_info(), ".mp4") == "remux"


def test_mov_container_is_remuxable():
    assert normalize_plan(_info(), ".MOV") == "remux"


@pytest.mark.parametrize("vcodec", ["vp9", "av01", "hevc", "mpeg4", ""])
def test_non_h264_reencodes(vcodec):
    assert normalize_plan(_info(vcodec=vcodec), ".mp4") == "reencode"


@pytest.mark.parametrize("pix", ["yuv444p", "yuv420p10le", ""])
def test_exotic_pixel_format_reencodes(pix):
    assert normalize_plan(_info(pix_fmt=pix), ".mp4") == "reencode"


def test_yuvj420p_still_remuxes():
    assert normalize_plan(_info(pix_fmt="yuvj420p"), ".mp4") == "remux"


@pytest.mark.parametrize("suffix", [".mkv", ".webm", ".avi", ".ts"])
def test_non_mp4_container_reencodes(suffix):
    assert normalize_plan(_info(), suffix) == "reencode"


def test_opus_audio_only_transcodes_audio():
    assert normalize_plan(_info(acodec="opus"), ".mp4") == "audio_only"


def test_missing_audio_copies_video_only():
    assert normalize_plan(_info(acodec="", has_audio=False), ".mp4") == "audio_only"


# ----------------------------------------------------------- ffmpeg_timeout

def test_timeout_floor_for_short_inputs(cfg):
    assert ffmpeg_timeout(cfg, 60.0) == 1800


def test_timeout_scales_with_duration(cfg):
    # 2-hour input: 7200s * 3.0 = 21600s, not the flat 1800s that caused the bug
    assert ffmpeg_timeout(cfg, 7200.0) == 21600


def test_timeout_uses_config_values():
    cfg = {"ffmpeg": {"timeout_base_seconds": 100,
                      "timeout_per_input_second": 2.0}}
    assert ffmpeg_timeout(cfg, 30.0) == 100
    assert ffmpeg_timeout(cfg, 200.0) == 400


def test_timeout_defaults_without_section():
    assert ffmpeg_timeout({}, 1000.0) == 3000


# ------------------------------------------------------------ demo capping

def test_demo_cap_off_by_default(monkeypatch):
    monkeypatch.delenv("CLIPFORGE_DEMO", raising=False)
    assert demo_cap_seconds() is None


def test_demo_cap_on(monkeypatch):
    monkeypatch.setenv("CLIPFORGE_DEMO", "1")
    monkeypatch.delenv("CLIPFORGE_DEMO_MAX_SECONDS", raising=False)
    assert demo_cap_seconds() == 300.0


def test_demo_cap_custom(monkeypatch):
    monkeypatch.setenv("CLIPFORGE_DEMO", "1")
    monkeypatch.setenv("CLIPFORGE_DEMO_MAX_SECONDS", "120")
    assert demo_cap_seconds() == 120.0
