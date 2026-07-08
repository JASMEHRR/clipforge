"""Virality rating: explainable **engagement signals** for each clip.

True virality is not predictable, so this is deliberately framed as a
breakdown of engagement signals (never a guarantee). Six sub-scores (0-10),
each computed from data the pipeline already has, combine into a 0-100 total
and a display band (Strong / Promising / Weak):

    hook · completeness · pacing · captions · duration · delivery

Weights and their rationale (with sources) are documented in RESEARCH.md.
Everything scoring-related is a pure function so it is fully unit-testable and
key-free; a real LLM rubric (the ``clip_score`` task) is folded in as one extra
signal only when a real provider is configured. The legacy
``rate_virality`` API and its {score, verdict, reasons} shape are preserved.
"""
from __future__ import annotations

import json
import math

import llm
from config import load_config
from errors import LLMError
from highlights import HOOK_KEYWORDS
from logutil import get_logger
from schemas import SCHEMAS, validate

log = get_logger("virality")

# Weights per sub-score (sum to 1.0). See RESEARCH.md for the evidence.
SIGNAL_WEIGHTS = {
    "hook": 0.28,
    "completeness": 0.18,
    "pacing": 0.16,
    "captions": 0.14,
    "duration": 0.14,
    "delivery": 0.10,
}
DURATION_SWEET_SPOT = 45.0   # seconds — centre of the 30-60s plateau (research)

# Emotion / curiosity words that tend to lift short-form retention.
EMOTION_WORDS = {
    "love", "hate", "fear", "shock", "shocking", "insane", "crazy", "unbelievable",
    "incredible", "amazing", "terrible", "worst", "best", "painful", "hilarious",
    "heartbreaking", "furious", "excited", "obsessed", "regret", "proud", "afraid",
    "wow", "never", "always", "secret", "warning", "danger", "mistake", "truth",
    "nobody", "everyone", "shouldn't", "can't", "won't", "life-changing",
}

_VERDICTS = ("post", "maybe", "skip")


def _gaussian(x: float, mu: float, sigma: float) -> float:
    """0..1 peak at mu."""
    return math.exp(-((x - mu) ** 2) / (2.0 * sigma * sigma))


def _verdict(score: float) -> str:
    return "post" if score >= 70 else "maybe" if score >= 40 else "skip"


def rule_based_virality(text: str, hook: str, duration: float) -> dict:
    """Deterministic 0-100 score from pacing, emotion density, length fit, and
    hook strength. Returns {score, verdict, reasons}."""
    words = [w.lower().strip(".,!?\"'():;") for w in (text or "").split()]
    n = len(words)
    dur = max(1e-6, float(duration))

    pacing = n / dur                                   # words per second
    pace_fit = _gaussian(pacing, 2.8, 1.1)             # conversational sweet spot
    emo = sum(1 for w in words if w in EMOTION_WORDS)
    emo_density = emo / max(n, 1)
    emo_fit = min(1.0, emo_density * 12.0)             # ~8%+ emotional words saturates
    length_fit = _gaussian(dur, 42.0, 12.0)            # ~35-50s sweet spot
    hook_words = [w.lower().strip(".,!?\"'():;")
                  for w in (hook or "").split()[:8]]
    hook_fit = min(1.0, sum(1 for w in hook_words
                            if w in HOOK_KEYWORDS or w in EMOTION_WORDS) / 2.0)

    score = 100.0 * (0.35 * hook_fit + 0.25 * pace_fit
                     + 0.20 * emo_fit + 0.20 * length_fit)
    score = int(round(max(0.0, min(100.0, score))))

    reasons = [
        f"hook: {'strong' if hook_fit >= 0.5 else 'weak'} opener "
        f"({int(hook_fit * 100)}%)",
        f"pacing: {pacing:.1f} words/sec",
        f"emotion: {emo} charged word(s) ({emo_density * 100:.0f}%)",
        f"length: {dur:.0f}s ({'in' if length_fit >= 0.6 else 'off'} sweet spot)",
    ]
    return {"score": score, "verdict": _verdict(score), "reasons": reasons}


