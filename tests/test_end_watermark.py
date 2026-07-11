"""Upload-time end watermark (item 7): a branded end card is appended only to
the uploaded copy, leaving the archived render untouched. Real-ffmpeg tests
(skipped if ffmpeg is unavailable) — a mock can't measure the output duration.
"""
import subprocess

import pytest

import ffutil
import upload_scheduler as sched
from config import load_config


def _ffmpeg_ok() -> bool:
    try:
        subprocess.run([ffutil.ffmpeg_bin(), "-version"],
                       capture_output=True, timeout=15)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


pytestmark = pytest.mark.skipif(not _ffmpeg_ok(), reason="ffmpeg not available")


def _make_clip(path, dur=2.0):
    subprocess.run(
        [ffutil.ffmpeg_bin(), "-y", "-v", "error",
         "-f", "lavfi", "-i", f"color=c=blue:s=180x320:r=24:d={dur}",
         "-f", "lavfi", "-i", f"sine=frequency=330:duration={dur}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-shortest", str(path)], check=True, capture_output=True)


def test_disabled_returns_original_untouched(tmp_path):
    clip = tmp_path / "final.mp4"
    _make_clip(clip)
    cfg = load_config()
    cfg["upload"]["end_watermark"] = {"enabled": False}
    out, is_temp = sched.apply_end_watermark(clip, cfg)
    assert out == str(clip) and is_temp is False


def test_enabled_appends_end_card(tmp_path):
    clip = tmp_path / "final.mp4"
    _make_clip(clip, dur=2.0)
    src_dur = ffutil.probe(clip)["duration"]

    cfg = load_config()
    cfg["upload"]["end_watermark"] = {"enabled": True, "text": "ClipForge",
                                      "duration_s": 1.2}
    out, is_temp = sched.apply_end_watermark(clip, cfg)
    try:
        assert is_temp is True and out != str(clip)
        out_dur = ffutil.probe(out)["duration"]
        # source + ~1.2s outro, allowing for encoder frame rounding
        assert out_dur == pytest.approx(src_dur + 1.2, abs=0.4)
        # the archived render is unchanged
        assert ffutil.probe(clip)["duration"] == pytest.approx(src_dur, abs=0.05)
    finally:
        __import__("pathlib").Path(out).unlink(missing_ok=True)


def test_missing_font_falls_back_to_clean_file(tmp_path, monkeypatch):
    clip = tmp_path / "final.mp4"
    _make_clip(clip)
    monkeypatch.setattr(sched, "BRAND_FONT", tmp_path / "nope.ttf")
    cfg = load_config()
    cfg["upload"]["end_watermark"] = {"enabled": True}
    # branding failure must never block the upload — returns the clean file
    out, is_temp = sched.apply_end_watermark(clip, cfg)
    assert out == str(clip) and is_temp is False
