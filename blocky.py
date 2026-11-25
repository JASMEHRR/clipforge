"""Blocky mode: toy-brick 3D story Shorts ("What if..." / "In this town...").

The format going round Shorts and TikTok: blocky characters in a bright brick
town act out one absurd rule while a narrator tells the story, one word of
caption at a time. These are drawn by code, not by a video model:

  premise → story (LLM, or a hand-written --story-file) → clean + check
  → narration per shot (voice.py: edge-tts, Kokoro fallback) → timeline
  → Whisper word timings (captions, part-pop timing)
  → frames: assets/blocky/blocky.js (three.js) in headless Chrome/Edge,
    one deterministic frame per call, streamed into ffmpeg
  → mux narration → one-word pop captions → music bed + sound effects

Output uses the normal job layout so the Library, approvals and upload center
pick it up unchanged: output/<ts>_blocky-<slug>/clip_00/ (format "short").
Every clip needs an explicit approve before upload, like Create mode.

CLI:
  .venv/Scripts/python.exe blocky.py                          # next unused premise
  .venv/Scripts/python.exe blocky.py --premise "What if your house followed you to school?"
  .venv/Scripts/python.exe blocky.py --story-file my_story.json   # story written elsewhere (e.g. Claude)
  .venv/Scripts/python.exe blocky.py --provider mock              # keyless render check
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import random
import re
import shutil
import sys
import time
import urllib.parse
import uuid
from pathlib import Path

import create
import llm
import music
import voice
from config import ROOT, load_config
from errors import ClipForgeError
from ffutil import RawVideoWriter, verify_ffmpeg, video_encode_args
from logutil import add_file_handler, get_logger, remove_file_handler, stage_timer
from schemas import (BLOCKY_ACTIONS, BLOCKY_CAMERAS, BLOCKY_COLORS, BLOCKY_EXTRAS,
                     BLOCKY_FACES, BLOCKY_HAIR, BLOCKY_PLACES, BLOCKY_PROPS,
                     BLOCKY_SFX, BLOCKY_SHAPES, BLOCKY_SIZES, BLOCKY_SKINS,
                     SchemaValidationError, validate)

log = get_logger("blocky")

ASSET_DIR = ROOT / "assets" / "blocky"
FONT_DIR = ROOT / "assets" / "fonts"
ORIGIN = "http://blocky.clipforge"
USED_PATH = ROOT / "cache" / "blocky_used.json"
SIGN_MAX = 24


class BlockyError(ClipForgeError):
    stage = "blocky"


def _bcfg(cfg: dict) -> dict:
    c = cfg.get("blocky")
    if not isinstance(c, dict):
        raise BlockyError("config.yaml has no blocky section", retryable=False)
    return c


# ------------------------------------------------------------------ premises

def load_used() -> list[str]:
    """Premises already made (newest last). A missing or broken file means none."""
    try:
        data = json.loads(USED_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as e:
        log.warning("could not read %s (%s) - treating every premise as unused",
                    USED_PATH, e)
        return []
    return [str(x) for x in data] if isinstance(data, list) else []


def mark_used(premise: str) -> None:
    used = [p for p in load_used() if p.lower() != premise.lower()] + [premise]
    try:
        USED_PATH.parent.mkdir(parents=True, exist_ok=True)
        USED_PATH.write_text(json.dumps(used[-200:], indent=2), encoding="utf-8")
    except OSError as e:
        log.warning("could not record the used premise (%s)", e)


def pick_premise(seeds: list[str], used: list[str], rng: random.Random) -> str | None:
    """Pure. A random seed premise not made yet; None when every seed is used
    (the model then invents a fresh one)."""
    done = {u.strip().lower() for u in used}
    fresh = [s for s in seeds if s.strip() and s.strip().lower() not in done]
    return rng.choice(fresh) if fresh else None


# ------------------------------------------------------------------ story

def story_prompt(premise: str | None, avoid: list[str], cfg: dict) -> str:
    """The writer's brief. Built from what the hits share (research 2026-11-25:
    the laSenu "In this town" skill's measured rules, creator guides, the
    "Huss Huss Huss" burger/basketball trend): first person, one impossible
    rule said as normal, quick one-line shots, a recurring side character,
    three failed tries, a twist that pays off planted details, comment bait."""
    b = _bcfg(cfg)
    wmin, wmax = int(b.get("words_min", 180)), int(b.get("words_max", 280))
    smin, smax = int(b.get("shots_min", 20)), int(b.get("shots_max", 32))
    n_shots = (smin + smax) // 2
    ask = (f'Premise: "{premise}"' if premise else
           "Invent ONE fresh premise in one of the two formats below. Do not reuse:\n"
           + "\n".join(f"- {a}" for a in avoid[-40:]))
    return (
        "You write viral narrated story Shorts acted out by blocky toy-brick characters "
        "in a bright brick town. A program animates your shot list in 3D, so only use "
        "the options listed below.\n\n"
        f"{ask}\n\n"
        "TWO FORMATS THAT WORK (pick the one that fits the premise)\n"
        "A) RULE: 'In this town, <one impossible rule said like it is normal>.' Then "
        "how it works with one concrete example and a number, a small case and a huge "
        "case, town life under the rule, then 'Except me.' / 'And me? ...' (the hero is "
        "the exception or the worst person at surviving the rule).\n"
        "B) SERIOUS GRIND: 'Everyone in my town <does the fun thing>. But not me.' A "
        "dead-serious training monologue about something silly (wrapping burgers, "
        "dribbling, napping). Wake at five. Practice two hundred times. Get faster. "
        "Fail. Train harder. Win big at the end. Deadpan, never winking at the joke.\n\n"
        "STORY RULES (measured from the hits)\n"
        "- FIRST PERSON. The narrator is the hero, cast id 'me'. Short dialogue in "
        "quotes is good: 'Mom said, \"Not today.\"'\n"
        "- Shot 1's text is the hook: the rule or the 'everyone but me' line, under 14 "
        "words, and the hero is in shot 1.\n"
        "- One recurring side character (a mom, a best friend, a rival, a teacher) with "
        "a detail that comes back.\n"
        "- Plant two small details early that the twist pays off.\n"
        "- A problem, then THREE failed tries, then a last chance.\n"
        "- A big moment, then a twist that flips the start or pays off the details.\n"
        "- Last shot: a short line that makes people comment (a question tied to the "
        "rule, or 'Part two if this gets...'). No 'like and subscribe'.\n"
        f"- {n_shots} shots, {wmin}-{wmax} words in total. ONE sentence per shot, 6-12 "
        "words, plain words a ten-year-old knows, present or past tense, never slow.\n"
        "- Every sentence must be SHOWABLE with the cast, places, actions and props "
        "below (use a sign for numbers, prices and rules). No events the program cannot "
        "show (no transformations, no fire, no vehicles driving, no crowds of 50).\n"
        "- Comedy: exaggerated scale and numbers; two normal reactions then one absurd "
        "one; deadpan face next to chaos.\n"
        "- Write numbers as words. No emoji, no symbols, no brand names, no real people, "
        "nothing gory or truly scary, no meanness. Slapstick and heart.\n\n"
        "CAST (2-5 characters; ids lowercase; the hero's id is me)\n"
        f"- shape: {', '.join(BLOCKY_SHAPES)} (objects get a face, legs and arms).\n"
        f"- shirt, pants, hair_color: {', '.join(BLOCKY_COLORS)}\n"
        f"- skin: {', '.join(BLOCKY_SKINS)}; hair: {', '.join(BLOCKY_HAIR)}; extras "
        f"(optional): {', '.join(BLOCKY_EXTRAS)}\n"
        f"- size: {', '.join(BLOCKY_SIZES)}. SIZE IS PART OF THE JOKE: use giant and "
        "baby boldly and put big and small characters in the SAME shot so the "
        "difference shows. Give each character a distinct look.\n\n"
        "SHOTS\n"
        f"- place: {', '.join(BLOCKY_PLACES)}. Use 2-4 places and stay in each for "
        "several shots.\n"
        f"- camera: {', '.join(BLOCKY_CAMERAS)}. 'player' (raised three-quarter view "
        "showing the place and everyone in it) is the default: use it or wide for about "
        "half the shots. close_up only for a key reaction, at most one shot in seven, "
        "never twice in a row. top_down or player for shot 1. low_angle makes giants "
        "huge. reveal for the twist. shake for impacts. Never the same camera twice in "
        "a row.\n"
        "- focus: the id the camera frames, or group, or sign.\n"
        "- actors: 1-4 cast members in the shot. Put two or more in at least half the "
        "shots. Each has:\n"
        f"  action: {', '.join(BLOCKY_ACTIONS)} (lose_part pops a body part off for "
        "good; shrink and grow change size during the shot)\n"
        f"  face: {', '.join(BLOCKY_FACES)}\n"
        f"  prop held in hand: {', '.join(BLOCKY_PROPS)}\n"
        f"- sfx: {', '.join(BLOCKY_SFX)}. On visible events only, about one shot in four.\n"
        "- emphasis: 0-1 single word copied exactly from that shot's text, the word the "
        "joke lands on (it flashes yellow).\n"
        f"- sign: '' or a short sign in the scene (max {SIGN_MAX} characters): prices, "
        "counts, rules, 'ONE HOUR LATER'.\n\n"
        "ALSO: title (under 60 characters, hooky, no emoji), premise (the rule or grind "
        "line), description (2 sentences), hashtags: 8-12 like #InThisTown.\n\n"
        f"Write EXACTLY {n_shots} shots: the whole story, start to finish. Shape of one "
        "cast member and one shot (fields only; write your own story):\n"
        '{"id": "me", "name": "Milo", "shape": "person", "shirt": "red", '
        '"pants": "navy", "skin": "yellow", "hair": "short", "hair_color": "brown", '
        '"size": "kid", "extras": []}\n'
        '{"text": "My mom is the tallest lady in town.", "place": "street", '
        '"camera": "low_angle", "focus": "mom", "actors": [{"id": "mom", "action": '
        '"wave", "face": "smile", "prop": "none"}, {"id": "me", "action": "idle", '
        '"face": "smile", "prop": "book"}], "sfx": "none", "emphasis": ["tallest"], '
        '"sign": ""}\n'
        "Return only JSON matching the schema."
    )


def write_story(premise: str | None, avoid: list[str], cfg: dict,
                provider: str | None) -> dict:
    return create.ask("blocky_story", story_prompt(premise, avoid, cfg), cfg,
                      provider, {"premise": premise or ""})


def load_story_file(path: str | Path) -> dict:
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise BlockyError(f"story file not found: {p}", retryable=False) from e
    except (OSError, ValueError) as e:
        raise BlockyError(f"could not read story file {p}", detail=str(e),
                          retryable=False) from e
    try:
        validate(data, "blocky_story")
    except SchemaValidationError as e:
        raise BlockyError(f"story file {p.name} does not match the blocky story "
                          "format", detail=str(e), retryable=False) from e
    return data


DEFAULT_TAGS = ("#WhatIf", "#Animation", "#Story", "#Shorts", "#Funny", "#Cartoon",
                "#StoryTime", "#Comedy", "#3DAnimation", "#Blocky")


def clean_hashtags(tags: list[str]) -> list[str]:
    """Pure. 8-12 tags that pass CLIP_METADATA ('#' + letters/digits/_),
    deduped case-insensitively, topped up from DEFAULT_TAGS."""
    out: list[str] = []
    for t in list(tags) + list(DEFAULT_TAGS):
        tag = "#" + re.sub(r"[^A-Za-z0-9_]", "", str(t))
        if len(tag) > 1 and tag.lower() not in {o.lower() for o in out}:
            out.append(tag)
        if len(out) >= 12 or (len(out) >= 8 and t in DEFAULT_TAGS):
            break
    return out[:12]


_WORD = re.compile(r"[a-z0-9']+")


def _norm(word: str) -> str:
    return "".join(_WORD.findall(str(word).lower()))


# Common slips from free models -> the nearest option the renderer can draw.
ALIASES = {
    "action": {"walking": "walk", "running": "run", "jumping": "jump",
               "waving": "wave", "dancing": "dance", "crying": "cry",
               "falling": "fall", "spinning": "spin", "pointing": "point",
               "sitting": "sit", "talking": "talk", "floating": "float",
               "laugh": "celebrate", "cheer": "celebrate", "smile": "idle",
               "stand": "idle", "standing": "idle", "scared": "shock",
               "surprised": "shock", "shocked": "shock", "hide": "sneak"},
    "face": {"happy": "smile", "surprised": "shock", "scared": "shock",
             "shocked": "shock", "mad": "angry", "crying": "cry", "nervous": "worried",
             "excited": "grin", "laughing": "laugh", "confused": "worried",
             "proud": "smug", "scared_face": "shock", "dizzy": "ko"},
    "color": {"gray": "grey", "blonde": "yellow", "blond": "yellow", "ginger": "orange", "light_blue": "cyan", "dark_blue": "navy",
              "violet": "purple", "golden": "gold", "silver": "grey"},
    "skin": {"pale": "light", "fair": "light", "medium": "tan", "olive": "tan",
             "black": "dark", "white": "light"},
    "hair": {"bald": "none", "ponytail": "long", "hat": "cap", "curly": "short",
             "braids": "long", "mohawk": "spiky"},
    "place": {"home": "house", "living_room": "room", "bedroom": "room",
              "town": "street", "road": "street", "store": "shop",
              "school_yard": "school", "garden": "park"},
    "camera": {"close": "close_up", "closeup": "close_up", "zoom": "push_in",
               "zoom_in": "push_in", "low": "low_angle", "overhead": "top_down",
               "aerial": "top_down", "medium": "push_in"},
}


def _pick(value, allowed: tuple, default: str, alias: str, fixes: list[str],
          what: str) -> str:
    """Pure. `value` as one of `allowed` (case, spaces and aliases forgiven);
    otherwise `default`, with the swap noted in `fixes`."""
    v = re.sub(r"[\s\-]+", "_", str(value or "").strip().lower())
    v = ALIASES.get(alias, {}).get(v, v)
    if v in allowed:
        return v
    fixes.append(f"{what} '{value}' -> '{default}'")
    return default


def limit_close_ups(shots: list[dict], fixes: list[str], share: float = 1 / 6) -> None:
    """Pure, in place. Hits keep close-ups to ~10-15% of the time and never two
    in a row ("too close, one person, town dead" was the top fault of weak
    videos), so extra close-ups become the raised 'player' camera."""
    allowed = max(1, int(len(shots) * share))
    used = 0
    for i, s in enumerate(shots):
        if s["camera"] != "close_up":
            continue
        if used >= allowed or (i and shots[i - 1]["camera"] == "close_up"):
            s["camera"] = "player"
            fixes.append(f"shot {i + 1} close_up -> player")
        else:
            used += 1


def clean_story(story: dict) -> dict:
    """Pure. A schema-valid story made safe to render: shot actors point at
    cast indexes (unknown ids dropped, duplicates removed), focus becomes an
    index into the shot's actors, -1 (group) or "sign", signs are trimmed,
    emphasis keeps only words that are really in the text, narration is made
    speakable, and every option value is mapped onto what the renderer can
    draw (the swaps are listed in the result's "fixes"). Raises BlockyError
    when nothing renderable is left."""
    import explain
    fixes: list[str] = []
    cast, index = [], {}
    for c in story["cast"]:
        if c["id"] in index:
            continue
        index[c["id"]] = len(cast)
        who = c["id"]
        shape = _pick(c["shape"], BLOCKY_SHAPES, "person", "", fixes, f"{who} shape")
        cast.append({
            "id": who, "name": str(c["name"]).strip() or who, "shape": shape,
            "kind": "person" if shape == "person" else "object",
            "shirt": _pick(c["shirt"], BLOCKY_COLORS, "blue", "color", fixes, f"{who} shirt"),
            "pants": _pick(c["pants"], BLOCKY_COLORS, "navy", "color", fixes, f"{who} pants"),
            "skin": _pick(c["skin"], BLOCKY_SKINS, "yellow", "skin", fixes, f"{who} skin"),
            "hair": _pick(c["hair"], BLOCKY_HAIR, "short", "hair", fixes, f"{who} hair"),
            "hair_color": _pick(c["hair_color"], BLOCKY_COLORS, "brown", "color", fixes,
                                f"{who} hair colour"),
            "size": _pick(c["size"], BLOCKY_SIZES, "adult", "", fixes, f"{who} size"),
            "extras": [e for e in (str(x).strip().lower() for x in c.get("extras") or [])
                       if e in BLOCKY_EXTRAS]})
    if not cast:
        raise BlockyError("the story has no cast", retryable=False)
    shots: list[dict] = []
    for n, s in enumerate(story["shots"]):
        text = explain.speakable(str(s["text"]).strip())
        if not text:
            continue
        actors, seen = [], set()
        for a in s["actors"]:
            if a["id"] in index and a["id"] not in seen:
                seen.add(a["id"])
                tag = f"shot {n + 1} {a['id']}"
                actors.append({
                    "cast": index[a["id"]],
                    "action": _pick(a["action"], BLOCKY_ACTIONS, "idle", "action", fixes,
                                    f"{tag} action"),
                    "face": _pick(a["face"], BLOCKY_FACES, "neutral", "face", fixes,
                                  f"{tag} face"),
                    "prop": _pick(a["prop"], BLOCKY_PROPS, "none", "", fixes,
                                  f"{tag} prop")})
        if not actors:
            actors = [{"cast": 0, "action": "idle", "face": "neutral", "prop": "none"}]
        sign = re.sub(r"\s+", " ", str(s.get("sign") or "")).strip()[:SIGN_MAX] or None
        focus_id = str(s["focus"]).strip().lower()
        if focus_id == "sign" and sign:
            focus = "sign"
        elif focus_id in index and index[focus_id] in [a["cast"] for a in actors]:
            focus = [a["cast"] for a in actors].index(index[focus_id])
        elif focus_id == "group" and len(actors) > 1:
            focus = -1
        else:
            focus = 0
        if isinstance(focus, int) and focus > 0:
            # the framed character stands at the front of the layout
            actors.insert(0, actors.pop(focus))
            focus = 0
        words = {_norm(w) for w in text.split()}
        emphasis = [e for e in (s.get("emphasis") or []) if _norm(e) in words][:2]
        prev = shots[-1] if shots else None
        shots.append({
            "text": text,
            "place": _pick(s["place"], BLOCKY_PLACES, prev["place"] if prev else "street",
                           "place", fixes, f"shot {n + 1} place"),
            "camera": _pick(s["camera"], BLOCKY_CAMERAS,
                            "close_up" if prev and prev["camera"] != "close_up"
                            else "push_in", "camera", fixes, f"shot {n + 1} camera"),
            "focus": focus, "actors": actors,
            "sfx": _pick(s["sfx"], BLOCKY_SFX, "none", "", fixes, f"shot {n + 1} sfx"),
            "emphasis": emphasis, "sign": sign})
    if len(shots) < 3:
        raise BlockyError(f"only {len(shots)} usable shot(s) in the story",
                          retryable=False)
    limit_close_ups(shots, fixes)
    return {"title": story["title"].strip(), "premise": story["premise"].strip(),
            "description": story["description"].strip(),
            "hashtags": clean_hashtags(story["hashtags"]), "cast": cast, "shots": shots,
            "fixes": fixes}


# ------------------------------------------------------------------ timing

def spread_words(text: str, start: float, end: float) -> list[dict]:
    """Pure. Word timings for `text` spoken between start and end, each word
    getting time in proportion to its length (+1 for the gap). Used when
    Whisper is unavailable: we wrote the text, so only the timing is unknown."""
    tokens = text.split()
    if not tokens or end <= start:
        return []
    weights = [len(t) + 1 for t in tokens]
    total, t, out = float(sum(weights)), start, []
    for tok, wt in zip(tokens, weights):
        dur = (end - start) * wt / total
        out.append({"word": f" {tok}", "start": round(t, 3),
                    "end": round(t + dur * 0.85, 3)})
        t += dur
    return out


def voiced_span(path: str | Path, frame_s: float = 0.02,
                level: float = 0.04) -> tuple[float, float]:
    """(first, last) second where the line's audio is louder than `level` x
    its peak; the whole file when it is silent or unreadable."""
    import numpy as np
    import soundfile as sf
    try:
        data, sr = sf.read(str(path), dtype="float32")
    except (OSError, RuntimeError) as e:
        log.warning("could not read %s for timing (%s)", path, e)
        return 0.0, 0.0
    mono = data.mean(axis=1) if data.ndim > 1 else data
    hop = max(1, int(sr * frame_s))
    n = len(mono) // hop
    if n == 0:
        return 0.0, len(mono) / sr
    env = np.abs(mono[:n * hop]).reshape(n, hop).max(axis=1)
    loud = np.nonzero(env > level * max(float(env.max()), 1e-6))[0]
    if not len(loud):
        return 0.0, len(mono) / sr
    return float(loud[0] * frame_s), float((loud[-1] + 1) * frame_s)


def trim_line(path: str | Path, pad: float = 0.06) -> float:
    """Cut the dead air before and after one narrated line (TTS adds ~0.3-0.5 s
    at each end; on 26 lines that was a third of the video, and hits never pause
    longer than ~0.8 s). Rewrites the wav in place; returns its new length."""
    import soundfile as sf
    a, b = voiced_span(path)
    try:
        data, sr = sf.read(str(path), dtype="float32")
        if b > a:
            lo = max(0, int((a - pad) * sr))
            hi = min(len(data), int((b + pad) * sr))
            data = data[lo:hi]
            sf.write(str(path), data, sr)
    except (OSError, RuntimeError) as e:
        raise BlockyError(f"could not trim narration line {path}", detail=str(e)) from e
    return round(len(data) / sr, 3)


def align_words(script: list[dict], heard: list[dict]) -> list[dict]:
    """Pure. Caption words come from OUR script (numbers stay words, no
    hallucinated 'Thanks' in a silence); Whisper only says when each was
    spoken. `script` = [{"word", "start", "end"}] with rough times (spread per
    line); `heard` = Whisper words. Matched words take Whisper's times; the
    rest keep their rough times, clamped between their matched neighbours."""
    import difflib
    a = [_norm(w["word"]) for w in script]
    b = [_norm(w["word"]) for w in heard]
    out = [dict(w) for w in script]
    fixed = [False] * len(out)
    for blk in difflib.SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks():
        for k in range(blk.size):
            i, j = blk.a + k, blk.b + k
            out[i]["start"], out[i]["end"] = heard[j]["start"], heard[j]["end"]
            fixed[i] = True
    lo = 0.0
    for i, w in enumerate(out):
        if fixed[i]:
            lo = w["end"]
            continue
        nxt = next((out[k]["start"] for k in range(i + 1, len(out)) if fixed[k]), None)
        start = max(w["start"], lo)
        if nxt is not None:
            start = min(start, nxt - 0.05)
        w["start"] = round(max(lo, start), 3)
        w["end"] = round(max(w["start"] + 0.05, min(w["end"], nxt if nxt else w["end"])), 3)
        lo = w["end"]
    return out


def estimate_words(timeline: dict, line_wavs: list[str]) -> list[dict]:
    """Word timings for the whole narration without Whisper: each line's
    words spread across the voiced part of its own audio."""
    out: list[dict] = []
    for ln, wav in zip(timeline["lines"], line_wavs):
        a, b = voiced_span(wav)
        if b <= a:
            a, b = 0.0, ln["end"] - ln["start"]
        out += spread_words(ln["text"], ln["start"] + a, ln["start"] + b)
    return out


def words_in(words: list[dict], start: float, end: float) -> list[dict]:
    """Pure: Whisper words (absolute times) that start inside [start, end)."""
    return [w for w in words if start - 0.05 <= w["start"] < end]


def emphasis_times(shot: dict, words: list[dict], start: float, end: float) -> list[float]:
    """Pure: absolute start times of this shot's emphasis words."""
    want = {_norm(e) for e in shot["emphasis"]}
    return [w["start"] for w in words_in(words, start, end) if _norm(w["word"]) in want]


def mark_emphasis(story: dict, bounds: list[float], words: list[dict]) -> list[dict]:
    """Pure: copy of `words` with emph=True on each shot's emphasis words
    (captions.py colours them)."""
    out = [dict(w) for w in words]
    for i, shot in enumerate(story["shots"]):
        want = {_norm(e) for e in shot["emphasis"]}
        for w in out:
            if bounds[i] - 0.05 <= w["start"] < bounds[i + 1] and _norm(w["word"]) in want:
                w["emph"] = True
    return out


_ARMED = ("person", "house", "phone", "fridge", "tree")


def losable_parts(member: dict) -> int:
    """Pure. How many parts blocky.js can pop off this cast member (its
    PART_ORDER: armB, armA, hat, legB - whichever the shape has)."""
    shape = member["shape"]
    arms = 2 if shape in _ARMED else 0
    hat = 1 if shape == "person" and member["hair"] != "none" else 0
    leg = 0 if shape == "car" else 1
    return arms + hat + leg


def build_spec(story: dict, bounds: list[float], words: list[dict], w: int, h: int,
               fps: int, seed: int, quality: float) -> dict:
    """Pure. The renderer's input: cast + one entry per shot with its time span,
    a small seeded camera angle, punch-in times and, for lose_part, the moment
    the part pops (first emphasis word, else 40% into the shot)."""
    if len(bounds) != len(story["shots"]) + 1:
        raise BlockyError(f"{len(story['shots'])} shots but {len(bounds)} time bounds")
    rng = random.Random(seed)
    left = {k: losable_parts(c) for k, c in enumerate(story["cast"])}
    shots = []
    for i, s in enumerate(story["shots"]):
        start, end = float(bounds[i]), float(bounds[i + 1])
        dur = end - start
        if dur <= 0:
            raise BlockyError(f"shot {i} has no duration")
        emph = emphasis_times(s, words, start, end)
        pop_t = round((emph[0] - start) if emph else 0.4 * dur, 3)
        actors = []
        for a in s["actors"]:
            pops = a["action"] == "lose_part" and left[a["cast"]] > 0
            left[a["cast"]] -= pops
            # no lose_t when nothing is left to pop: no flying part, no pop sound
            actors.append({**a, **({"lose_t": pop_t} if pops else {})})
        punch = [round(t - start, 3) for t in emph[:1]] if s["sfx"] == "pop" else []
        shots.append({"start": round(start, 3), "end": round(end, 3),
                      "place": s["place"], "camera": s["camera"], "focus": s["focus"],
                      "actors": actors, "sign": s["sign"], "punch": punch,
                      "yaw": round(rng.uniform(-0.32, 0.32), 3)})
    return {"w": int(w), "h": int(h), "fps": int(fps), "seed": int(seed),
            "jpeg_quality": float(quality), "total": float(bounds[-1]),
            "cast": story["cast"], "shots": shots}


def sfx_cues(story: dict, spec: dict, words: list[dict], cap: int) -> list[dict]:
    """Pure. Sound effects in clip time: whoosh on its cut, a riser that peaks
    on the cut, pop/ding on the first emphasis word (else just after the cut),
    and a pop on every part that pops off. At most `cap`, earliest first."""
    cues = []
    for s, sp in zip(story["shots"], spec["shots"]):
        start, end = sp["start"], sp["end"]
        kind = s["sfx"]
        if kind == "whoosh":
            cues.append({"t": max(0.0, start - 0.12), "kind": "whoosh"})
        elif kind == "riser":
            cues.append({"t": max(0.0, start - 0.75), "kind": "riser"})
        elif kind in ("pop", "ding"):
            emph = emphasis_times(s, words, start, end)
            cues.append({"t": emph[0] if emph else start + 0.1, "kind": kind})
        for a in sp["actors"]:
            if "lose_t" in a:
                cues.append({"t": start + a["lose_t"], "kind": "pop"})
    cues.sort(key=lambda c: c["t"])
    return [{"t": round(c["t"], 3), "kind": c["kind"]} for c in cues[:max(0, cap)]]


# ------------------------------------------------------------------ render

def _serve(route) -> None:
    """Playwright route handler: the page's files come from assets/blocky
    (fonts/ from assets/fonts). Anything outside those folders is a 404."""
    path = urllib.parse.urlparse(route.request.url).path.lstrip("/")
    base, rel = (FONT_DIR, path[len("fonts/"):]) if path.startswith("fonts/") \
        else (ASSET_DIR, path)
    target = (base / rel).resolve()
    if not target.is_file() or not target.is_relative_to(base.resolve()):
        route.fulfill(status=404, body="not found")
        return
    route.fulfill(path=str(target))


def _launch(pw, channels: list) -> object:
    """Headless browser from the first installed channel (Chrome, then Edge,
    which every Windows PC has). A GPU is used when the browser offers one."""
    errors = []
    for ch in channels:
        try:
            return pw.chromium.launch(channel=ch or None, headless=True,
                                      args=["--ignore-gpu-blocklist",
                                            "--enable-gpu-rasterization"])
        except Exception as e:  # noqa: BLE001 - missing channel: try the next one
            errors.append(f"{ch}: {str(e).splitlines()[0]}")
    raise BlockyError("no browser could be started to draw the 3D frames",
                      detail="; ".join(errors) + " (install Chrome or Edge)",
                      retryable=False)


def _decode(data_url: str, w: int, h: int):
    import cv2
    import numpy as np
    if not isinstance(data_url, str) or "," not in data_url:
        raise BlockyError("the renderer returned no image")
    buf = np.frombuffer(base64.b64decode(data_url.split(",", 1)[1]), dtype=np.uint8)
    img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if img is None or img.shape[:2] != (h, w):
        raise BlockyError("the renderer returned a frame of the wrong size")
    return np.ascontiguousarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))


