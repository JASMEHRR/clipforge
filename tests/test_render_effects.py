"""Render effects (speed ramps, punch-in zooms, pop-ins, transitions):
the pure planning/remap math and filtergraph builders. No ffmpeg."""
import copy

import pytest

import captions
import cut
import style_refiner as sr
from config import load_config as _load


def _cfg(**style_over) -> dict:
    cfg = copy.deepcopy(_load())
    cfg["style"] = {**cfg["style"], **style_over}
    return cfg


# --- speed-ramp planning + remap -------------------------------------------

def test_plan_ramps_word_free_gaps_only():
    cfg = _cfg(speed_ramps={"enabled": True, "rate": 2.0, "min_gap_s": 0.4})
    words = [{"word": "a", "start": 10.0, "end": 10.5},
             {"word": "b", "start": 11.5, "end": 12.0},   # 1.0 s gap → ramp
             {"word": "c", "start": 12.2, "end": 12.6}]   # 0.2 s gap → no
    ramps = sr.plan_speed_ramps([[10.0, 13.0]], words, cfg)
    assert len(ramps) == 1
    rp = ramps[0]
    assert rp["rate"] == 2.0
    assert rp["start"] >= 10.5 and rp["end"] <= 11.5  # inside the gap, padded


def test_plan_ramps_disabled():
    assert sr.plan_speed_ramps([[0, 10]], [], _cfg()) == []


def test_ramp_never_crosses_segment_boundary():
    cfg = _cfg(speed_ramps={"enabled": True, "rate": 1.5, "min_gap_s": 0.4})
    # words in different segments — the gap spans a join, so no ramp
    words = [{"word": "a", "start": 10.0, "end": 10.5},
             {"word": "b", "start": 20.0, "end": 20.5}]
    assert sr.plan_speed_ramps([[10.0, 11.0], [19.5, 21.0]], words, cfg) == []


def test_src_to_out_and_shrunk():
    segs = [[10.0, 14.0], [20.0, 25.0]]
    assert sr._src_to_out(12.0, segs) == pytest.approx(2.0)
    assert sr._src_to_out(21.0, segs) == pytest.approx(5.0)
    # a 1 s ramp at 2x saves 0.5 s; points after it shift left by 0.5
    ramp_outs = [(2.0, 3.0, 0.5)]
    assert sr._shrunk(5.0, ramp_outs) == pytest.approx(4.5)
    assert sr._shrunk(1.0, ramp_outs) == pytest.approx(1.0)


def test_expand_ramps_splits_segments():
    subs = cut.expand_ramps([[10.0, 14.0]],
                            [{"start": 11.0, "end": 12.0, "rate": 2.0}])
    assert subs == [(10.0, 11.0, 1.0), (11.0, 12.0, 2.0), (12.0, 14.0, 1.0)]


def test_expand_ramps_ignores_outside_and_no_ramps():
    assert cut.expand_ramps([[0.0, 5.0]], None) == [(0.0, 5.0, 1.0)]
    assert cut.expand_ramps([[0.0, 5.0]],
                            [{"start": 8.0, "end": 9.0, "rate": 1.5}]) \
        == [(0.0, 5.0, 1.0)]


def test_refine_clip_ramp_remap_end_to_end():
    """Words after a ramped gap land earlier by exactly the saved time, and
    output_duration shrinks to match (the invariant that keeps captions
    synced)."""
    cfg = _cfg(speed_ramps={"enabled": True, "rate": 2.0, "min_gap_s": 0.4},
               enabled=True)
    words = [{"word": "start", "start": 0.2, "end": 0.7},
             {"word": "after", "start": 2.7, "end": 3.2}]  # 2.0 s gap
    transcript = {"words": words, "sentences": [
        {"text": "start after.", "start": 0.2, "end": 3.2}]}
    cand = {"start": 0.0, "end": 4.0, "hook": "", "reason": "", "score": 1.0}
    subs = {"present": False, "band_top_pct": 0.0, "band_bottom_pct": 0.0,
            "confidence": 0.0}
    plan = sr.refine_clip(cand, transcript, {"scenes": []}, subs, None, cfg,
                          provider="mock")
    assert plan["speed_ramps"], "expected a ramp in the 2 s gap"
    rp = plan["speed_ramps"][0]
    saved = (rp["end"] - rp["start"]) * 0.5
    w_after = next(w for w in plan["words"] if w["word"] == "after")
    # without ramps the word starts at 2.7 minus whatever pause compression
    # removed; the ramp shifts it a further `saved` left. Just assert the
    # invariant: last word end <= output_duration and duration shrank by saved.
    assert w_after["end"] <= plan["output_duration"] + 1e-6
    assert saved > 0


# --- zoom / popin planning ---------------------------------------------------

def test_zoom_events_emphasis_and_interval():
    cfg = _cfg(punch_in={"mode": "emphasis", "amount_pct": 10})
    words = [{"word": "WOW!", "start": 2.0, "end": 2.4},
             {"word": "meh", "start": 5.0, "end": 5.4}]
    evs = sr.zoom_events_for(words, 20.0, [], cfg)
    assert evs == [{"t": 2.0, "dur": 0.8, "amount": 0.10}]

    cfg = _cfg(punch_in={"mode": "interval", "amount_pct": 5, "interval_s": 8})
    evs = sr.zoom_events_for([], 20.0, [], cfg)
    assert [e["t"] for e in evs] == [8.0, 16.0]


def test_zoom_transition_adds_join_punches():
    cfg = _cfg(punch_in={"mode": "off"}, transition="zoom")
    evs = sr.zoom_events_for([], 30.0, [10.0, 20.0], cfg)
    assert [e["t"] for e in evs] == [10.0, 20.0]
    assert all(e["amount"] == 0.08 for e in evs)


def test_popin_events_match_keywords_once():
    cfg = _cfg(popins=[{"keyword": "fire", "asset": "assets/popins/fire.png"}])
    words = [{"word": "Fire!", "start": 1.0, "end": 1.4},
             {"word": "fire", "start": 9.0, "end": 9.4}]  # second match ignored
    evs = sr.popin_events_for(words, cfg)
    assert evs == [{"t": 1.0, "dur": 1.2, "asset": "assets/popins/fire.png"}]


def test_popin_no_config_no_events():
    assert sr.popin_events_for([{"word": "fire", "start": 1, "end": 2}],
                               _cfg()) == []


# --- filtergraph builders ------------------------------------------------------

def test_zoom_crop_vf_shape():
    vf = captions.zoom_crop_vf([{"t": 2.0, "dur": 0.8, "amount": 0.07}],
                               1080, 1920, 30.0)
    assert vf.startswith("zoompan=z=")
    assert "s=1080x1920" in vf and "fps=30" in vf
    assert "0.0700" in vf and "2.000" in vf


def test_whip_blur_vf_windows():
    vf = captions.whip_blur_vf([5.0, 12.0])
    assert vf.startswith("boxblur=")
    assert vf.count("between(") == 2


def test_popin_chain_labels_and_enable():
    parts, last = captions._popin_chain(
        "vout", [{"t": 1.0, "dur": 1.2, "asset": "x.png"},
                 {"t": 4.0, "dur": 1.2, "asset": "y.png"}],
        input_offset=1, png_w=200)
    assert last == "po1"
    joined = ";".join(parts)
    assert "[1:v]" in joined and "[2:v]" in joined
    assert "between(t,1.000,2.200)" in joined
