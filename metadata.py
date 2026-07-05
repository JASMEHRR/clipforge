"""Per-clip social metadata: title (<60 chars, hook-first), 2-sentence
description, 8-12 hashtags. One LLM call with strict JSON; deterministic
template fallback guarantees every clip ALWAYS ships valid metadata."""
from __future__ import annotations

import json
import re
from collections import Counter

import llm
from config import load_config
from errors import LLMError
from logutil import get_logger
from schemas import SCHEMAS, validate

log = get_logger("metadata")

STAPLE_TAGS = ["#shorts", "#reels", "#tiktok", "#viral", "#fyp", "#video",
               "#clips", "#trending", "#learn", "#creator", "#daily", "#watch"]

STOPWORDS = set("""a an and are as at be but by for from has have he her his i
if in is it its just me my not of on or our she so that the their them they
this to was we were what when which who will with you your about all can could
do did get got had how like more most no now one only other out over some such
than then there these up us very would""".split())

PROMPT = """TASK: Write social metadata for a short vertical video clip.
CONSTRAINTS:
- title: under 60 characters, the hook comes first, no quotes, no emojis
- description: exactly 2 sentences; the first line is the hook
- hashtags: 8 to 12, each starting with #, letters/digits/underscore only
OUTPUT SCHEMA (respond with ONLY this JSON):
{schema}
EXAMPLE OUTPUT:
{{"title": "Most projects fail for one reason", "description": "Most projects fail for one reason. Here is the single question that keeps yours alive.", "hashtags": ["#productivity", "#builder", "#startup", "#focus", "#shipit", "#devlife", "#shorts", "#reels"]}}
CLIP HOOK: {hook}
CLIP TRANSCRIPT:
{text}
"""


def generate_metadata(clip_text: str, hook: str, cfg: dict | None = None,
                      provider: str | None = None) -> dict:
    """Returns ClipMetadata (schema-validated) — LLM first, template fallback."""
    cfg = cfg or load_config()
    prompt = PROMPT.format(schema=json.dumps(SCHEMAS["clip_metadata"]),
                           hook=hook[:200], text=clip_text[:2000])
    try:
        data = llm.complete_json("clip_metadata", "clip_metadata", prompt,
                                 provider=provider,
                                 context={"text": clip_text, "hook": hook},
                                 cfg=cfg)
        return data
    except LLMError as e:
        log.warning("metadata LLM failed (%s) — template fallback", e)
        data = template_metadata(hook, clip_text)
        validate(data, "clip_metadata")
        return data


def template_metadata(hook: str, text: str) -> dict:
    """Deterministic generator: title = trimmed hook sentence (≤60);
    description = hook + one summary sentence; hashtags = top transcript
    keywords + platform staples (always 8-12, always pattern-valid)."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    hook_s = (hook or (sentences[0] if sentences else "")).strip()
    title = _trim_title(hook_s) or "Watch this moment"

    summary = next((s for s in sentences if s and s != hook_s), "")
    description = " ".join(x for x in [
        hook_s if hook_s.endswith((".", "!", "?")) else hook_s + ".",
        summary or "Watch the full moment for the payoff.",
    ] if x).strip()

    words = [w.lower().strip(".,!?\"'():;") for w in text.split()]
    keywords = [w for w, _ in Counter(
        w for w in words
        if len(w) > 3 and w.isalpha() and w not in STOPWORDS).most_common(7)]
    tags = [f"#{k}" for k in keywords]
    for t in STAPLE_TAGS:
        if len(tags) >= 10:
            break
        if t not in tags:
            tags.append(t)
    tags = [t for t in tags if re.fullmatch(r"#[A-Za-z0-9_]+", t)][:12]
    while len(tags) < 8:  # pathological input — pad from staples
        for t in STAPLE_TAGS:
            if t not in tags:
                tags.append(t)
                break

    return {"title": title, "description": description, "hashtags": tags}


def _trim_title(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip().strip('"')
    if len(s) <= 60:
        return s.rstrip(".")[:60]
    cutoff = s[:57]
    if " " in cutoff:
        cutoff = cutoff[:cutoff.rfind(" ")]
    return cutoff + "..."


if __name__ == "__main__":
    demo = template_metadata(
        "Most projects fail not because of bad code.",
        "Most projects fail not because of bad code. They fail because of "
        "unclear goals. Write the one sentence that explains who needs this.")
    validate(demo, "clip_metadata")
    print(json.dumps(demo, indent=2))