# --------------------------------------------------------------------------
# Virality v2 — explainable engagement signals (pure, unit-tested)
# --------------------------------------------------------------------------

def _clamp10(x: float) -> float:
    return round(max(0.0, min(10.0, x)), 1)


def _band(total: float) -> str:
    return "Strong" if total >= 70 else "Promising" if total >= 45 else "Weak"


def _norm_words(s: str) -> list[str]:
    return [w.lower().strip(".,!?\"'():;") for w in (s or "").split()]


def score_hook(hook: str, text: str, weak_hook: bool,
               self_contained) -> tuple[float, str]:
    """First-3s pull: keyword/emotion content, minus a weak-hook penalty, plus
    a self-contained-opening bonus."""
    hw = _norm_words(hook)[:8]
    hits = sum(1 for w in hw if w in HOOK_KEYWORDS or w in EMOTION_WORDS)
    base = min(1.0, hits / 2.0) * 8.0            # up to 8 from hook content
    if not hw and text:                          # no explicit hook → sample text
        tw = _norm_words(text)[:8]
        base = min(1.0, sum(1 for w in tw if w in HOOK_KEYWORDS
                            or w in EMOTION_WORDS) / 2.0) * 6.0
    if self_contained is True:
        base += 2.0
    if weak_hook:
        base -= 3.0
    s = _clamp10(base)
    tag = "strong" if s >= 6 else "soft" if s >= 3 else "weak"
    return s, f"{tag} opener" + (" (flagged weak_hook)" if weak_hook else "")


def score_completeness(self_contained, ending_complete) -> tuple[float, str]:
    """Reuses the refiner's start self-containment + ending completeness. Neutral
    when the refiner did not run (style disabled)."""
    if self_contained is None and ending_complete is None:
        return 5.0, "not evaluated (style off)"
    s = 4.0
    if self_contained:
        s += 3.0
    if ending_complete:
        s += 3.0
    bits = []
    bits.append("self-contained start" if self_contained else "abrupt start")
    bits.append("clean ending" if ending_complete else "unresolved ending")
    return _clamp10(s), ", ".join(bits)


def score_pacing(words_per_sec: float, cuts_per_min: float,
                 prof_wps: float, prof_cpm: float) -> tuple[float, str]:
    """Speech + cut pacing measured against the active StyleProfile (relative,
    not absolute — a calm profile shouldn't be punished)."""
    wps_fit = _gaussian(words_per_sec, prof_wps or 2.8, 1.1)
    if prof_cpm and cuts_per_min is not None:
        cut_fit = _gaussian(cuts_per_min, prof_cpm, max(6.0, prof_cpm * 0.5))
        s = 10.0 * (0.6 * wps_fit + 0.4 * cut_fit)
    else:
        s = 10.0 * wps_fit
    return _clamp10(s), f"{words_per_sec:.1f} w/s vs profile {prof_wps or 2.8:.1f}"


def score_captions(words_per_line, prof_wpl, coverage: float,
                   emphasis_present: bool, enabled: bool) -> tuple[float, str]:
    """Muted-viewing readability: short lines, good coverage, emphasis present."""
    if not enabled:
        return 3.0, "captions disabled"
    wpl = words_per_line or prof_wpl or 3
    read_fit = _gaussian(float(wpl), 3.0, 1.6)   # ~3 words/line reads best
    s = 10.0 * (0.5 * read_fit + 0.4 * max(0.0, min(1.0, coverage)))
    if emphasis_present:
        s += 1.0
    return _clamp10(s), (f"{wpl} words/line, {coverage * 100:.0f}% coverage"
                         + (", emphasis" if emphasis_present else ""))


def score_duration(duration: float) -> tuple[float, str]:
    """Distance from the researched 30-60s sweet spot (peak ~45s)."""
    s = 10.0 * _gaussian(float(duration), DURATION_SWEET_SPOT, 15.0)
    fit = "in" if s >= 6 else "off"
    return _clamp10(s), f"{duration:.0f}s ({fit} 30-60s sweet spot)"


