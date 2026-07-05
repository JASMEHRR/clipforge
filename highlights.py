"""Highlight selection: candidate windows → LLM scoring (schema-enforced,
retry → repair inside llm.py) → rule-based fallback scorer. All results are
sentence-snapped and hard-bounded to [min_seconds, max_seconds].

Also: post-render re-scoring (weighted 1–10) with the drop-bottom-30% /
keep-at-least-3 rule."""
from __future__ import annotations

import json
import math
from pathlib import Path

import llm
from config import load_config
from errors import LLMError
from logutil import get_logger
from schemas import validate

log = get_logger("highlights")

HOOK_KEYWORDS = {
    "secret", "mistake", "never", "always", "why", "how", "truth", "surprising",
    "best", "worst", "stop", "start", "nobody", "everyone", "actually", "wrong",
    "important", "amazing", "danger", "warning", "remember", "imagine", "problem",
    "simple", "easy", "free", "first", "last", "biggest", "question", "story",
}

PROMPT_TEMPLATE = """TASK: You are a short-form video editor. From the numbered
candidate windows of a talk transcript below, choose the {n} moments most likely
to succeed as standalone 30-60 second vertical clips.

SELECTION CRITERIA (all must be weighed):
- Strong hook within the first 3 seconds of the window
- Emotionally engaging, surprising, or informative
- Understandable with zero outside context
- Avoid filler, long pauses, low-energy passages
- Prefer clean sentence boundaries

CONSTRAINTS:
- Use ONLY the start/end times of the given windows (seconds, may be adjusted
  slightly but must stay within the transcript).
- Duration of every chosen clip must be between {min_s} and {max_s} seconds.
- "hook" = the opening line a viewer hears first, quoted from the transcript.
- Return AT MOST {n} candidates, ranked strongest first.

OUTPUT SCHEMA (respond with ONLY this JSON, no prose):
{schema}

EXAMPLE OUTPUT:
{{"candidates": [{{"start": 12.4, "end": 58.1, "hook": "Most projects fail for one reason.", "reason": "strong contrarian hook, self-contained argument", "score": 8.7}}]}}

CANDIDATE WINDOWS:
{windows}
"""


# ------------------------------------------------------------ window building

def build_windows(transcript: dict, cfg: dict, max_windows: int = 24) -> list[dict]:
    """Sentence-aligned candidate windows within [min,max] seconds."""
    min_s, max_s = cfg["clips"]["min_seconds"], cfg["clips"]["max_seconds"]
    sents = transcript["sentences"]
    windows = []
    i = 0
    while i < len(sents):
        j, start = i, sents[i]["start"]
        while j < len(sents) and sents[j]["end"] - start < min_s:
            j += 1
        if j < len(sents) and sents[j]["end"] - start <= max_s:
            windows.append({
                "start": round(start, 3),
                "end": round(sents[j]["end"], 3),
                "text": " ".join(s["text"] for s in sents[i:j + 1]),
            })
        i += 2
    if len(windows) > max_windows:  # spread evenly across the video
        step = len(windows) / max_windows
        windows = [windows[int(k * step)] for k in range(max_windows)]
    return windows


def mechanical_windows(duration: float, cfg: dict) -> list[dict]:
    """No transcript (silent/synthetic audio): fixed windows so the pipeline
    still runs end-to-end mechanically."""
    span = (cfg["clips"]["min_seconds"] + cfg["clips"]["max_seconds"]) / 2
    out, t, n = [], 0.0, 1
    while t + cfg["clips"]["min_seconds"] <= duration and len(out) < 8:
        end = min(t + span, duration)
        out.append({"start": round(t, 3), "end": round(end, 3),
                    "text": f"Segment {n}"})
        t += span
        n += 1
    return out


# ------------------------------------------------------- sentence snapping

