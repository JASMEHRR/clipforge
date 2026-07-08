"""Position law + CTA in the ASS writer (pure, no ffmpeg)."""
import re

from captions import write_ass, _clamp_anchor


def _words(n=6):
    return [{"word": f"w{i}", "start": round(0.4 * i, 2), "end": round(0.4 * i + 0.35, 2)}
            for i in range(n)]


def test_anchor_clamped_to_band():
    assert _clamp_anchor(0.40) == 0.52
    assert _clamp_anchor(0.99) == 0.66
    assert _clamp_anchor(0.60) == 0.60


def test_pos_within_band(tmp_path, cfg):
    ass = tmp_path / "c.ass"
    write_ass(_words(), ass, cfg, "karaoke-pop", play_w=1080, play_h=1920,
              anchor=0.60)
    ys = [int(m) for m in re.findall(r"\\pos\(540,(\d+)\)", ass.read_text(encoding="utf-8-sig"))]
    assert ys, "expected \\pos tags"
    assert all(0.52 * 1920 <= y <= 0.66 * 1920 for y in ys)


def test_out_of_band_anchor_is_clamped(tmp_path, cfg):
    ass = tmp_path / "c.ass"
    write_ass(_words(), ass, cfg, "karaoke-pop", play_h=1920, anchor=0.95)
    ys = [int(m) for m in re.findall(r"\\pos\(540,(\d+)\)", ass.read_text(encoding="utf-8-sig"))]
    assert all(y <= 0.66 * 1920 for y in ys)


def test_cta_event_emitted(tmp_path, cfg):
    ass = tmp_path / "c.ass"
    write_ass(_words(), ass, cfg, "karaoke-pop", anchor=0.60,
              cta={"enabled": True, "text": "Follow for more", "duration_s": 1.5},
              clip_duration=10.0)
    text = ass.read_text(encoding="utf-8-sig")
    assert "Follow for more" in text


def test_legacy_no_anchor_has_no_pos(tmp_path, cfg):
    ass = tmp_path / "c.ass"
    write_ass(_words(), ass, cfg, "karaoke-pop")  # anchor=None -> legacy
    assert r"\pos(" not in ass.read_text(encoding="utf-8-sig")