def score_delivery(energy_variance) -> tuple[float, str]:
    """Vocal dynamism from audio energy variance (0..1 normalised). Neutral when
    unavailable."""
    if energy_variance is None:
        return 5.0, "energy not measured"
    s = 10.0 * max(0.0, min(1.0, float(energy_variance)))
    tag = "dynamic" if s >= 6 else "steady" if s >= 3 else "flat"
    return _clamp10(s), f"{tag} delivery"


def engagement_signals(features: dict) -> dict:
    """Pure composer: features dict → {score, band, verdict, signals[], reasons}.

    Every input has a sensible neutral default so the score degrades gracefully
    when a signal is unavailable. ``signals`` is the visible breakdown; ``score``
    (0-100) and ``verdict`` preserve the legacy contract.
    """
    f = features
    subs = {
        "hook": score_hook(f.get("hook", ""), f.get("text", ""),
                           bool(f.get("weak_hook")), f.get("self_contained")),
        "completeness": score_completeness(f.get("self_contained"),
                                           f.get("ending_complete")),
        "pacing": score_pacing(f.get("words_per_sec", 2.8),
                               f.get("cuts_per_min"),
                               f.get("prof_wps", 2.8), f.get("prof_cpm", 0.0)),
        "captions": score_captions(f.get("words_per_line"),
                                   f.get("prof_words_per_line", 3),
                                   f.get("caption_coverage", 0.0),
                                   bool(f.get("emphasis_present")),
                                   f.get("captions_enabled", True)),
        "duration": score_duration(f.get("duration", 0.0)),
        "delivery": score_delivery(f.get("energy_variance")),
    }
    total = sum(SIGNAL_WEIGHTS[k] * subs[k][0] for k in SIGNAL_WEIGHTS) * 10.0
    total = int(round(max(0.0, min(100.0, total))))
    signals = [{"name": k, "score": subs[k][0], "reason": subs[k][1]}
               for k in SIGNAL_WEIGHTS]
    reasons = [f"{k}: {subs[k][1]}" for k in ("hook", "completeness",
                                              "pacing", "duration")]
    return {"score": total, "band": _band(total), "verdict": _verdict(total),
            "signals": signals, "reasons": reasons}


def _feature_coverage(text: str, duration: float) -> float:
    """Rough caption coverage proxy: speaking density vs a spoken clip baseline
    (~2.8 w/s). 1.0 = words fill the clip, lower = long silent stretches."""
    n = len(_norm_words(text))
    if duration <= 0:
        return 0.0
    return max(0.0, min(1.0, (n / duration) / 2.8))


PROMPT = """TASK: Rate this short-form vertical video clip's viral potential 0-100.
Judge: hook strength in the first 3 seconds, speech pacing, emotional pull,
self-contained clarity, and whether the length fits a 35-50s sweet spot.
CONSTRAINTS:
- score: integer 0-100
- verdict: "post" (>=70), "maybe" (40-69), or "skip" (<40)
- reasons: 2-4 short strings, each naming a concrete factor
OUTPUT SCHEMA (respond with ONLY this JSON):
{schema}
CLIP LENGTH: {duration:.0f}s
CLIP HOOK (first seconds): {hook}
CLIP TRANSCRIPT:
{text}
"""


