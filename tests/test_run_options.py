"""Per-run option threading + watermark filter (Feature 5, overnight run)."""
import copy

import config as cfgmod
from captions import _logo_graph, _wm_mode, cta_from_cfg, watermark_filter


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


def test_wm_mode_backward_compat():
    assert _wm_mode({"mode": "image"}) == "image"
    assert _wm_mode({"mode": "off"}) == "off"
    assert _wm_mode({"enabled": True}) == "text"       # legacy config, no mode
    assert _wm_mode({"enabled": False}) == "off"
    assert _wm_mode({}) == "off"


def test_logo_graph_single_pass_overlay():
    g = _logo_graph(["subtitles=x"], {"scale": 0.2, "opacity": 0.85,
                                      "position": "bottom-right", "margin_px": 30})
    # one filtergraph: alpha logo scaled to frame width, overlaid, ends at [vout]
    assert "colorchannelmixer=aa=0.85" in g
    assert "scale2ref=w=main_w*0.2" in g
    assert g.endswith("[vout]") and "overlay=W-w-30:H-h-30" in g
    assert "[0:v]subtitles=x[base0]" in g


def test_logo_graph_empty_vf_uses_null():
    g = _logo_graph([], {"position": "top-right"})
    assert "[0:v]null[base0]" in g


def test_apply_run_options_image_watermark(cfg):
    out = cfgmod.apply_run_options(cfg, {"watermark_mode": "image",
                                         "watermark_image": "assets/user_branding/logo.png"})
    wm = out["captions"]["watermark"]
    assert wm["mode"] == "image" and wm["enabled"] is False
    assert wm["image_path"].endswith("logo.png")


def test_font_family_override(cfg):
    preset = cfg["captions"]["preset"]
    out = cfgmod.apply_run_options(cfg, {"font_family": "Montserrat Black",
                                         "preset": preset})
    assert out["captions"]["presets"][preset]["font"] == "Montserrat Black"


def test_cta_from_cfg_enabled():
    # No-refine path must still carry the CTA when config enables it (the fix for
    # CTA text silently dropped without Style Refinement).
    cfg = {"style": {"cta": {"enabled": True, "text": "Sub now", "duration_s": 1.5}}}
    assert cta_from_cfg(cfg) == {"cta": cfg["style"]["cta"]}


def test_cta_from_cfg_disabled_or_blank():
    assert cta_from_cfg({"style": {"cta": {"enabled": False, "text": "x"}}}) == {}
    assert cta_from_cfg({"style": {"cta": {"enabled": True, "text": "  "}}}) == {}
    assert cta_from_cfg({}) == {}                       # missing style/cta → no-op


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
    test_wm_mode_backward_compat()
    test_logo_graph_single_pass_overlay()
    test_logo_graph_empty_vf_uses_null()
    test_apply_run_options_image_watermark(c)
    test_font_family_override(c)
    test_cta_from_cfg_enabled()
    test_cta_from_cfg_disabled_or_blank()
    print("ok")
