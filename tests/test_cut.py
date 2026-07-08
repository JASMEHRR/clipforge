"""cut_segments concat behaviour against a tiny synthetic A/V clip."""
import cut
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
