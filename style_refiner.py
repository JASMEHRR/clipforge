"""Timeline refiner: turns a rough highlight window into an EditPlan that the
existing cut → reframe → captions path renders ONCE.

Runs after highlight selection, before rendering. Operates purely on source
timestamps + the word-level transcript — never on finished pixels — so caption
re-chunking / repositioning stays possible and clips are re-encoded a single
time. Every style value comes from the StyleProfile + cfg["style"]; nothing
here hardcodes a style number.

Pipeline of one clip:
  start fixer (trim fillers/silence → self-contained-hook check → shift/flag)
  → ending optimizer (trim tail → completeness check → extend/pull-back/flag)
  → pacing cleaner (compress long pauses into segments, remap word timeline)
  → existing-subtitle decision (replace / keep / ignore).

LLM classifiers (hook self-containment, ending completeness) go through
llm.complete_json with the standard ladder; a deterministic rule-based
heuristic is the fallback AND the answer under --provider mock, so results are
stable with no key.
"""
from __future__ import annotations

import argparse
import json
import re

from config import load_config
from errors import StyleError
from llm import complete_json, resolve_provider
from logutil import get_logger
from schemas import validate

log = get_logger("style_refiner")

# Sentence openers that make a sentence depend on unseen prior context.
_DEPENDENT_OPENERS = {
    "and", "but", "so", "because", "which", "or", "nor", "yet", "then",
    "also", "plus", "besides", "however", "therefore", "thus", "anyway",
    "meanwhile", "otherwise", "instead",
}
# Bare pronouns that, sentence-initial, usually reference unseen antecedents.
_DEPENDENT_PRONOUNS = {
    "it", "he", "she", "they", "them", "this", "that", "these", "those",
    "him", "her", "his", "their", "its",
}
# Words/phrases a resolved ending must NOT trail off on.
_CLIFFHANGER_TAILS = {
    "and", "but", "so", "because", "or", "then", "which", "that", "if",
    "when", "while", "as", "to", "of", "for", "with", "the", "a", "an",
}
_QUESTION_WORDS = {"what", "why", "how", "who", "when", "where", "which",
                   "is", "are", "do", "does", "did", "can", "could", "would",
                   "should", "will"}
_SHOCK_WORDS = {"never", "always", "everyone", "nobody", "nothing", "everything",
                "shocking", "secret", "truth", "actually", "insane", "crazy",
                "worst", "best", "biggest", "warning", "danger", "die", "death",
                "money", "millions", "billion"}