def snap_to_sentences(start: float, end: float, sentences: list[dict],
                      min_s: float, max_s: float) -> tuple[float, float] | None:
    """Snap (start, end) to sentence boundaries with duration in [min_s, max_s].
    Pure function (unit-tested). Returns None when impossible."""
    if not sentences:
        return None
    starts = [s["start"] for s in sentences]
    ends = [s["end"] for s in sentences]
    s_idx = min(range(len(starts)), key=lambda i: abs(starts[i] - start))
    snap_start = starts[s_idx]
    # candidate ends: sentence ends after snap_start, duration within bounds
    valid = [e for e in ends if min_s <= e - snap_start <= max_s]
    if valid:
        return round(snap_start, 3), round(min(valid, key=lambda e: abs(e - end)), 3)
    # nothing in bounds from this start — try earlier starts to gain room
    for i in range(s_idx - 1, -1, -1):
        valid = [e for e in ends if min_s <= e - starts[i] <= max_s]
        if valid:
            return round(starts[i], 3), round(max(valid), 3)
    return None


def _overlap_frac(a: tuple[float, float], b: tuple[float, float]) -> float:
    inter = max(0.0, min(a[1], b[1]) - max(a[0], b[0]))
    shorter = max(1e-6, min(a[1] - a[0], b[1] - b[0]))
    return inter / shorter


def _dedupe(cands: list[dict], max_keep: int) -> list[dict]:
    kept: list[dict] = []
    for c in sorted(cands, key=lambda c: -c["score"]):
        span = (c["start"], c["end"])
        if all(_overlap_frac(span, (k["start"], k["end"])) < 0.4 for k in kept):
            kept.append(c)
        if len(kept) >= max_keep:
            break
    return sorted(kept, key=lambda c: -c["score"])


# ---------------------------------------------------------- fallback scorer

def rule_based_candidates(windows: list[dict], cfg: dict) -> list[dict]:
    """Deterministic keyword + speech-energy heuristic (keyless fallback)."""
    out = []
    for w in windows:
        words = w["text"].split()
        dur = max(1e-6, w["end"] - w["start"])
        energy = len(words) / dur                     # words/sec ≈ speech energy
        kw = sum(1 for t in words if t.lower().strip(".,!?\"'") in HOOK_KEYWORDS)
        first = w["text"].split(".")[0]
        hook_bonus = 1.0 if any(t.lower().strip(".,!?\"'") in HOOK_KEYWORDS
                                for t in first.split()[:8]) else 0.0
        score = min(10.0, round(2.0 + energy * 1.6 + kw * 0.5 + hook_bonus, 2))
        out.append({"start": w["start"], "end": w["end"],
                    "hook": first.strip()[:120] or "An interesting moment",
                    "reason": f"rule-based: {kw} hook keywords, "
                              f"{energy:.1f} words/sec",
                    "score": score})
    return out


# ------------------------------------------------------------- main entry

def select_highlights(transcript: dict, scenes: dict, duration: float,
                      cfg: dict | None = None, provider: str | None = None,
                      debug_dir: str | Path | None = None) -> list[dict]:
    """Returns validated, sentence-snapped, deduped candidate list (desc score)."""
    cfg = cfg or load_config()
    ccfg = cfg["clips"]
    min_s, max_s, max_n = ccfg["min_seconds"], ccfg["max_seconds"], ccfg["max_candidates"]

    mechanical = not transcript["sentences"]
    windows = (mechanical_windows(duration, cfg) if mechanical
               else build_windows(transcript, cfg))
    if not windows:
        log.warning("video too short for %ds clips — single full-length window",
                    min_s)
        windows = [{"start": 0.0, "end": min(duration, max_s),
                    "text": transcript["text"][:500] or "Full video"}]

    if mechanical:
        cands = [{"start": w["start"], "end": w["end"], "hook": w["text"],
                  "reason": "mechanical window (no transcript)", "score": 5.0}
                 for w in windows]
    else:
        cands = _llm_or_fallback(windows, cfg, provider, min_s, max_s, max_n,
                                 debug_dir)

    snapped = []
    for c in cands:
        if mechanical:
            snapped.append(c)
            continue
        r = snap_to_sentences(c["start"], c["end"], transcript["sentences"],
                              min_s, max_s)
        if r is None:
            log.warning("candidate %.1f-%.1f could not be snapped in bounds — dropped",
                        c["start"], c["end"])
            continue
        snapped.append({**c, "start": r[0], "end": r[1]})

    final = _dedupe(snapped, max_n)
    result = {"candidates": final}
    validate(result, "highlight_candidates")
    log.info("selected %d candidates (mechanical=%s)", len(final), mechanical)
    if debug_dir:
        Path(debug_dir).mkdir(parents=True, exist_ok=True)
        (Path(debug_dir) / "candidates.json").write_text(
            json.dumps(result, indent=2), encoding="utf-8")
    return final