def _build_features(clip_text: str, hook: str, duration: float,
                    refine: dict | None, profile: dict | None,
                    extra: dict | None) -> dict:
    """Assemble the engagement-signal inputs from data the pipeline already has.
    Refiner flags/actions supply hook/completeness signals; the StyleProfile
    supplies the pacing/caption baselines. Missing pieces fall back to neutral."""
    extra = extra or {}
    flags = (refine or {}).get("flags", []) or []
    start_action = (refine or {}).get("start_action", "")
    ending_action = (refine or {}).get("ending_action", "")
    prof = profile or {}
    prof_pace = prof.get("pacing", {})
    prof_caps = prof.get("captions", {})
    n = len(_norm_words(clip_text))
    wps = extra.get("words_per_sec")
    if wps is None:
        wps = n / duration if duration > 0 else 2.8
    return {
        "hook": hook, "text": clip_text, "duration": float(duration),
        "weak_hook": "weak_hook" in flags,
        # refiner acting on start/ending is our signal that they were sound;
        # "unresolved_ending" flag marks a known-incomplete ending.
        "self_contained": None if refine is None else ("weak_hook" not in flags),
        "ending_complete": None if refine is None else (
            "unresolved_ending" not in flags),
        "words_per_sec": wps,
        "cuts_per_min": extra.get("cuts_per_min"),
        "prof_wps": prof_pace.get("words_per_sec", 2.8),
        "prof_cpm": prof_pace.get("scene_cuts_per_min", 0.0),
        "words_per_line": extra.get("words_per_line"),
        "prof_words_per_line": prof_caps.get("words_per_line", 3),
        "caption_coverage": extra.get("caption_coverage",
                                      _feature_coverage(clip_text, duration)),
        "emphasis_present": extra.get("emphasis_present",
                                      bool(prof_caps.get("emphasis"))),
        "captions_enabled": extra.get("captions_enabled", True),
        "energy_variance": extra.get("energy_variance"),
    }


def rate_virality(clip_text: str, hook: str, duration: float,
                  cfg: dict | None = None, provider: str | None = None,
                  refine: dict | None = None, profile: dict | None = None,
                  extra: dict | None = None) -> dict:
    """Explainable engagement-signal rating {score, band, verdict, signals,
    reasons}. Heuristic signals always compute (key-free, deterministic); a real
    LLM rubric (the ``clip_score`` task) is blended in as one extra signal only
    when a real provider is configured — under mock the heuristics carry it."""
    cfg = cfg or load_config()
    features = _build_features(clip_text, hook, duration, refine, profile, extra)
    result = engagement_signals(features)

    # Fold in an LLM rubric score only on a real provider (keeps the keyless
    # gate deterministic; mock's clip_score would just echo a heuristic seed).
    try:
        if llm.resolve_provider(cfg, provider) != "mock":
            rubric = _llm_rubric_score(clip_text, hook, duration, cfg, provider)
            if rubric is not None:
                blended = int(round(0.7 * result["score"] + 0.3 * rubric))
                result["signals"].append(
                    {"name": "llm_rubric", "score": round(rubric / 10.0, 1),
                     "reason": f"LLM rubric {rubric}/100"})
                result["score"] = blended
                result["band"] = _band(blended)
                result["verdict"] = _verdict(blended)
                result["reasons"].append(f"llm rubric: {rubric}/100")
    except Exception as e:  # noqa: BLE001 — LLM blend is best-effort
        log.warning("virality LLM blend skipped: %s", e)

    validate(result, "virality")
    return result


def _llm_rubric_score(clip_text: str, hook: str, duration: float,
                      cfg: dict, provider: str | None) -> float | None:
    """One 0-100 rubric number from the LLM via the existing virality task."""
    prompt = PROMPT.format(schema=json.dumps(SCHEMAS["virality"]),
                           duration=float(duration), hook=hook[:200],
                           text=clip_text[:2000])
    try:
        data = llm.complete_json(
            "virality", "virality", prompt, provider=provider,
            context={"text": clip_text, "hook": hook, "duration": duration},
            cfg=cfg)
        return float(data.get("score"))
    except (LLMError, TypeError, ValueError):
        return None


if __name__ == "__main__":
    text = ("This is the biggest mistake nobody warns you about. It will change "
            "how you work forever.")
    hook = "The biggest mistake nobody warns you about"
    demo = engagement_signals(_build_features(
        text, hook, 42.0,
        refine={"flags": [], "start_action": "kept", "ending_action": "kept"},
        profile=None, extra=None))
    validate(demo, "virality")
    # sanity: a strong hook + good length should not land in the Weak band
    assert demo["band"] in ("Strong", "Promising"), demo
    assert len(demo["signals"]) == 6
    print(json.dumps(demo, indent=2))
