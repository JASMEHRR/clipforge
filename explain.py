"""Explainer mode: PPT-style money explainers that play themselves in sync with
the narration (a second channel next to Create mode's kids videos).

  topic → slide script (LLM, `explainer_script`: slides with a layout, a
  heading, narration lines and up to 4 items, each with a `cue` word)
  → per-slide fact-check (create.ask / create.apply_fact_check)
  → Kokoro narration (voice.py) → timeline + voice.wav
  → flat AI pictures of the items' objects (art.py, square, cached per object
    name with a fixed seed so a "car" looks the same in every video)
  → Whisper word timings → cue_times: each item appears as its cue is spoken
  → slides.render_track → create.finish_video (music, loudnorm)

Output: output/<ts>_explain-<slug>/clip_00 (format "long", not made for
kids). Clips wait in approvals like every Create clip (create._write_clip).

CLI:
  .venv/Scripts/python.exe explain.py --topic "buying a car: credit card vs lease vs loan" --slides 7
  .venv/Scripts/python.exe explain.py --provider mock
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
import uuid
from pathlib import Path

import numpy as np

import art
import create
import slides as slide_render
import voice
from config import load_config
from errors import ClipForgeError, LLMError
from ffutil import verify_ffmpeg
from logutil import add_file_handler, get_logger, remove_file_handler, stage_timer
from schemas import validate

log = get_logger("explain")


class ExplainError(ClipForgeError):
    stage = "explain"


def _ecfg(cfg: dict) -> dict:
    e = cfg.get("explain")
    if not isinstance(e, dict):
        raise ExplainError("config is missing the explain: block", retryable=False)
    return e


# ------------------------------------------------------------------ script

def script_prompt(topic: str, slides: int) -> str:
    return (
        f"Write a narrated YouTube explainer video for adults in the United States about: "
        f"{topic}.\n\n"
        "The video is a slideshow that plays itself: each slide stays on screen while its "
        "lines are spoken, and every item on the slide pops up at the exact moment the "
        "narrator says its cue.\n\n"
        "TONE - this is the whole point: a confident, upbeat narrator who talks straight "
        "to the viewer like a sharp friend who is good with money. The message running "
        "through the video: money is easy to make and easy to keep when you are smart "
        "about it, and easy to lose when you are careless. The content is serious and "
        "useful - real numbers, real trade-offs - but it must be FUN to listen to, never "
        "a textbook: open with a hook that names what is at stake ('Two people buy the "
        "same car. One pays five thousand dollars more. Here is why.'), ask the viewer "
        "quick questions, use vivid everyday comparisons ('that is a vacation, gone'), "
        "drop 'here is the trick' moments, and end slides on a small cliffhanger that "
        "pulls into the next one. Plain everyday words, energy and certainty, short "
        "sentences. The fun comes from the situation and the stakes, not from jokes. NO "
        "slang or nicknames for money things anywhere, on screen or spoken (never "
        "'plastic', 'the vig', 'skinned', 'wheels'), no gangster talk, no swearing, no "
        "movie references.\n"
        "Make it concrete with a small cast of 2 ordinary example people with plain "
        "first names (for example Maya, who makes the smart move, and Jake, who doesn't): "
        "follow what each one does and what it costs them. Be fair to Jake - explain "
        "his choice, never mock him. Jargon always gets a one-line plain explanation.\n\n"
        "cast: those 2 people, each with name and look - a short visual description for "
        "a cartoon figure (adult, hair, clothes and colours), clearly different from each "
        "other; no real people.\n\n"
        f"Return exactly {slides} slides. Each slide has:\n"
        "- layout: one of title (the opening hook: a big heading and 1-3 objects), "
        "compare (2-4 options side by side), list (2-4 points), big_number (one striking "
        "number that counts up), flow (2-4 steps joined by arrows), bars (2-4 values as a "
        "bar chart; every bar the SAME kind of amount, e.g. the monthly cost of each "
        "option, never a per-mile fee next to a total). The first slide is title, the "
        "last is a list recap. Use a variety.\n"
        "- kicker: a 1-3 word label shown above the heading (for example 'THE OPTIONS', "
        "'THE CATCH', 'THE MATH', 'THE SMART MOVE').\n"
        "- heading: at most 6 words, clear and confident.\n"
        "- highlight: 1-3 words copied exactly from the heading, the key idea, shown "
        "in gold.\n"
        "- sticker: a red stamp of 1-2 words (for example 'SMART MOVE', 'COSTLY', "
        "'PAID OFF', 'WATCH OUT') on about every third slide; otherwise \"\".\n"
        "- actor: the cast member this slide is about (name exactly as in cast), or \"\" "
        "for a general slide; mood: how they feel on this slide (happy, confident, "
        "worried, shocked, sad, thinking; use thinking when actor is \"\").\n"
        "- lines: 1-4 narration lines of 8-20 words, spoken in order.\n"
        "- items: what appears on screen (big_number: exactly 1; others: 1-4). Each item:\n"
        "  label: 1-4 words shown big; detail: a short supporting phrase (may be empty);\n"
        "  object: only when the item IS a big concrete physical thing that makes a "
        "good illustration (car, house, bank building, piggy bank, truck, briefcase of "
        "cash); otherwise \"\" (a credit card or a loan is not a building). Never a "
        "person;\n"
        "  icon: one common emoji for the item (for example 💳 📄 🪙 📅 🔑 💰 📈 🏦), "
        "used when object is empty; \"\" if none fits;\n"
        "  value + unit: the number for big_number and bars (unit \"$\" or \"%\"), "
        "otherwise 0 and \"none\";\n"
        "  cue: a word or short phrase copied EXACTLY from this slide's lines; the "
        "item appears when it is spoken. Cues must follow the order the items are "
        "listed, and the FIRST item's cue must come from the slide's first line so "
        "the screen is never empty.\n"
        "- facts: every factual claim the slide makes.\n\n"
        "Numbers: ALWAYS write them as digits, everywhere - in the lines, headings, "
        "labels and cues ('$21,000', '24%', '7%', '5 years'), never spelled out "
        "('twenty-one thousand dollars'). A heading with a number shows the digits "
        "('$21,000 IN INTEREST').\n\n"
        "Accuracy: only well-established, widely documented facts. Numbers are "
        "illustrative examples ('for example', 'around', 'can'), round and realistic; "
        "never present today's rates, averages or prices as facts (no 'average rate "
        "today'; say 'say the loan is at 7 percent'). No brand names. This is "
        "education, not financial advice: explain trade-offs, never tell the viewer what "
        "to buy.\n\n"
        "title: at most 60 characters, bold and curiosity-driven but honest. description: 2-3 "
        "sentences. hashtags: 8-12 tags like #PersonalFinance (letters and digits only).\n"
        "Return only JSON matching the schema."
    )


def fact_check_prompt(topic: str, slide: dict) -> str:
    lines = "\n".join(f"{i + 1}. {ln['text']}" for i, ln in enumerate(slide["lines"]))
    facts = "\n".join(f"- {f}" for f in slide.get("facts", []))
    return (
        f"You are a careful fact-checker for a personal-finance explainer video about "
        f"{topic} for a US audience. Check this slide's narration.\n\n"
        f"Slide heading: {slide['title']}\nLines:\n{lines}\nClaimed facts:\n{facts}\n\n"
        "Rules:\n"
        '- verdict "pass": accurate (simplifying is fine; clearly illustrative example '
        "numbers are fine). Invented example people and confident, upbeat phrasing are "
        "fine - "
        "judge only the money facts behind them.\n"
        '- verdict "fix": something is wrong or misleading but fixable. Return '
        f"corrected_lines with exactly {len(slide['lines'])} strings, same order, "
        "changing only the words that are wrong - keep the original wording, hook, "
        "energy and every other sentence exactly as written.\n"
        '- verdict "reject": the slide\'s main point is false or reads as personal '
        "financial advice.\n"
        "- A number presented as today's rate, price or national average is a problem: "
        'use "fix" to reword it as an example ("say the loan is at 7 percent").\n'
        "List every problem in issues (empty list if none).\n"
        "Return only JSON matching the schema."
    )


def writer_cfg(cfg: dict) -> dict:
    """Config for the copy-writing calls: free-tier flash models write far better
    than flash-lite but can take minutes when overloaded - this channel waits
    for them (it runs at night). Fact-checks keep create.gemini_models."""
    e = _ecfg(cfg)
    return {**cfg, "create": {**cfg["create"],
                              "gemini_models": e.get("writer_models")
                              or cfg["create"].get("gemini_models"),
                              "llm_timeout_s": e.get("writer_timeout_s",
                                                     cfg["create"].get("llm_timeout_s", 120))}}


def write_shorts(script: dict, n: int, cfg: dict, provider: str | None) -> list[dict]:
    """Short teasers for a checked script; [] when the writer fails (the long
    video still ships)."""
    try:
        data = create.ask("explainer_shorts", shorts_prompt(script, n), writer_cfg(cfg),
                          provider, {"topic": script.get("topic", ""), "shorts": n})
    except LLMError as err:
        log.warning("shorts script failed: %s", err)
        return []
    return data["shorts"][:n]


def social_captions(reel_title: str, script: dict, youtube_url: str = "") -> dict:
    """Pure. Ready-to-paste text for posting one reel on each platform (the
    video file is the same everywhere). Instagram: up to 30 hashtags, links
    are not clickable; Pinterest: title (100), description (500) and a link."""
    tags = [t for t in script.get("hashtags", []) if t.startswith("#")]
    where = (f"Full video on YouTube: {youtube_url}" if youtube_url
             else "Full video on our YouTube channel (link in bio).")
    desc = script.get("description", "").strip()
    return {
        "youtube_shorts": {"title": reel_title[:90],
                           "description": (f"{desc}\n\nFull video: {script['title']}\n"
                                           + " ".join(tags[:8] + ["#shorts"]))},
        "instagram": {"caption": (f"{reel_title}\n\n{desc}\n\n{where}\n\n"
                                  + " ".join(tags[:30]))},
        "pinterest": {"title": reel_title[:100], "description": f"{desc} {where}"[:500],
                      "link": youtube_url},
    }


def shorts_prompt(script: dict, n: int) -> str:
    """Vertical reels (one file for Shorts / Instagram / Pinterest) built ONLY
    from an already fact-checked script."""
    slides = "\n".join(f"- [{s['layout']}] {s['heading']}: " + " ".join(s["lines"])
                       for s in script["slides"])
    cast = ", ".join(f"{c['name']} ({c['look']})" for c in script.get("cast", []))
    return (
        f"Below is the narration of a finished, fact-checked YouTube video titled "
        f"\"{script['title']}\". Write {n} vertical reels (45-60 seconds each). The SAME "
        "reel is posted on YouTube Shorts, Instagram Reels and Pinterest, so never say "
        "'Short' or name the app the viewer is in. Each reel is a real mini-lesson on a "
        "DIFFERENT angle of the video (the most surprising number, a costly mistake, a "
        "smart trick): someone who only sees the reel must still learn something "
        "complete and useful.\n"
        "- slide 1 is the hook: a surprising number or a sharp question in the first "
        "line, nothing generic;\n"
        "- the middle slides explain it properly: what happens, why, and what it costs, "
        "with 3-4 concrete facts and real numbers from the video;\n"
        "- the LAST slide's last line is: 'For the complete breakdown, watch the full "
        "video on our YouTube channel.' and its sticker is 'FULL VIDEO ON YOUTUBE'.\n"
        "5-6 slides per reel, 1-2 lines per slide, 8-16 words per line. Same tone as the "
        "video: confident, upbeat, plain words, no slang. Use ONLY facts and numbers that "
        "appear below - never add new ones. Phone screen: keep labels to 1-3 words, at "
        "most 3 items per slide; compare and flow slides have 2-3 items. Slide 1 should "
        "be a big_number or bars slide whenever the hook is a number.\n"
        "Numbers: ALWAYS digits, in lines, headings, labels and cues ('$21,000', '24%', "
        "'5 years'), never spelled out. The first item's cue comes from the slide's "
        "first line.\n"
        f"Cast you may use as actor: {cast or 'none'}.\n"
        "Slide fields are the same as the video's: layout, kicker, heading (max 5 words), "
        "highlight (words copied from the heading), sticker, actor, mood, lines, items "
        "(label, detail, object, icon, value, unit, cue copied exactly from that slide's "
        "lines), facts.\n\n"
        f"Video:\n{slides}\n\nReturn only JSON matching the schema."
    )


def checked_script(topic: str, n_slides: int, cfg: dict, provider: str | None) -> dict:
    """Script whose slides passed (or were fixed by) the fact-check. Rejected
    slides are dropped; more than a quarter rejected fails the video, since
    the remaining slides would no longer tell a coherent story."""
    script = create.ask("explainer_script", script_prompt(topic, n_slides), writer_cfg(cfg),
                        provider, {"topic": topic, "slides": n_slides})
    script["topic"] = topic
    kept, report = [], []
    for s in script["slides"]:
        ch = {"title": s["heading"], "lines": [{"text": t} for t in s["lines"]],
              "facts": s.get("facts", [])}
        try:
            verdict = create.ask("fact_check", fact_check_prompt(topic, ch), cfg, provider,
                                 {"topic": topic})
        except LLMError as e:
            verdict = {"verdict": "reject", "issues": [f"fact-check failed: {e}"]}
        fixed = create.apply_fact_check(ch, verdict)
        report.append({"slide": s["heading"], "verdict": verdict["verdict"],
                       "issues": verdict.get("issues", []), "kept": fixed is not None})
        if fixed is not None:
            kept.append({**s, "lines": [ln["text"] for ln in fixed["lines"]]})
    log.info("fact-check: %d/%d slides kept", len(kept), len(script["slides"]))
    if len(kept) < max(3, round(0.75 * len(script["slides"]))):
        raise ExplainError(f"only {len(kept)}/{len(script['slides'])} slides passed the "
                           "fact-check", detail=json.dumps(report)[:1500])
    script = {**script, "slides": kept}
    validate(script, "explainer_script")
    script["fact_check"] = report
    return script


# ------------------------------------------------------------------ cue sync

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def _same(a: str, b: str) -> bool:
    return a == b or (len(a) > 3 and len(b) > 3 and a.rstrip("s") == b.rstrip("s"))


def _find(toks: list[str], cue: list[str], pos: int) -> int | None:
    for i in range(pos, len(toks) - len(cue) + 1):
        if all(_same(toks[i + j], c) for j, c in enumerate(cue)):
            return i
    return None


def tighten_times(times: list[float], start: float, first_by: float,
                  max_gap: float) -> list[float]:
    """Pure. No dead air: the first item is on screen within `first_by` seconds
    of the slide start and no later item waits more than `max_gap` after the
    one before it. Items only ever move EARLIER, so increasing input (what
    cue_times returns) stays increasing with its spacing intact."""
    out: list[float] = []
    for k, t in enumerate(times):
        out.append(min(t, start + first_by if k == 0 else out[-1] + max_gap))
    return out


def join_number_pieces(words: list[dict]) -> list[dict]:
    """Pure. Whisper splits numbers ('$30' + ',000.', '24' + '%'); glue a
    piece that starts with ',', '.', '%' or a digit right after a number back
    onto it, so captions read '$30,000.' and cue matching sees one token."""
    out: list[dict] = []
    for w in words:
        txt = str(w["word"])
        prev = str(out[-1]["word"]).strip() if out else ""
        if out and not txt.startswith(" ") and txt[:1] in ",.%0123456789" \
                and prev[-1:].isdigit():
            out[-1] = {**out[-1], "word": out[-1]["word"] + txt, "end": w["end"]}
        else:
            out.append(dict(w))
    return out


def cue_times(words: list[dict], start: float, end: float, cues: list[str],
              lead: float = 0.12, min_gap: float = 0.3) -> list[float]:
    """Pure. When each item appears: the first time its cue is spoken inside
    [start, end), searched in order (a repeated word maps to the right item),
    a little before the word so the pop lands on it. A cue Whisper didn't
    match falls back to its multi-word cue's longest word, then to an even
    spread. Times are increasing (>= min_gap apart) and inside the slide."""
    span = [(float(w["start"]), _norm(str(w["word"]))) for w in words
            if start - 0.05 <= float(w["start"]) < end]
    toks = [tk for _, tk in span]
    out: list[float | None] = []
    pos = 0
    for cue in cues:
        ct = [c for c in (_norm(x) for x in cue.split()) if c]
        hit = _find(toks, ct, pos) if ct else None
        if hit is None and len(ct) > 1:
            hit = _find(toks, [max(ct, key=len)], pos)
        if hit is None:
            out.append(None)
            continue
        out.append(span[hit][0] - lead)
        pos = hit + 1
    n = len(cues)
    dur = max(0.0, end - start)
    times: list[float] = []
    for k, t in enumerate(out):
        if t is None:
            t = start + dur * 0.8 * (k + 1) / (n + 1)
        lo = start + 0.15 if not times else times[-1] + min_gap
        times.append(min(max(t, lo), max(lo, end - 0.4)))
    return times


# ------------------------------------------------------------------ pictures

def object_prompt(obj: str, style: str) -> str:
    return f"a {obj.strip().rstrip('.')}, {style}"


def object_seed(obj: str) -> int:
    """Stable per object name: the same 'car' in every video (and cached)."""
    return int(hashlib.sha256(obj.strip().lower().encode("utf-8")).hexdigest()[:8], 16)


_FLUENT_BASE = "https://raw.githubusercontent.com/microsoft/fluentui-emoji/main/assets/"


def fluent_png(name: str, cfg: dict, skin: str | None = None) -> str | None:
    """Microsoft Fluent Emoji 3D PNG (MIT) for a CLDR name such as 'Worried
    face'; people live under a skin-tone folder ('Default', 'Light',
    'Medium-Light', 'Medium', 'Medium-Dark', 'Dark'). Cached in
    cache/emoji/fluent; None when it doesn't exist or can't be fetched."""
    import urllib.error
    import urllib.parse
    import urllib.request
    from config import ROOT
    stem = name.lower().replace(" ", "_")
    d = ROOT / cfg["paths"]["cache_dir"] / "emoji" / "fluent"
    path = d / (f"{stem}_3d_{skin.lower()}.png" if skin else f"{stem}_3d.png")
    miss = path.with_suffix(".missing")
    if path.is_file():
        return str(path)
    if miss.exists():
        return None
    q = urllib.parse.quote
    url = _FLUENT_BASE + (f"{q(name)}/{q(skin)}/3D/{q(stem)}_3d_{q(skin.lower())}.png"
                          if skin else f"{q(name)}/3D/{q(stem)}_3d.png")
    d.mkdir(parents=True, exist_ok=True)
    part = path.with_suffix(".part")
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            part.write_bytes(resp.read())
        part.replace(path)
        return str(path)
    except urllib.error.HTTPError as err:
        if err.code == 404:
            miss.touch()                  # no such Fluent asset: callers fall back
    except OSError as err:
        log.warning("Fluent download failed for %r: %s", name, err)
    return None


def icon_png(token: str, cfg: dict) -> str | None:
    """A PNG for one emoji: Microsoft Fluent Emoji 3D (MIT) when its folder
    matches the emoji's Unicode name, else Noto Emoji (Apache-2.0, via
    animate.icon_sprite's cache). None when neither exists - text-only card."""
    import unicodedata
    import animate
    from config import ROOT
    emoji = animate.resolve_emoji(token or "")
    if not emoji:
        return None
    folder = unicodedata.name(emoji[0], "").capitalize()
    if folder and len(emoji.replace("\ufe0f", "")) == 1:
        found = fluent_png(folder, cfg)
        if found:
            return found
    if animate.icon_sprite(emoji, cfg) is not None:
        noto = ROOT / cfg["paths"]["cache_dir"] / "emoji" / animate.emoji_filename(emoji)
        if noto.is_file():
            return str(noto)
    return None