def _llm_or_fallback(windows, cfg, provider, min_s, max_s, max_n, debug_dir):
    listing = "\n".join(
        f"[{i}] {w['start']:.1f}s–{w['end']:.1f}s: {w['text'][:400]}"
        for i, w in enumerate(windows))
    from schemas import SCHEMAS as _S
    prompt = PROMPT_TEMPLATE.format(n=max_n, min_s=min_s, max_s=max_s,
                                    schema=json.dumps(_S["highlight_candidates"]),
                                    windows=listing)
    if debug_dir:
        Path(debug_dir).mkdir(parents=True, exist_ok=True)
        (Path(debug_dir) / "highlight_prompt.txt").write_text(
            prompt, encoding="utf-8")
    try:
        out = llm.complete_json("highlight_candidates", "highlight_candidates",
                                prompt, provider=provider,
                                context={"windows": windows}, cfg=cfg)
        cands = out["candidates"]
        if not cands:
            raise LLMError("LLM returned zero candidates")
        if debug_dir:
            (Path(debug_dir) / "highlight_llm_raw.json").write_text(
                json.dumps(out, indent=2), encoding="utf-8")
        return cands
    except LLMError as e:
        log.warning("LLM highlight scoring failed (%s) — rule-based fallback", e)
        return rule_based_candidates(windows, cfg)


# ------------------------------------------------------------- re-scoring

def rescore_clips(clips: list[dict], transcript: dict,
                  cfg: dict | None = None, provider: str | None = None) -> list[dict]:
    """Score each rendered clip 1–10 on four axes, compute the weighted score,
    keep the top keep_ratio but never fewer than min_keep. Adds
    'scores'/'weighted_score'/'kept' to each clip dict; returns clips desc."""
    cfg = cfg or load_config()
    weights = cfg["clips"]["rescore_weights"]
    for clip in clips:
        text = _slice_text(transcript, clip["start"], clip["end"])
        prompt = (
            "TASK: Rate this short-form clip transcript 1-10 on each axis.\n"
            "AXES: hook_strength (first 3 seconds), retention potential, "
            "clarity out of context, emotional/informational impact.\n"
            "CONSTRAINTS: numbers 1-10, JSON only.\n"
            "OUTPUT SCHEMA: {\"hook_strength\": n, \"retention\": n, "
            "\"clarity\": n, \"impact\": n}\n"
            f"CLIP ({clip['end'] - clip['start']:.0f}s): {text[:1500]}")
        try:
            scores = llm.complete_json("clip_score", "clip_score", prompt,
                                       provider=provider,
                                       context={"text": text}, cfg=cfg)
        except LLMError as e:
            log.warning("rescore failed for clip %.1f (%s) — neutral fallback",
                        clip["start"], e)
            scores = {k: 5.0 for k in ("hook_strength", "retention",
                                       "clarity", "impact")}
        clip["scores"] = scores
        clip["weighted_score"] = round(
            sum(scores[k] * weights[k] for k in weights), 3)

    clips.sort(key=lambda c: -c["weighted_score"])
    n = len(clips)
    keep = max(min(cfg["clips"]["min_keep"], n),
               math.ceil(n * cfg["clips"]["keep_ratio"]))
    for i, c in enumerate(clips):
        c["kept"] = i < keep
    if n <= cfg["clips"]["min_keep"]:
        log.info("only %d candidates existed — keeping all (min-keep rule)", n)
    else:
        log.info("rescore: kept %d of %d clips (dropped bottom %d)",
                 keep, n, n - keep)
    return clips


def _slice_text(transcript: dict, start: float, end: float) -> str:
    ws = [w["word"] for w in transcript["words"]
          if w["start"] >= start - 0.05 and w["end"] <= end + 0.05]
    return " ".join(ws)