def _tokens(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-zA-Z']+", text.lower()) if w]


# --- rule heuristics (shared with style_profile.py) ------------------------

def rule_hook_type(text: str) -> str:
    """Classify opening-sentence hook type without an LLM."""
    t = text.strip()
    toks = _tokens(t)
    if not toks:
        return "statement"
    if t.endswith("?") or toks[0] in _QUESTION_WORDS:
        return "question"
    if any(w in _SHOCK_WORDS for w in toks):
        return "shocking-statement"
    # Curiosity gap: teases a reveal without delivering it.
    if any(p in t.lower() for p in ("here's why", "the reason", "this is what",
                                    "you won't believe", "watch what", "wait for",
                                    "what happens", "the secret")):
        return "curiosity-gap"
    return "statement"


def rule_self_contained(text: str) -> bool:
    """True when a viewer with no prior context can follow the opening."""
    toks = _tokens(text)
    if not toks:
        return False
    if toks[0] in _DEPENDENT_OPENERS:
        return False
    # "That's why ...", "this means ..." style back-references.
    if toks[0] in _DEPENDENT_PRONOUNS and len(toks) >= 2:
        # A pronoun immediately followed by a verb-ish word reads as a reference.
        return False
    return True


def rule_ending_complete(text: str, min_words: int = 4) -> bool:
    """True when the final sentence resolves the thought."""
    toks = _tokens(text)
    if len(toks) < min_words:
        return False
    stripped = text.strip()
    if stripped and stripped[-1] not in ".!?…":
        # No terminal punctuation → likely cut mid-thought.
        if toks[-1] in _CLIFFHANGER_TAILS:
            return False
    if toks[-1] in _CLIFFHANGER_TAILS:
        return False
    return True


def _is_strong(hook_type: str) -> bool:
    return hook_type in ("question", "shocking-statement", "curiosity-gap")


# --- LLM-or-rule classifiers -----------------------------------------------

_HOOK_PROMPT = (
    "You judge whether the FIRST sentence of a short video clip works as a "
    "standalone hook for a viewer who saw nothing before it.\n"
    "Return JSON {{\"self_contained\": bool, \"hook_type\": one of "
    "[question, shocking-statement, curiosity-gap, statement], \"reason\": str}}.\n"
    "self_contained is false if it opens with a dependent connector "
    "(And/But/So/Because/Which...) or an unresolved pronoun reference.\n\n"
    "Sentence: {text}"
)
_ENDING_PROMPT = (
    "Does this clip's ending resolve the thought for a viewer who saw nothing "
    "else? Return JSON {{\"complete\": bool, \"reason\": str}}. It is incomplete "
    "if it trails off on a connector/cliffhanger (but/so/because/and then/which "
    "means...) or is a sentence fragment.\n\n"
    "Final sentence: {text}"
)


def classify_hook(text: str, cfg: dict, provider: str | None) -> dict:
    if resolve_provider(cfg, provider) == "mock":
        return {"self_contained": rule_self_contained(text),
                "hook_type": rule_hook_type(text), "reason": "rule(mock)"}
    try:
        return complete_json("hook_classify", "hook_classify",
                             _HOOK_PROMPT.format(text=text), provider=provider,
                             context={"sentence": text}, cfg=cfg)
    except StyleError:
        raise
    except Exception:  # noqa: BLE001 — any LLM failure falls back to the rule
        return {"self_contained": rule_self_contained(text),
                "hook_type": rule_hook_type(text), "reason": "rule(fallback)"}


def classify_ending(text: str, cfg: dict, provider: str | None) -> dict:
    min_words = int(cfg["style"].get("min_ending_words", 4))
    if resolve_provider(cfg, provider) == "mock":
        return {"complete": rule_ending_complete(text, min_words),
                "reason": "rule(mock)"}
    try:
        return complete_json("ending_classify", "ending_classify",
                             _ENDING_PROMPT.format(text=text), provider=provider,
                             context={"sentence": text}, cfg=cfg)
    except Exception:  # noqa: BLE001
        return {"complete": rule_ending_complete(text, min_words),
                "reason": "rule(fallback)"}


# --- transcript helpers -----------------------------------------------------

def _words_in(words: list[dict], s0: float, e0: float) -> list[dict]:
    return [w for w in words if w["end"] > s0 + 1e-6 and w["start"] < e0 - 1e-6]


def _sentences_overlapping(sentences: list[dict], s0: float, e0: float) -> list[dict]:
    return [s for s in sentences if s["end"] > s0 + 1e-6 and s["start"] < e0 - 1e-6]


def clamp_anchor(value: float) -> float:
    """CAPTION POSITION LAW: block center always inside [0.52, 0.66]."""
    return round(min(0.66, max(0.52, float(value))), 4)


# --- pacing cleaner (pure) --------------------------------------------------

def compress_pauses(words: list[dict], s0: float, e0: float, cfg: dict):
    """Compress long inter-word pauses into segments + a remapped word timeline.

    Returns (segments, out_words, output_duration, total_removed). Segments are
    ordered source spans [[start,end],...]; out_words are clip-relative,
    monotonic, non-negative, with the last end <= output_duration.
    """
    scfg = cfg["style"]
    clips = cfg["clips"]
    max_pause = float(scfg["max_pause_s"])
    target = min(0.5, max(0.25, float(scfg["target_pause_s"])))
    min_seconds = float(clips["min_seconds"])
    span = max(0.0, e0 - s0)

    win = _words_in(words, s0, e0)
    if len(win) < 2 or span <= 0:
        return [[round(s0, 3), round(e0, 3)]], _rebase(win, s0, []), round(span, 3), 0.0

    gaps = []
    for a, b in zip(win, win[1:]):
        g = b["start"] - a["end"]
        if g > max_pause:
            gaps.append({"a_end": a["end"], "b_start": b["start"], "g": g})

    # Budget: cap by removal ratio AND by keeping duration >= min_seconds.
    budget = min(float(scfg["max_removal_ratio"]) * span,
                 max(0.0, span - min_seconds))
    selected = []
    total_removed = 0.0
    for gp in sorted(gaps, key=lambda x: x["g"], reverse=True):
        save = gp["g"] - target
        if save <= 0:
            continue
        if total_removed + save <= budget + 1e-9:
            selected.append(gp)
            total_removed += save

    selected.sort(key=lambda x: x["a_end"])
    cuts = []  # (cut_start, cut_end, removed_len) in source time
    for gp in selected:
        cut_start = gp["a_end"] + target / 2.0
        cut_end = gp["b_start"] - target / 2.0
        if cut_end > cut_start:
            cuts.append((cut_start, cut_end, cut_end - cut_start))

    segments = []
    seg_start = s0
    for cs, ce, _ in cuts:
        segments.append([round(seg_start, 3), round(cs, 3)])
        seg_start = ce
    segments.append([round(seg_start, 3), round(e0, 3)])

    output_duration = span - sum(c[2] for c in cuts)
    out_words = _rebase(win, s0, cuts)
    return segments, out_words, round(output_duration, 3), round(sum(c[2] for c in cuts), 3)


def _rebase(win: list[dict], s0: float, cuts) -> list[dict]:
    """Shift kept words into output time, subtracting removed chunks before each."""
    out = []
    prev_end = 0.0
    for w in win:
        removed_before = sum(rl for cs, ce, rl in cuts if ce <= w["start"] + 1e-9)
        os_ = max(0.0, w["start"] - s0 - removed_before)
        oe_ = max(os_, w["end"] - s0 - removed_before)
        os_ = max(os_, prev_end)  # enforce monotonic (guards tiny fp overlaps)
        oe_ = max(oe_, os_)
        prev_end = oe_
        out.append({"word": w["word"], "start": round(os_, 3), "end": round(oe_, 3)})
    return out


# --- existing-subs decision -------------------------------------------------

def decide_existing_subs(subs: dict, cfg: dict, mode: str | None):
    """Return the existing_subs EditPlan block (mode auto|replace|keep|ignore)."""
    scfg = cfg["style"]["existing_subs"]
    mode = (mode or scfg.get("mode", "auto")).lower()
    max_band = float(scfg.get("max_band_ratio", 0.18))
    none = {"mode": mode, "decision": "none", "reason": "no burned subtitles",
            "bottom_exclusion_ratio": 0.0, "h_bias_center": -1.0}

    if mode == "ignore" or not subs.get("present"):
        return none, False, True   # block, subs_kept_flag, captions_enabled
    band_h = float(subs["band_bottom_pct"]) - float(subs["band_top_pct"])
    excludable = band_h <= max_band

    def replace_block(reason):
        return {"mode": mode, "decision": "replace", "reason": reason,
                "bottom_exclusion_ratio": round(1.0 - float(subs["band_top_pct"]), 4),
                "h_bias_center": -1.0}, False, True

    def keep_block(reason):
        return {"mode": mode, "decision": "keep", "reason": reason,
                "bottom_exclusion_ratio": 0.0, "h_bias_center": 0.5}, True, False

    if mode == "replace":
        return replace_block("forced replace") if excludable else \
            keep_block("band too tall to exclude; keeping source subs")
    if mode == "keep":
        return keep_block("forced keep")
    # auto
    if excludable:
        return replace_block(f"band {band_h:.2f} <= {max_band}; crop above and recaption")
    return keep_block(f"band {band_h:.2f} > {max_band}; cannot exclude, keep source subs")


# --- main entry -------------------------------------------------------------

def load_profile(cfg: dict) -> dict | None:
    from config import ROOT
    rel = cfg.get("style", {}).get("profile")
    if not rel:
        return None
    path = ROOT / rel
    if not path.exists():
        log.warning("style profile %s not found; using config defaults", rel)
        return None
    prof = json.loads(path.read_text(encoding="utf-8"))
    validate(prof, "style_profile")
    return prof


def refine_clip(candidate: dict, transcript: dict, scenes: dict, subs: dict,
                profile: dict | None, cfg: dict | None = None,
                provider: str | None = None, subs_mode: str | None = None) -> dict:
    """Produce a schema-valid EDIT_PLAN for one highlight candidate."""
    cfg = cfg or load_config()
    scfg = cfg["style"]
    clips = cfg["clips"]
    words = transcript["words"]
    sentences = transcript["sentences"]
    fillers = {w.lower() for w in scfg.get("filler_openers", [])}
    min_s, max_s = float(clips["min_seconds"]), float(clips["max_seconds"])

    s0 = float(candidate["start"])
    e0 = float(candidate["end"])
    flags: list[str] = []
    start_action = "keep"
    ending_action = "keep"
    zoom_punch = False

    # --- START FIXER ---
    win = _words_in(words, s0, e0)
    if win:
        i = 0
        while i < len(win) and re.sub(r"[^a-z]", "", win[i]["word"].lower()) in fillers:
            i += 1
        if i < len(win) and win[i]["start"] > s0 + 0.01:
            s0 = win[i]["start"]
            start_action = "trim_silence"

    open_sents = _sentences_overlapping(sentences, s0, e0)
    if open_sents:
        first = open_sents[0]
        hk = classify_hook(first["text"], cfg, provider)
        if not (hk["self_contained"] and _is_strong(hk["hook_type"])):
            # Search forward for a self-contained (ideally strong) opener.
            window_end = s0 + float(scfg["hook_search_window_s"])
            best_sc = None
            for s in open_sents:
                if s["start"] > window_end or (e0 - s["start"]) < min_s:
                    continue
                h = classify_hook(s["text"], cfg, provider)
                if h["self_contained"] and _is_strong(h["hook_type"]):
                    best_sc = s
                    break
                if h["self_contained"] and best_sc is None:
                    best_sc = s
            if best_sc is not None and best_sc["start"] > s0 + 0.01:
                s0 = best_sc["start"]
                start_action = "shift_to_hook"
                final_hook = classify_hook(best_sc["text"], cfg, provider)
                if not _is_strong(final_hook["hook_type"]):
                    flags.append("weak_hook")
                    zoom_punch = True
            else:
                # No strong hook available anywhere in the window.
                flags.append("weak_hook")
                zoom_punch = True

    # --- ENDING OPTIMIZER ---
    win = _words_in(words, s0, e0)
    tail = float(scfg["tail_ms"]) / 1000.0
    if win:
        last_end = win[-1]["end"]
        trimmed_e = min(e0, last_end + tail)
        if trimmed_e < e0 - 0.01:
            ending_action = "trim_tail"
        e0 = trimmed_e

    end_sents = _sentences_overlapping(sentences, s0, e0)
    if end_sents:
        last_sent = end_sents[-1]
        comp = classify_ending(last_sent["text"], cfg, provider)
        if not comp["complete"]:
            resolved = False
            for s in sentences:
                if s["start"] >= last_sent["end"] - 1e-6 and (s["end"] - s0) <= max_s:
                    if classify_ending(s["text"], cfg, provider)["complete"]:
                        e0 = s["end"]
                        ending_action = "extend_forward"
                        resolved = True
                        break
            if not resolved:
                # Pull back to the latest earlier complete sentence >= min_seconds.
                for s in reversed([x for x in end_sents if x is not last_sent]):
                    if (s["end"] - s0) >= min_s and \
                            classify_ending(s["text"], cfg, provider)["complete"]:
                        e0 = s["end"]
                        ending_action = "pull_back"
                        resolved = True
                        break
            if not resolved:
                flags.append("unresolved_ending")

    # --- PACING CLEANER ---
    segments, out_words, output_duration, total_removed = compress_pauses(
        words, s0, e0, cfg)

    # --- EXISTING SUBS ---
    subs_block, subs_kept, captions_enabled = decide_existing_subs(subs, cfg, subs_mode)
    if subs_kept:
        flags.append("subs_kept")

    # --- assemble ---
    unresolved = "unresolved_ending" in flags
    anchor_src = (profile or {}).get("captions", {}).get(
        "vertical_anchor", scfg["captions"]["vertical_anchor"])
    plan = {
        "segments": segments,
        "words": out_words,
        "output_duration": output_duration,
        "start_action": start_action,
        "ending_action": ending_action,
        "total_ms_removed": round(total_removed * 1000.0, 1),
        "flags": flags,
        "zoom_punch": zoom_punch,
        "fades": {
            "audio_in_ms": float(scfg["audio_fade_in_ms"]),
            "audio_out_ms": float(max(scfg["audio_fade_out_ms"],
                                      scfg["unresolved_fade_ms"] if unresolved else 0)),
            "video_out_ms": float(scfg["unresolved_fade_ms"] if unresolved else 0),
        },
        "captions_enabled": captions_enabled,
        "caption_anchor": clamp_anchor(anchor_src),
        "cta": {
            "enabled": bool(scfg["cta"]["enabled"]),
            "text": str(scfg["cta"]["text"]),
            "duration_s": float(scfg["cta"]["duration_s"]),
        },
        "existing_subs": subs_block,
    }
    validate(plan, "edit_plan")
    return plan


def summarize(plan: dict) -> dict:
    """Compact EditPlan summary for metadata.json / job log."""
    return {
        "segments": len(plan["segments"]),
        "ms_removed": plan["total_ms_removed"],
        "start_action": plan["start_action"],
        "ending_action": plan["ending_action"],
        "existing_subs": plan["existing_subs"]["decision"],
        "cta": plan["cta"]["enabled"],
        "caption_anchor": plan["caption_anchor"],
        "flags": plan["flags"],
    }


def _self_check() -> None:
    """Synthetic transcript → assert EditPlan invariants (no ffmpeg/LLM key)."""
    cfg = load_config()
    # ~44s of words (comfortably above min_seconds) with one 2s dead-air gap.
    words = []
    t = 0.0
    for k in range(80):
        if k == 40:
            t += 2.0  # a 2s pause
        words.append({"word": f"w{k}", "start": round(t, 3), "end": round(t + 0.4, 3)})
        t += 0.5
    sentences = [
        {"text": "Here is a shocking fact you need to know.",
         "start": words[0]["start"], "end": words[39]["end"],
         "words": words[:40]},
        {"text": "And that is the whole story here today, folks.",
         "start": words[40]["start"], "end": words[-1]["end"],
         "words": words[40:]},
    ]
    transcript = {"text": "x", "language": "en",
                  "duration": words[-1]["end"], "words": words, "sentences": sentences}
    cand = {"start": 0.0, "end": words[-1]["end"], "hook": "h", "reason": "r", "score": 7.0}
    scenes = {"scenes": [{"index": 0, "start": 0.0, "end": words[-1]["end"]}]}
    subs = {"present": False, "band_top_pct": 0.0, "band_bottom_pct": 0.0,
            "confidence": 0.0, "sampled_frames": 0}
    plan = refine_clip(cand, transcript, scenes, subs, None, cfg, provider="mock")
    # Invariants.
    assert plan["words"], "should keep words"
    assert plan["words"] == sorted(plan["words"], key=lambda w: w["start"]), "monotonic"
    assert all(w["start"] >= 0 for w in plan["words"]), "non-negative"
    assert plan["words"][-1]["end"] <= plan["output_duration"] + 1e-6, "within duration"
    assert plan["caption_anchor"] >= 0.52 and plan["caption_anchor"] <= 0.66, "anchor band"
    # The 2s pause exceeds max_pause and should be compressed.
    assert plan["total_ms_removed"] > 0, "should compress the dead-air gap"
    assert plan["output_duration"] >= cfg["clips"]["min_seconds"] - 1e-6 or \
        plan["output_duration"] <= words[-1]["end"], "duration sane"
    print("style_refiner self-check OK:", json.dumps(summarize(plan)))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="style_refiner self-check")
    ap.parse_args()
    _self_check()