def draw_objects(names: list[str], cfg: dict) -> dict[str, str | None]:
    """Object name -> cut-out PNG (or the whole picture if no clean cut-out)."""
    e = _ecfg(cfg)
    uniq = sorted({n.strip().lower() for n in names if n.strip()})
    if not uniq:
        return {}
    style = str(e.get("object_style", ""))
    size = int(e.get("object_px", 512))
    pics = art.draw_images([(object_prompt(n, style), object_seed(n)) for n in uniq], cfg,
                           size=(size, size))
    out = {}
    for n, pic in zip(uniq, pics):
        lay = art.cutout(pic, cfg, max_area=0.9)
        out[n] = lay["fg"] or str(pic)
    return out


# PJ disliked the AI-drawn chibi figures (2026-11-10) and picked Microsoft's
# 3D emoji people: crisp, consistent, same style as the icons.
OUTFITS = ("office worker", "technologist", "student")
TONES = ("Medium-Light", "Medium-Dark", "Light", "Medium")
MOOD_FACES = {"happy": "Money-mouth face", "confident": "Smiling face with sunglasses",
              "worried": "Worried face", "shocked": "Face screaming in fear",
              "sad": "Crying face", "thinking": "Thinking face"}


def cast_people(cast: list[dict]) -> dict[str, tuple[str, str]]:
    """Pure. cast name -> (Fluent person, skin tone): gender read from the
    look; every member gets the next outfit AND the next skin tone, so two
    people in one video never look alike (at video size the same outfit and
    tone read as one person)."""
    out = {}
    for k, c in enumerate(cast):
        look = c.get("look", "").lower()
        g = ("Woman " if re.search(r"\b(woman|female|girl|lady)\b", look)
             else "Man " if re.search(r"\b(man|male|guy|boy)\b", look) else "")
        outfit = OUTFITS[k % len(OUTFITS)]
        out[c["name"].strip().lower()] = ((g + outfit).capitalize(), TONES[k % len(TONES)])
    return out


