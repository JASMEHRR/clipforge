"""Virality rating: score each clip 0-100 for short-form potential.

Signals: hook strength in the first ~3 seconds, speech pacing (words/sec),
emotional-keyword density, and length sweet spot (~35-50s). An LLM returns
{score, verdict, reasons[]}; a deterministic rule-based scorer is the keyless
fallback (and the mock provider's answer), so every clip always gets a rating.
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


def rate_virality(clip_text: str, hook: str, duration: float,
                  cfg: dict | None = None, provider: str | None = None) -> dict:
    """Returns a validated virality rating {score, verdict, reasons}. LLM first,
    deterministic rule-based fallback (also the mock provider's answer)."""
    cfg = cfg or load_config()
    fallback = rule_based_virality(clip_text, hook, duration)
    prompt = PROMPT.format(schema=json.dumps(SCHEMAS["virality"]),
                           duration=float(duration), hook=hook[:200],
                           text=clip_text[:2000])
    try:
        data = llm.complete_json(
            "virality", "virality", prompt, provider=provider,
            context={"text": clip_text, "hook": hook, "duration": duration},
            cfg=cfg)
        data["verdict"] = data.get("verdict") if data.get("verdict") in _VERDICTS \
            else _verdict(data["score"])
        return data
    except LLMError as e:
        log.warning("virality LLM failed (%s) — rule-based fallback", e)
        validate(fallback, "virality")
        return fallback


if __name__ == "__main__":
    demo = rule_based_virality(
        "This is the biggest mistake nobody warns you about. It will change "
        "how you work forever.", "The biggest mistake nobody warns you about",
        42.0)
    validate(demo, "virality")
    print(json.dumps(demo, indent=2))
