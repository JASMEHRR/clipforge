"""cut_segments concat behaviour against a tiny synthetic A/V clip."""
import pytest

import cut
from errors import CutError
from ffutil import probe, run_ffmpeg


def _make_av(path, seconds=4.0):
    run_ffmpeg(["-f", "lavfi", "-i", f"testsrc=size=320x240:rate=10:d={seconds}",
                "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                "-pix_fmt", "yuv420p", "-shortest", str(path)])
    return path


def test_cut_segments_concats(tmp_path, cfg):
    src = _make_av(tmp_path / "src.mp4")
    out = cut.cut_segments(src, [[0.5, 1.5], [2.5, 3.5]], tmp_path / "out.mp4", cfg)
    d = probe(out)["duration"]
    assert 1.7 < d < 2.3, f"expected ~2.0s, got {d}"


def test_cut_segments_single_delegates(tmp_path, cfg):
    src = _make_av(tmp_path / "src.mp4")
    out = cut.cut_segments(src, [[0.5, 2.5]], tmp_path / "one.mp4", cfg)
    d = probe(out)["duration"]
    assert 1.8 < d < 2.2


def test_cut_clip_clamps_end_past_source_duration(tmp_path, cfg):
    # source is 4s; request a range extending 100s past it (simulating an
    # unclamped extend_forward bound) — must clamp, never produce an empty file.
    src = _make_av(tmp_path / "src.mp4")
    out = cut.cut_clip(src, 1.0, 104.0, tmp_path / "out.mp4", cfg)
    info = probe(out)
    assert info["duration"] > 0 and info["has_audio"]


def test_cut_clip_range_entirely_past_duration_raises(tmp_path, cfg):
    src = _make_av(tmp_path / "src.mp4")
    with pytest.raises(CutError):
        cut.cut_clip(src, 100.0, 104.0, tmp_path / "out.mp4", cfg)


def test_cut_segments_clamps_out_of_bounds_segment(tmp_path, cfg):
    # second segment starts past the 4s source; should be dropped, not concat'd
    # into an empty/broken output.
    src = _make_av(tmp_path / "src.mp4")
    out = cut.cut_segments(src, [[0.5, 1.5], [100.0, 101.0]],
                           tmp_path / "out.mp4", cfg)
    info = probe(out)
    assert info["duration"] > 0 and info["has_audio"]


def test_cut_segments_all_out_of_bounds_raises(tmp_path, cfg):
    src = _make_av(tmp_path / "src.mp4")
    with pytest.raises(CutError):
        cut.cut_segments(src, [[100.0, 101.0], [102.0, 103.0]],
                         tmp_path / "out.mp4", cfg)
