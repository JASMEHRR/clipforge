"""Pure-logic tests for style_refiner: heuristics, pause math, remap invariants,
anchor clamp, existing-subs ladder, and an end-to-end mock EditPlan."""
import style_refiner as sr
from schemas import validate


def _words(n, spacing=0.5, dur=0.4, gap_at=None, gap=2.0, start=0.0):
    ws, t = [], start
    for k in range(n):
        if gap_at is not None and k == gap_at:
            t += gap
        ws.append({"word": f"w{k}", "start": round(t, 3), "end": round(t + dur, 3)})
        t += spacing
    return ws


# --- heuristics ---

def test_self_contained_rules():
    assert sr.rule_self_contained("The atomic bomb is dangerous.")
    assert not sr.rule_self_contained("And then it exploded everywhere.")
    assert not sr.rule_self_contained("But that changed everything.")
    assert not sr.rule_self_contained("It was the biggest mistake.")


def test_hook_type_rules():
    assert sr.rule_hook_type("Why does this happen?") == "question"
    assert sr.rule_hook_type("What happens next will shock you.") in (
        "question", "curiosity-gap", "shocking-statement")
    assert sr.rule_hook_type("Nobody expected the money to vanish.") == "shocking-statement"
    assert sr.rule_hook_type("The dog walked home slowly.") == "statement"


def test_ending_complete_rules():
    assert sr.rule_ending_complete("That is how you stay safe today.")
    assert not sr.rule_ending_complete("Because of the")
    assert not sr.rule_ending_complete("and then")  # trails on connector
    assert not sr.rule_ending_complete("Too short")  # < min words


# --- anchor clamp ---

def test_anchor_clamp_band():
    assert sr.clamp_anchor(0.40) == 0.52
    assert sr.clamp_anchor(0.90) == 0.66
    assert sr.clamp_anchor(0.60) == 0.60


# --- pause compression + remap ---

def test_compress_removes_long_pause(cfg):
    words = _words(80, gap_at=40, gap=2.0)
    s0, e0 = 0.0, words[-1]["end"]
    segs, out, dur, removed = sr.compress_pauses(words, s0, e0, cfg)
    assert len(segs) == 2, "one cut for the single long pause"
    assert removed > 0
    # Remap invariants.
    assert out == sorted(out, key=lambda w: w["start"]), "monotonic"
    assert all(w["start"] >= 0 and w["end"] >= w["start"] for w in out)
    assert out[-1]["end"] <= dur + 1e-6, "last word within output duration"
    assert abs((e0 - s0) - removed - dur) < 1e-3, "duration accounting"


def test_short_gaps_not_removed(cfg):
    words = _words(80)  # all gaps 0.1s, below max_pause
    segs, out, dur, removed = sr.compress_pauses(words, 0.0, words[-1]["end"], cfg)
    assert removed == 0.0
    assert len(segs) == 1


def test_removal_respects_min_seconds(cfg):
    # Clip barely above min: removing the pause would drop below min_seconds,
    # so the guardrail must forbid it.
    mn = cfg["clips"]["min_seconds"]
    words = _words(int((mn + 1) / 0.5), gap_at=5, gap=3.0)
    s0, e0 = 0.0, words[-1]["end"]
    _, _, dur, removed = sr.compress_pauses(words, s0, e0, cfg)
    assert dur >= mn - 1e-6, "never trim below min_seconds"


def test_removal_respects_max_ratio(cfg):
    words = _words(140, spacing=0.5, gap_at=None)
    # Inject several big pauses.
    for idx in (20, 40, 60, 80, 100):
        for w in words[idx:]:
            w["start"] += 3.0
            w["end"] += 3.0
    s0, e0 = 0.0, words[-1]["end"]
    _, _, _, removed = sr.compress_pauses(words, s0, e0, cfg)
    assert removed <= cfg["style"]["max_removal_ratio"] * (e0 - s0) + 1e-6


# --- existing-subs ladder ---

def _subs(present, top=0.80, bottom=0.90):
    return {"present": present, "band_top_pct": top, "band_bottom_pct": bottom,
            "confidence": 1.0, "sampled_frames": 10}


def test_subs_auto_none(cfg):
    block, kept, caps = sr.decide_existing_subs(_subs(False), cfg, "auto")
    assert block["decision"] == "none" and caps and not kept


def test_subs_auto_replace_thin_band(cfg):
    block, kept, caps = sr.decide_existing_subs(_subs(True, 0.86, 0.96), cfg, "auto")
    assert block["decision"] == "replace" and caps
    assert block["bottom_exclusion_ratio"] > 0


def test_subs_auto_keep_tall_band(cfg):
    block, kept, caps = sr.decide_existing_subs(_subs(True, 0.55, 0.95), cfg, "auto")
    assert block["decision"] == "keep" and kept and not caps
    assert block["h_bias_center"] == 0.5


def test_subs_ignore_mode(cfg):
    block, kept, caps = sr.decide_existing_subs(_subs(True, 0.8, 0.9), cfg, "ignore")
    assert block["decision"] == "none" and caps


# --- end to end ---

def test_refine_clip_valid_editplan(cfg):
    words = _words(80, gap_at=40, gap=2.0)
    sentences = [
        {"text": "Here is a shocking fact you should know.",
         "start": words[0]["start"], "end": words[39]["end"], "words": words[:40]},
        {"text": "That is the full story for today.",
         "start": words[40]["start"], "end": words[-1]["end"], "words": words[40:]},
    ]
    transcript = {"text": "x", "language": "en", "duration": words[-1]["end"],
                  "words": words, "sentences": sentences}
    cand = {"start": 0.0, "end": words[-1]["end"], "hook": "h", "reason": "r", "score": 7.0}
    scenes = {"scenes": [{"index": 0, "start": 0.0, "end": words[-1]["end"]}]}
    plan = sr.refine_clip(cand, transcript, scenes, _subs(False), None, cfg,
                          provider="mock")
    validate(plan, "edit_plan")
    assert plan["total_ms_removed"] > 0
    assert 0.52 <= plan["caption_anchor"] <= 0.66