def compose_figure(person_png: str, face_png: str | None):
    """The 3D person with a cream reaction bubble holding the mood face."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (600, 660), (0, 0, 0, 0))
    person = Image.open(person_png).convert("RGBA").resize((440, 440), Image.LANCZOS)
    if face_png:
        d = ImageDraw.Draw(img)
        cx, cy, r = 450, 120, 104
        d.polygon([(cx - 70, cy + 60), (cx - 30, cy + 90), (cx - 120, cy + 150)],
                  fill=(243, 233, 210, 255))
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(243, 233, 210, 255))
        face = Image.open(face_png).convert("RGBA").resize((160, 160), Image.LANCZOS)
        img.alpha_composite(face, (cx - 80, cy - 80))
    img.alpha_composite(person, (10, 660 - 440))
    return img


def draw_cast(cast: list[dict], slides: list[dict], cfg: dict) -> dict[tuple, str]:
    """(name, mood) -> PNG of that cast member reacting, for every slide that
    shows one. Cached per (person, face) in cache/explain_cast."""
    from config import ROOT
    people = cast_people(cast)
    need = sorted({(s["actor"].strip().lower(), s["mood"]) for s in slides
                   if s.get("actor", "").strip().lower() in people})
    d = ROOT / cfg["paths"]["cache_dir"] / "explain_cast"
    out = {}
    for name, mood in need:
        (person, tone), face = people[name], MOOD_FACES.get(mood, MOOD_FACES["thinking"])
        stem = f"{person}_{tone}__{face}".lower().replace(" ", "_")
        path = d / f"{stem}.png"
        if not path.is_file():
            pp = fluent_png(person, cfg, skin=tone)
            if not pp:
                log.warning("no Fluent picture for %s - %s appears without a figure",
                            person, name)
                continue
            d.mkdir(parents=True, exist_ok=True)
            compose_figure(pp, fluent_png(face, cfg)).save(path)
        out[(name, mood)] = str(path)
    return out


# ------------------------------------------------------------------ sound effects

SFX_KINDS = ("slap", "whoosh", "stamp", "kaching")


def synth_sfx(kind: str, sr: int) -> np.ndarray:
    """Pure + deterministic. Sounds synthesised here (no audio files, no
    licences): 'slap' = a card tossed on the table, 'whoosh' = whip-pan swell,
    'stamp' = rubber-stamp thud, 'kaching' = cash-register bell."""
    rng = np.random.default_rng(7)
    if kind == "slap":
        t = np.arange(int(sr * 0.07)) / sr
        noise = rng.standard_normal(len(t))
        hi = noise - np.convolve(noise, np.ones(8) / 8, "same")      # papery top end
        body = np.sin(2 * np.pi * 180 * t) * np.exp(-t / 0.012)
        return 0.13 * (0.8 * hi * np.exp(-t / 0.010) + 0.5 * body)
    if kind == "whoosh":
        n = int(sr * 0.42)
        noise = rng.standard_normal(n + 64)
        band = (np.convolve(noise, np.ones(5) / 5, "same")
                - np.convolve(noise, np.ones(48) / 48, "same"))[:n]
        env = np.sin(np.pi * np.arange(n) / n) ** 2
        return 0.16 * band * env / max(1e-9, float(np.abs(band).max()))
    if kind == "stamp":
        t = np.arange(int(sr * 0.28)) / sr
        freq = 55 + 90 * np.exp(-t * 30)
        thump = np.sin(2 * np.pi * np.cumsum(freq) / sr) * np.exp(-t / 0.07)
        knock = rng.standard_normal(len(t)) * np.exp(-t / 0.008)
        return 0.22 * (thump + 0.35 * knock) / 1.35
    if kind == "kaching":
        t = np.arange(int(sr * 0.75)) / sr
        ka = rng.standard_normal(len(t)) * np.exp(-t / 0.006)          # the drawer latch
        late = np.clip(t - 0.06, 0, None)
        bell = (np.sin(2 * np.pi * 2093 * late) + 0.7 * np.sin(2 * np.pi * 2637 * late)
                + 0.3 * np.sin(2 * np.pi * 4186 * late)) * np.exp(-late / 0.22) * (t >= 0.06)
        return 0.12 * (0.6 * ka + bell) / 2.0
    raise ExplainError(f"unknown sound effect '{kind}'")


def sfx_cues(plan: list[dict], bounds: list[float]) -> list[tuple[float, str]]:
    """Pure. A whoosh on every slide change, a slap as each card lands, a
    ka-ching when a big number finishes counting, a thud for every stamp."""
    cues = []
    for i, s in enumerate(plan):
        if i > 0:
            cues.append((bounds[i] - 0.05, "whoosh"))
        cues += [(float(it["at"]) + 0.03, "slap") for it in s["items"]]
        mt = slide_render.money_time(s)
        if mt is not None:
            cues.append((mt, "kaching"))
        if s.get("sticker", "").strip():
            cues.append((slide_render.sticker_time(s["items"], bounds[i], bounds[i + 1])
                         + 0.12, "stamp"))
    return sorted(cues)


def mix_sfx(voice_wav: Path, cues: list[tuple[float, str]], out: Path,
            gain_db: float = 0.0) -> Path:
    import soundfile as sf
    data, sr = sf.read(str(voice_wav), dtype="float32", always_2d=False)
    mono = data.mean(axis=1) if data.ndim > 1 else data
    mix = mono.astype(np.float64).copy()
    g = 10 ** (gain_db / 20)
    sounds = {k: synth_sfx(k, sr) for k in SFX_KINDS}
    for t, kind in cues:
        a = int(max(0.0, t) * sr)
        snd = sounds[kind][: max(0, len(mix) - a)]
        mix[a:a + len(snd)] += g * snd
    peak = float(np.abs(mix).max())
    if peak > 0.98:
        mix *= 0.98 / peak
    out.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out), mix.astype(np.float32), sr)
    return out


# ------------------------------------------------------------------ topics

def _used_path(cfg: dict) -> Path:
    from config import ROOT
    return ROOT / cfg["paths"]["cache_dir"] / "explain_topics_used.json"


def load_used_topics(cfg: dict) -> list[str]:
    p = _used_path(cfg)
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    except (OSError, json.JSONDecodeError):
        return []
    return [str(t) for t in data] if isinstance(data, list) else []


def mark_topic_used(topic: str, cfg: dict) -> None:
    used = load_used_topics(cfg)
    if topic.lower() not in {t.lower() for t in used}:
        used.append(topic)
    p = _used_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(used, indent=2), encoding="utf-8")
    tmp.replace(p)


def next_topic(cfg: dict, rng) -> str:
    """Next unused explain.topics entry (the rotation restarts when all are used)."""
    return create.pick_topic(list(_ecfg(cfg).get("topics") or []), load_used_topics(cfg), rng)


def produce(slides: list[dict], cast: list[dict], work: Path, clip_dir: Path,
            size: tuple[int, int], cfg: dict, timings: dict, tag: str, *,
            captions: bool, preset: str, anchor: float | None, cta: str | None = None):
    """Slides -> finished clip: narration, pictures, word timings, cue sync,
    deck render (landscape or vertical by `size`), sound effects, mux.
    `cta` (Shorts) = the long video's title: the last slide becomes the
    'full video on the channel' end card. Returns (final path, plan, bounds,
    words)."""
    import transcribe
    e = _ecfg(cfg)
    w, h = size
    sections = [{"key": f"s{i}", "title": s["heading"],
                 "lines": [{"text": t, "visual": ""} for t in s["lines"]]}
                for i, s in enumerate(slides)]
    texts = [t for s in slides for t in s["lines"]]
    with stage_timer(log, f"voice_{tag}", timings):
        vcfg = {**cfg, "create": {**cfg["create"],
                                  "voice": {**cfg["create"]["voice"], **e.get("voice", {})}}}
        synth = voice.synthesize_lines(texts, work / "voice_lines", vcfg)
        timeline = create.build_timeline(sections, [r["duration_s"] for r in synth],
                                         float(e.get("line_gap_s", 0.2)),
                                         float(e.get("slide_gap_s", 0.6)))
        voice_wav = create.assemble_voice([r["path"] for r in synth], timeline,
                                          work / "voice.wav")
    with stage_timer(log, f"art_{tag}", timings):          # before Whisper: GPU memory
        pictures = draw_objects([it["object"] for s in slides for it in s["items"]], cfg)
        figures = draw_cast(cast, slides, cfg)
    with stage_timer(log, f"transcribe_{tag}", timings):
        wcfg = {**cfg, "whisper": {**cfg["whisper"], "language": "en",
                                   "model_override": str(e.get("whisper_model", "small"))}}
        words = join_number_pieces(transcribe.transcribe(voice_wav, wcfg)["words"])
    secs = timeline["sections"]
    bounds = [0.0] + [s["start"] for s in secs[1:]] + [timeline["total"]]
    plan = []
    for i, s in enumerate(slides):
        times = cue_times(words, bounds[i], bounds[i + 1], [it["cue"] for it in s["items"]])
        # a Short's first slide is the hook: its number is up almost at once
        first_by = float((e.get("shorts") or {}).get("hook_s", 0.35)) if (h > w and i == 0) \
            else float(e.get("first_item_s", 0.9))
        times = tighten_times(times, bounds[i], first_by, float(e.get("max_item_gap_s", 4.5)))
        plan.append({"layout": s["layout"], "heading": s["heading"],
                     "cta": cta if (cta and i == len(slides) - 1) else None,
                     "kicker": s["kicker"], "highlight": s["highlight"],
                     "sticker": s["sticker"], "start": bounds[i], "end": bounds[i + 1],
                     "figure": figures.get((s.get("actor", "").strip().lower(),
                                            s.get("mood", ""))),
                     "items": [{**it, "at": t,
                                "picture": (pictures.get(it["object"].strip().lower())
                                            or icon_png(it["icon"], cfg))}
                               for it, t in zip(s["items"], times)]})
    (work / "slides.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    with stage_timer(log, f"render_{tag}", timings):
        track = slide_render.render_track(plan, bounds, w, h, int(e.get("fps", 30)),
                                          work / "work" / "track.mp4", cfg)
        audio = voice_wav
        if e.get("sfx", True):
            audio = mix_sfx(voice_wav, sfx_cues(plan, bounds), work / "work" / "voice_sfx.wav",
                            float(e.get("sfx_db", 0.0)))
        final = create.finish_video(track, audio, words, clip_dir, cfg, captions=captions,
                                    preset=preset, anchor=anchor, music_path=None,
                                    music_db=-24)
    return final, plan, bounds, words


# ------------------------------------------------------------------ one video

def make_video(cfg: dict, topic: str, n_slides: int | None = None,
               provider: str | None = None) -> Path:
    """One explainer video. Returns the job dir; job.json always written."""
    from pipeline import new_job_dir
    import llm

    e = _ecfg(cfg)
    n_slides = int(n_slides or e.get("slides", 12))
    resolved = llm.resolve_provider(cfg, provider)
    w, h = int(e.get("width", 1920)), int(e.get("height", 1080))
    create._benched.clear()
    job_dir = new_job_dir(cfg, f"explain-{topic}")
    fh = add_file_handler(job_dir / "explain.log")
    timings: dict = {}
    notes: list[str] = []
    job = {"job_id": uuid.uuid4().hex[:12],
           "created": dt.datetime.now().isoformat(timespec="seconds"),
           "source": f"explain:{topic}", "status": "running",
           "settings": {"mode": "explain", "provider": resolved, "slides": n_slides},
           "stages": {}, "clips": [], "notes": notes}
    try:
        with stage_timer(log, "script", timings):
            script = checked_script(topic, n_slides, cfg, resolved)
        (job_dir / "script.json").write_text(json.dumps(script, indent=2), encoding="utf-8")
        credits = ("Pictures: AI-generated on our own computer; icons: Microsoft Fluent "
                   "Emoji (MIT) and Google Noto Emoji (Apache 2.0). Narration: AI voice. "
                   "For education only, not financial advice.")
        clip_dir = job_dir / "clip_00"
        final, _, _, words = produce(script["slides"], script.get("cast", []), job_dir,
                                     clip_dir, (w, h), cfg, timings, "long",
                                     captions=bool(e.get("captions", False)),
                                     preset=str(e.get("caption_preset", "clean-minimal")),
                                     anchor=None)
        meta = {"title": script["title"], "description":
                f"{script['description'].strip()}\n\n{credits}",
                "hashtags": script["hashtags"], "format": "long", "made_for_kids": False,
                "create": {"topic": topic, "mode": "explain",
                           "fact_check": script["fact_check"]}}
        job["clips"].append(create._write_clip(clip_dir, 0, final, meta, create._virality(cfg),
                                               "finance", "long", mock=resolved == "mock"))
        (job_dir / "words.json").write_text(json.dumps(words), encoding="utf-8")

        sc = e.get("shorts") or {}
        if sc.get("enabled", True):
            with stage_timer(log, "shorts_script", timings):
                teasers = write_shorts(script, int(sc.get("max", 3)), cfg, resolved)
            (job_dir / "shorts.json").write_text(json.dumps(teasers, indent=2),
                                                 encoding="utf-8")
            if not teasers:
                notes.append("no Shorts: the teaser script could not be written")
            for k, short in enumerate(teasers, 1):
                try:
                    sdir = job_dir / f"clip_{k:02d}"
                    final_s, _, _, _ = produce(
                        short["slides"], script.get("cast", []), job_dir / "work" / f"short_{k:02d}",
                        sdir, (int(sc.get("width", 1080)), int(sc.get("height", 1920))), cfg,
                        timings, f"short_{k}", captions=True,
                        preset=str(sc.get("caption_preset", "karaoke-pop")),
                        anchor=float(sc.get("caption_anchor", 0.64)), cta=script["title"])
                    smeta = {"title": short["title"][:60],
                             "description": (f"{short['title']}. Full video on the channel: "
                                             f"{script['title']}.\n\n{credits}"),
                             "hashtags": script["hashtags"], "format": "short",
                             "made_for_kids": False,
                             "create": {"topic": topic, "mode": "explain", "short": k}}
                    (sdir / "social.json").write_text(json.dumps(
                        social_captions(short["title"], script, str(e.get("youtube_url", ""))),
                        indent=2), encoding="utf-8")
                    job["clips"].append(create._write_clip(
                        sdir, k, final_s, smeta, create._virality(cfg), "finance", "short",
                        mock=resolved == "mock"))
                except Exception as err:  # noqa: BLE001 - one bad Short never sinks the rest
                    notes.append(f"short {k} failed: {err}")
                    log.error("short %d failed: %s", k, err)
        if resolved != "mock":            # a keyless test must not use up a real topic
            mark_topic_used(topic, cfg)
        job["status"] = "done"
    except Exception as err:  # noqa: BLE001 - the job record must capture any failure
        job["status"] = "failed"
        notes.append(f"job failed: {err}")
        log.error("explain job failed: %s", err)
    finally:
        job["stages"] = timings
        validate(job, "job_record")
        (job_dir / "job.json").write_text(json.dumps(job, indent=2), encoding="utf-8")
        try:
            from history import record_job
            record_job(job, job_dir, cfg)
        except Exception as err:  # noqa: BLE001 - history is best-effort
            log.warning("history record failed: %s", err)
        remove_file_handler(fh)
    log.info("explain job %s: %s -> %s", job["job_id"], job["status"], job_dir)
    return job_dir


def main(argv: list[str] | None = None) -> int:
    import random
    import config as config_mod
    import workspaces
    ap = argparse.ArgumentParser(description="ClipForge Explainer mode: PPT-style money "
                                             "explainers synced to narration")
    ap.add_argument("--topic", help="force a topic (otherwise the next unused one)")
    ap.add_argument("--count", type=int, default=1, help="videos to make (default 1)")
    ap.add_argument("--slides", type=int, help="slide count (default explain.slides)")
    ap.add_argument("--provider", help="LLM provider override (mock|gemini|...)")
    ap.add_argument("--workspace", help="library + YouTube channel (default explain.workspace)")
    ap.add_argument("--no-shorts", action="store_true", help="skip the vertical Shorts")
    args = ap.parse_args(argv)
    if args.count < 1:
        ap.error("--count must be at least 1")
    base = load_config()
    e = base.get("explain") or {}
    ws = args.workspace or str(e.get("workspace", "default"))
    workspaces.ensure_workspace(ws, str(e.get("workspace_name", ws.title())))
    config_mod.current_workspace.set(ws)
    cfg = load_config()                 # paths.output_dir now scoped to the workspace
    if args.no_shorts:
        cfg = {**cfg, "explain": {**cfg["explain"],
                                  "shorts": {**(cfg["explain"].get("shorts") or {}),
                                             "enabled": False}}}
    verify_ffmpeg(cfg)
    rng = random.Random()
    failed = 0
    for n in range(args.count):
        topic = args.topic if (args.topic and n == 0) else next_topic(cfg, rng)
        job_dir = make_video(cfg, topic, args.slides, args.provider)
        status = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))["status"]
        print(f"[{n + 1}/{args.count}] {status}: {job_dir}")
        failed += status != "done"
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
