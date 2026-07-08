"""Per-run option threading + watermark filter (Feature 5, overnight run)."""
import copy

import config as cfgmod
from captions import watermark_filter


def test_hex_to_ass_forms():
    assert cfgmod.hex_to_ass("#FF0000") == "&H000000FF"   # red → BGR
    assert cfgmod.hex_to_ass("00FF00") == "&H0000FF00"
    assert cfgmod.hex_to_ass("&H0012AB34") == "&H0012AB34"  # passthrough
    assert cfgmod.hex_to_ass("rgb(255, 0, 0)") == "&H000000FF"


def test_apply_run_options_is_pure(cfg):
    before = copy.deepcopy(cfg)
    out = cfgmod.apply_run_options(cfg, {"cta_text": "Sub now",
                                         "pacing": 1.0, "clip_min": 20,
                                         "clip_max": 45})
    assert cfg == before                       # singleton untouched
    assert out["style"]["cta"]["text"] == "Sub now"
    assert out["style"]["cta"]["enabled"] is True
    assert out["clips"]["min_seconds"] == 20
    assert out["clips"]["max_seconds"] == 45


def test_pacing_bounds(cfg):
    gentle = cfgmod.apply_run_options(cfg, {"pacing": 0.0})["style"]
    tight = cfgmod.apply_run_options(cfg, {"pacing": 1.0})["style"]
    assert gentle["max_pause_s"] > tight["max_pause_s"]
    assert 0.25 <= tight["target_pause_s"] <= 0.5


def test_highlight_color_override(cfg):
    preset = cfg["captions"]["preset"]
    out = cfgmod.apply_run_options(cfg, {"highlight_hex": "#00FF00",
                                         "preset": preset})
    assert out["captions"]["presets"][preset]["highlight_color"] == "&H0000FF00"


def test_clip_length_range_guard(cfg):
    out = cfgmod.apply_run_options(cfg, {"clip_min": 60, "clip_max": 30})
    assert out["clips"]["min_seconds"] <= out["clips"]["max_seconds"]


def test_empty_opts_is_noop(cfg):
    before = copy.deepcopy(cfg)
    out = cfgmod.apply_run_options(cfg, {})
    assert out == before


def test_watermark_filter_positions():
    f = watermark_filter({"text": "@me", "position": "bottom-right",
                          "font_size": 36, "opacity": 0.6, "margin_px": 40})
    assert "drawtext=" in f and "@me" in f and "w-tw-40" in f
    center = watermark_filter({"text": "x", "position": "center"})
    assert "(w-tw)/2" in center


if __name__ == "__main__":
    from config import load_config
    c = load_config()
    test_hex_to_ass_forms()
    test_apply_run_options_is_pure(c)
    test_pacing_bounds(c)
    test_highlight_color_override(c)
    test_clip_length_range_guard(c)
    test_empty_opts_is_noop(c)
    test_watermark_filter_positions()
    print("ok")
