"""Niche classification for produced clips (library organization).

Assigns each clip one content niche (podcast, comedy, gaming, ...) from its
transcript and metadata. The keyword heuristic always runs (key-free,
deterministic); an LLM pick replaces it only when a real provider is
configured and the answer is on the allowed list. Classification is
best-effort context for organizing the library — classify_niche never
raises, so it can never block or fail a render.
"""
from __future__ import annotations

import json
import re

import llm
from logutil import get_logger

log = get_logger("classify")

# Built-in starter taxonomy. "other" is the universal fallback and must stay
# last so tie-breaks between real niches never lose to it.
NICHES = ["podcast", "comedy", "gaming", "sports", "finance", "education",
          "reactions", "other"]

# Keyword evidence per niche. Deliberately distinctive words only — generic
# terms shared across niches (game/score/player) are kept to the niche where
# they carry the most signal, and ties resolve by NICHES order.
_KEYWORDS: dict[str, list[str]] = {
    "podcast": ["podcast", "episode", "interview", "guest", "host",
                "listeners", "conversation", "talk show", "on the show",
                "my guest", "great question"],
    "comedy": ["funny", "joke", "jokes", "laugh", "laughing", "hilarious",
               "comedy", "comedian", "prank", "sketch", "standup",
               "stand-up", "punchline", "humor"],
    "gaming": ["gaming", "gamer", "gameplay", "video game", "video games",
               "level up", "boss fight", "loot", "fps", "speedrun", "clutch",
               "respawn", "loadout", "minecraft", "fortnite", "twitch",
               "console", "playstation", "xbox", "nintendo"],
    "sports": ["championship", "league", "tournament", "playoffs", "coach",
               "athlete", "stadium", "football", "soccer", "cricket",
               "basketball", "tennis", "baseball", "goalkeeper", "home run",
               "touchdown", "wicket", "final score", "season"],
    "finance": ["money", "invest", "investing", "investment", "stock",
                "stocks", "market", "crypto", "bitcoin", "business",
                "startup", "revenue", "profit", "entrepreneur", "salary",
                "wealth", "portfolio", "compound interest", "passive income",
                "net worth"],
    "education": ["learn", "learning", "lesson", "tutorial", "explain",
                  "explained", "science", "history", "physics", "math",
                  "study", "teacher", "professor", "course", "experiment",
                  "did you know", "fun fact", "how to"],
    "reactions": ["react", "reaction", "reacting", "no way", "can't believe",
                  "cannot believe", "insane", "unbelievable", "shocking",
                  "watch this", "wait for it", "look at this", "oh my god"],
    # "other" has no keywords: it wins only when nothing else scores.
}

_PROMPT = """Classify this short-form video clip into exactly ONE niche.

Allowed niches: {niches}

Clip title: {title}
Hashtags: {hashtags}
Transcript excerpt:
{text}

Respond with ONLY a JSON object: {{"niche": "<one allowed niche>"}}"""


def allowed_niches(cfg: dict) -> list[str]:
    """Built-in taxonomy plus the user's custom niches (Settings), deduped,
    lowercase, built-ins first so tie-breaks stay deterministic."""
    custom = cfg.get("classify", {}).get("custom_niches", []) or []
    out = list(NICHES)
    for name in custom:
        slug = str(name).strip().lower()
        if slug and slug not in out:
            out.append(slug)
    return out


def rule_based_niche(text: str, title: str = "",
                     hashtags: list | None = None) -> str:
    """Deterministic keyword classifier — the always-available, key-free path.
    Title and hashtag hits weigh 3x a transcript hit (they're author-chosen
    signal); best-scoring niche wins, ties resolve by NICHES order, no
    evidence at all -> "other"."""
    body = (text or "").lower()
    head = " ".join([title or ""] +
                    [str(h).lstrip("#") for h in (hashtags or [])]).lower()
    best, best_score = "other", 0
    for niche, words in _KEYWORDS.items():
        score = 0
        for kw in words:
            pat = r"\b" + re.escape(kw) + r"\b"
            score += 3 * len(re.findall(pat, head))
            score += len(re.findall(pat, body))
        if score > best_score:
            best, best_score = niche, score
    return best


def classify_niche(clip_text: str, meta: dict, cfg: dict,
                   provider: str | None = None) -> str:
    """One niche for a produced clip. Heuristic first (always works, keyless);
    an LLM pick replaces it only on a real provider AND when the answer is on
    the allowed list. Never raises — any failure keeps the heuristic result."""
    classify_cfg = cfg.get("classify", {})
    if not classify_cfg.get("enabled", True):
        return "other"
    title = meta.get("title", "")
    hashtags = meta.get("hashtags", [])
    niche = rule_based_niche(clip_text, title, hashtags)
    try:
        if llm.resolve_provider(cfg, provider) != "mock":
            allowed = allowed_niches(cfg)
            prompt = _PROMPT.format(niches=json.dumps(allowed), title=title,
                                    hashtags=" ".join(hashtags),
                                    text=(clip_text or "")[:2000])
            data = llm.complete_json("niche", "niche", prompt,
                                     provider=provider,
                                     context={"text": clip_text,
                                              "title": title},
                                     cfg=cfg)
            picked = str(data.get("niche", "")).strip().lower()
            if picked in allowed:
                niche = picked
            else:
                log.warning("LLM niche '%s' not in allowed list; keeping "
                            "heuristic '%s'", picked, niche)
    except Exception as e:  # noqa: BLE001 — classification is best-effort
        log.warning("LLM niche classification skipped: %s", e)
    return niche


if __name__ == "__main__":
    # Smoke self-check: each taxonomy entry is reachable and garbage is safe.
    assert rule_based_niche("we invest in stocks and the market") == "finance"
    assert rule_based_niche("that boss fight gameplay was a clutch speedrun") == "gaming"
    assert rule_based_niche("", "My podcast episode", ["#interview"]) == "podcast"
    assert rule_based_niche("asdf qwerty zxcv") == "other"
    assert rule_based_niche("") == "other"
    print("classify.py self-check OK")