def render_track(spec: dict, out_path: Path, cfg: dict,
                 frames: list[int] | None = None, still_dir: Path | None = None) -> Path:
    """Draw every frame of `spec` and encode a silent video to `out_path`.
    With `frames` + `still_dir`, only those frames are saved as PNGs (a quick
    look at the picture without a full render) and no video is written."""
    try:
        from playwright.sync_api import Error as PWError
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise BlockyError("playwright is not installed",
                          detail="pip install playwright", retryable=False) from e
    b = _bcfg(cfg)
    w, h, fps = spec["w"], spec["h"], spec["fps"]
    n = max(1, int(round(spec["total"] * fps)))
    page_errors: list[str] = []
    timeout_ms = int(float(b.get("frame_timeout_s", 60)) * 1000)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = _launch(pw, list(b.get("browser_channels") or ["chrome", "msedge"]))
        try:
            page = browser.new_page(viewport={"width": 360, "height": 640})
            page.set_default_timeout(timeout_ms)
            page.on("pageerror", lambda e: page_errors.append(str(e)))
            page.on("console", lambda m: page_errors.append(m.text)
                    if m.type == "error" else None)
            page.route(f"{ORIGIN}/**", _serve)
            page.goto(f"{ORIGIN}/scene.html", wait_until="load")
            page.wait_for_function("window.BLOCKY_READY === true")
            info = page.evaluate("s => BLOCKY.load(s)", spec)
            for warn in info.get("warnings", []):
                log.warning("renderer: %s", warn)
            renderer = page.evaluate(
                "() => { const g = document.querySelector('canvas').getContext('webgl2');"
                " const d = g && g.getExtension('WEBGL_debug_renderer_info');"
                " return d ? g.getParameter(d.UNMASKED_RENDERER_WEBGL) : 'unknown'; }")
            log.info("3D renderer: %s, %dx%d, %d frames", renderer, w, h, n)
            if frames is not None:
                if still_dir is None:
                    raise BlockyError("render_track: frames given without still_dir")
                still_dir.mkdir(parents=True, exist_ok=True)
                import cv2
                for f in frames:
                    rgb = _decode(page.evaluate("t => BLOCKY.frame(t)", f / fps), w, h)
                    cv2.imwrite(str(still_dir / f"frame_{f:05d}.png"),
                                cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
                return still_dir
            t0 = time.perf_counter()
            with RawVideoWriter(out_path, w, h, fps, video_encode_args(cfg, final=True)) as wr:
                for f in range(n):
                    wr.write(_decode(page.evaluate("t => BLOCKY.frame(t)", f / fps), w, h))
                    if f and f % 300 == 0:
                        rate = f / (time.perf_counter() - t0)
                        log.info("frames %d/%d (%.1f fps, ~%.0fs left)", f, n, rate,
                                 (n - f) / max(rate, 0.01))
        except PWError as e:
            raise BlockyError("the 3D renderer failed",
                              detail=(str(e).splitlines()[0] + " | "
                                      + " | ".join(page_errors[-5:]))[:900]) from e
        finally:
            browser.close()
    if page_errors:
        log.warning("renderer page reported: %s", " | ".join(page_errors[-5:]))
    return out_path


# ------------------------------------------------------------------ one video

def _virality(cfg: dict) -> dict:
    v = {"score": float(_bcfg(cfg).get("virality_score", 60)), "verdict": "post",
         "reasons": ["Blocky mode: original animated story"]}
    validate(v, "virality")
    return v


def _music(b: dict, rng: random.Random, notes: list[str]) -> tuple[Path | None, str]:
    ids = [str(x) for x in (b.get("music_tracks") or [])]
    try:
        track = music.get_track(rng.choice(ids)) if ids else music.pick_track_by_mood("funny")
        return music.ensure_track(track), music.attribution_for(track)
    except music.MusicError as e:
        notes.append(f"no background music: {e.message}")
        log.warning("no background music: %s", e)
        return None, ""


def make_video(cfg: dict, provider: str | None = None, premise: str | None = None,
               story_file: str | Path | None = None,
               rng: random.Random | None = None) -> Path:
    """Make one blocky story Short. Returns the job directory; job.json always
    records the outcome (done / failed)."""
    from pipeline import new_job_dir
    import transcribe

    b = _bcfg(cfg)
    rng = rng or random.Random()
    resolved = "file" if story_file else llm.resolve_provider(cfg, provider)
    fps = int(b.get("fps", 30))
    w, h = int(b.get("width", 1080)), int(b.get("height", 1920))
    if not premise and not story_file:
        premise = pick_premise([str(p) for p in b.get("premises") or []], load_used(), rng)
    create._benched.clear()
    job_dir = new_job_dir(cfg, f"blocky-{premise or Path(str(story_file or 'auto')).stem}")
    fh = add_file_handler(job_dir / "blocky.log")
    timings: dict = {}
    notes: list[str] = []
    job = {"job_id": uuid.uuid4().hex[:12],
           "created": dt.datetime.now().isoformat(timespec="seconds"),
           "source": f"blocky:{premise or story_file or 'auto'}", "status": "running",
           "settings": {"mode": "blocky", "provider": resolved,
                        "voice": b.get("voice", {}).get("voice", "")},
           "stages": {}, "clips": [], "notes": notes}
    try:
        with stage_timer(log, "script", timings):
            raw = (load_story_file(story_file) if story_file
                   else write_story(premise, load_used(), cfg, resolved))
            story = clean_story(raw)
        premise = story["premise"]
        job["source"] = f"blocky:{premise}"
        if story["fixes"]:
            notes.append(f"{len(story['fixes'])} story value(s) swapped for ones the "
                         f"renderer draws: {'; '.join(story['fixes'][:6])}")
            log.warning("%s", notes[-1])
        (job_dir / "story.json").write_text(json.dumps(story, indent=2), encoding="utf-8")
        texts = [s["text"] for s in story["shots"]]
        n_words = sum(len(t.split()) for t in texts)
        if not int(b.get("words_min", 180)) <= n_words <= int(b.get("words_max", 280)) + 20:
            notes.append(f"narration is {n_words} words (target {b.get('words_min', 180)}"
                         f"-{b.get('words_max', 280)})")

        with stage_timer(log, "voice", timings):
            vcfg = {**cfg, "create": {**cfg["create"], "voice": b["voice"]}}
            synth = voice.synthesize_lines(texts, job_dir / "voice_lines", vcfg)
            for r in synth:
                r["duration_s"] = trim_line(r["path"])
            sections = [{"key": "story", "title": story["title"],
                         "lines": [{"text": s["text"], "visual": s["place"]}
                                   for s in story["shots"]]}]
            timeline = create.build_timeline(sections, [r["duration_s"] for r in synth],
                                             float(b.get("line_gap_s", 0.12)), 0.0,
                                             tail=float(b.get("tail_s", 1.2)))
            voice_wav = create.assemble_voice([r["path"] for r in synth], timeline,
                                              job_dir / "voice.wav")
            (job_dir / "timeline.json").write_text(json.dumps(timeline, indent=2),
                                                   encoding="utf-8")

        with stage_timer(log, "transcribe", timings):
            wcfg = {**cfg, "whisper": {**cfg["whisper"], "language": "en",
                                       "model_override": str(b.get("whisper_model",
                                                                   "small"))}}
            rough = estimate_words(timeline, [r["path"] for r in synth])
            try:
                heard = transcribe.transcribe(voice_wav, wcfg)["words"]
                words = align_words(rough, heard)
            except ClipForgeError as e:
                # Whisper only adds precise timing to text we already have (and
                # Windows Smart App Control has blocked its DLLs once), so a
                # failure costs caption precision, not the video.
                notes.append(f"Whisper failed ({e.message}); caption timing was "
                             "estimated from the narration audio")
                log.warning("%s", notes[-1])
                words = rough

        lines = timeline["lines"]
        bounds = [0.0] + [ln["start"] for ln in lines[1:]] + [timeline["total"]]
        seed = rng.randrange(1_000_000)
        spec = build_spec(story, bounds, words, w, h, fps, seed,
                          float(b.get("jpeg_quality", 0.93)))
        (job_dir / "render_spec.json").write_text(json.dumps(spec, indent=2),
                                                  encoding="utf-8")
        with stage_timer(log, "render", timings):
            track = render_track(spec, job_dir / "work" / "track.mp4", cfg)

        with stage_timer(log, "finish", timings):
            music_path, music_attr = _music(b, rng, notes)
            cues = sfx_cues(story, spec, words, int(b.get("sfx_max", 14)))
            fcfg = {**cfg, "sfx": {**cfg.get("sfx", {}), "enabled": True,
                                   "max_per_clip": max(1, len(cues)),
                                   "volume_db": float(b.get("sfx_volume_db", -8))}}
            clip_dir = job_dir / "clip_00"
            final = create.finish_video(
                track, voice_wav, mark_emphasis(story, bounds, words), clip_dir, fcfg,
                captions=True, preset=str(b.get("caption_preset", "word-pop")),
                anchor=float(b.get("caption_anchor", 0.62)), music_path=music_path,
                music_db=float(b.get("music_volume_db", -20)), sfx_cues=cues)
            credits = ("Animation: original, made with ClipForge. Narration: AI voice."
                       + (f" {music_attr}" if music_attr else ""))
            meta = {"title": story["title"][:60],
                    "description": f"{story['description']}\n\n{credits}",
                    "hashtags": story["hashtags"], "format": "short",
                    "made_for_kids": bool(b.get("made_for_kids", True)),
                    "blocky": {"premise": premise}}
            niche = create._niche(" ".join(texts), meta, cfg,
                                  "mock" if resolved in ("mock", "file") else resolved)
            job["clips"].append(create._write_clip(clip_dir, 0, final, meta,
                                                   _virality(cfg), niche, "short",
                                                   mock=resolved == "mock"))
        if resolved != "mock":
            mark_used(premise)
        shutil.rmtree(job_dir / "work", ignore_errors=True)
        job["status"] = "done"
    except Exception as e:  # noqa: BLE001 - the job record must capture any failure
        job["status"] = "failed"
        notes.append(f"job failed: {e}")
        log.error("blocky job failed: %s", e)
    finally:
        job["stages"] = timings
        validate(job, "job_record")
        (job_dir / "job.json").write_text(json.dumps(job, indent=2), encoding="utf-8")
        try:
            from history import record_job
            record_job(job, job_dir, cfg)
        except Exception as e:  # noqa: BLE001 - history is best-effort
            log.warning("history record failed: %s", e)
        remove_file_handler(fh)
    log.info("blocky job %s: %s -> %s", job["job_id"], job["status"], job_dir)
    return job_dir


# ------------------------------------------------------------------ CLI

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ClipForge Blocky mode: toy-brick 3D "
                                             "story Shorts")
    ap.add_argument("--premise", help="the story's rule, e.g. 'What if your house "
                                      "followed you to school?'")
    ap.add_argument("--story-file", help="render a story JSON written elsewhere "
                                         "(schema: schemas.BLOCKY_STORY)")
    ap.add_argument("--count", type=int, default=1, help="videos to make (default 1)")
    ap.add_argument("--provider", help="LLM provider override (mock|gemini|groq|ollama)")
    ap.add_argument("--workspace", help="library + YouTube channel (default blocky.workspace)")
    args = ap.parse_args(argv)
    if args.count < 1:
        ap.error("--count must be at least 1")
    if args.story_file and args.count != 1:
        ap.error("--story-file makes exactly one video")

    import config as config_mod
    import workspaces
    b = load_config().get("blocky") or {}
    ws = args.workspace or str(b.get("workspace", "default"))
    workspaces.ensure_workspace(ws, str(b.get("workspace_name", ws.title())))
    config_mod.current_workspace.set(ws)   # paths.output_dir -> this workspace's library
    cfg = load_config()
    verify_ffmpeg(cfg)
    failed = 0
    for n in range(args.count):
        job_dir = make_video(cfg, provider=args.provider,
                             premise=args.premise if n == 0 else None,
                             story_file=args.story_file)
        status = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))["status"]
        print(f"[{n + 1}/{args.count}] {status}: {job_dir}")
        failed += status != "done"
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
