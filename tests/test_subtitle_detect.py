"""subtitle_detect against a synthetic ffmpeg-rendered clip (text-like band)."""
import subtitle_detect
from ffutil import probe


def test_detects_burned_band(tmp_path):
    vid = subtitle_detect.make_synthetic(tmp_path / "subs.mp4", with_subs=True)
    dur = probe(vid)["duration"]
    r = subtitle_detect._detect_uncached(vid, 0.0, dur, 2.0, 0.45, 0.30)
    assert r["present"] is True
    # Fake band sits ~0.84 of frame height; must land in the lower region.
    assert r["band_top_pct"] > 0.55
    assert r["band_bottom_pct"] <= 1.0
    assert r["band_bottom_pct"] > r["band_top_pct"]
    assert 0.0 < r["confidence"] <= 1.0
    assert r["sampled_frames"] > 0


def test_clean_clip_reports_none(tmp_path):
    vid = subtitle_detect.make_synthetic(tmp_path / "clean.mp4", with_subs=False)
    dur = probe(vid)["duration"]
    r = subtitle_detect._detect_uncached(vid, 0.0, dur, 2.0, 0.45, 0.30)
    assert r["present"] is False
    assert r["confidence"] == 0.0


def test_result_validates_against_schema(tmp_path, cfg):
    from schemas import validate
    vid = subtitle_detect.make_synthetic(tmp_path / "s.mp4", with_subs=True)
    dur = probe(vid)["duration"]
    r = subtitle_detect.detect_subtitles(vid, 0.0, dur, cfg=cfg)
    validate(r, "subtitle_detect_result")  # raises if malformed
