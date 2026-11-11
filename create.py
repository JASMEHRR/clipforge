"""Create mode: original kids explainer videos from scratch.

One run = one topic → one long-form 16:9 video (intro, N chapters, outro)
plus every chapter rendered as its own vertical Short, all from the same
narration and footage:

  topic → script (LLM) → per-chapter fact-check (LLM; fix or drop chapters)
  → Kokoro narration per line (voice.py) → timeline + voice.wav
  → Whisper word timings (transcribe.py, for captions)
  → visuals, per create.visual_style:
      illustrated (default): a local AI cartoon picture per ~8 s beat (art.py,
        drawn BEFORE Whisper so the two never share the 4 GB GPU), cut into a
        character + background and animated by animate.py with camera moves,
        parallax, cross-fades, the owl host and banners
      animated: cartoon track drawn by animate.py - owl mascot with
        lip-flap, emoji illustrations, animated scene backgrounds, banners
      footage: stock shots per line (footage.py: Pexels → Pixabay → NASA →
        colour card), concatenated
  → mux narration → captions → music + loudnorm

Output uses the normal job layout so the Library, approvals and upload
center pick it up unchanged:
  output/<ts>_create-<slug>/clip_00/  long video   (metadata format "long")
  output/<ts>_create-<slug>/clip_01+/ chapter Shorts (format "short")
Every clip carries made_for_kids and goes through upload.require_approval
like any other clip - nothing is published unreviewed.

CLI:
  .venv/Scripts/python.exe create.py                      # next unused topic
  .venv/Scripts/python.exe create.py --topic "octopuses"
  .venv/Scripts/python.exe create.py --count 2 --provider mock
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import shutil
import sys
import uuid
from pathlib import Path

import animate
import art
import footage
import llm
import music
import voice
from config import ROOT, load_config
from errors import ClipForgeError, LLMError
from ffutil import probe, run_ffmpeg, verify_ffmpeg, video_encode_args
from logutil import add_file_handler, get_logger, remove_file_handler, stage_timer
from schemas import KIDS_LINE_MAX_CHARS, validate

log = get_logger("create")


class CreateError(ClipForgeError):
    stage = "create"


def _ccfg(cfg: dict) -> dict:
    c = cfg.get("create")
    if not isinstance(c, dict):
        raise CreateError("config is missing the create: block")
    return c


# ------------------------------------------------------------------ topics

def _used_topics_path(cfg: dict) -> Path:
    return ROOT / cfg["paths"]["cache_dir"] / "create_topics_used.json"


def load_used_topics(cfg: dict) -> list[str]:
    p = _used_topics_path(cfg)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("used-topics file unreadable (%s) - starting fresh", e)
        return []
    return [str(t) for t in data] if isinstance(data, list) else []


def mark_topic_used(topic: str, cfg: dict) -> None:
    used = load_used_topics(cfg)
    if topic.lower() not in {t.lower() for t in used}:
        used.append(topic)
    p = _used_topics_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(used, indent=2), encoding="utf-8")
    tmp.replace(p)


def pick_topic(topics: list[str], used: list[str], rng: random.Random,
               exclude: set[str] | None = None) -> str:
    """Random unused topic; when every topic is used, the rotation restarts.
    Pure given its inputs. Raises CreateError on an empty topic list."""
    exclude = {e.lower() for e in (exclude or set())}
    pool = [t for t in topics if t.strip() and t.lower() not in exclude]
    if not pool:
        raise CreateError("create.topics is empty (or every topic was excluded)")
    used_l = {u.lower() for u in used}
    fresh = [t for t in pool if t.lower() not in used_l]
    return rng.choice(fresh or pool)


# ------------------------------------------------------------------ script

def script_prompt(topic: str, cfg: dict) -> str:
    c = _ccfg(cfg)
    n = int(c.get("chapters", 8))
    if c.get("visual_style", "illustrated") == "illustrated":
        visual_rule = ("a 6-14 word description of ONE cartoon picture for the line: the "
                       "main animal or object first, what it is doing, then where (for "
                       "example 'a smiling orange octopus changing colour on a coral "
                       "reef'). One clear main subject per picture; no words or numbers "
                       "in the picture; no people or children.")
    else:
        visual_rule = ("a 2-5 word stock-footage search query using concrete nouns (for "
                       "example 'octopus swimming coral reef'); never ask for footage of "
                       "children.")
    return (
        f"Write a narrated educational YouTube video for {c.get('audience', 'young children')} "
        f"about: {topic}.\n\n"
        "Language: simple, warm, exciting English that a 7-year-old understands. "
        "Short sentences, at most 20 words per line. No scary or violent content, "
        "no medical advice, no brand names, no invented numbers. Only include facts "
        "that are well established and widely documented by reputable sources "
        "(NASA, national science museums, encyclopedias). If you are not sure a fact "
        "is true, leave it out.\n\n"
        "Structure:\n"
        "- intro: 3-4 lines (about 40 words) that greet the viewer and tease what "
        "they will learn.\n"
        f"- chapters: exactly {n} chapters. Each has a short title (at most 6 words) and "
        "exactly 14 lines of 8-14 words each (about 45-55 seconds read aloud). Explain "
        "each idea with a comparison or example a child already knows (for example "
        "'as long as a school bus', 'about the size of your thumb'). The FIRST line of "
        "every chapter must be a curiosity question (for example: 'Did you know an "
        "octopus has three hearts?') because each chapter is also posted on its own "
        "as a Short, so every chapter must make sense without the others. Put every "
        "factual claim the chapter makes into its `facts` list.\n"
        "- outro: 3-4 lines that recap the most surprising facts and say goodbye (you "
        "may say 'see you next time'; do not beg for likes or subscribers).\n\n"
        "For every line give `icons`: 1-3 emoji characters (the emoji itself, for "
        "example \"🐙\", \"❤️\", \"🌊\") showing what the line is about. They are drawn "
        "big on screen, so prefer concrete animals and objects; common emoji only, no "
        f"flags, no skin-tone or gender variants. Also give `visual`: {visual_rule}\n\n"
        "scene (for the intro and outro) and every chapter's own `scene`: the animated "
        "background, one of space, ocean, sky, meadow, snow, desert, body. Pick the "
        "setting that fits: planets and stars -> space, sea life -> ocean, insects and "
        "plants -> meadow, the human body -> body, polar animals -> snow, hot dry places "
        "-> desert, weather and birds -> sky.\n\n"
        "title: at most 60 characters, exciting but honest. description: 2-3 "
        "sentences written for parents. hashtags: 8-12 tags like #Octopus (letters "
        "and digits only).\n"
        "Return only JSON matching the schema."
    )


def llm_cfg(cfg: dict, model: str | None = None, provider: str = "gemini") -> dict:
    """Config copy with Create mode's LLM settings: a whole video script is a
    long generation (needs more than the 45s default timeout), and nested
    minItems/maxItems make Gemini reject the request outright (400), so those
    stay out of the response_schema - validate() still enforces them on the
    reply. Clipping mode keeps its own llm.* values; the singleton is never
    mutated."""
    c = _ccfg(cfg)
    over = {"gemini_timeout_s": float(c.get("llm_timeout_s", 120)),
            "max_retries": int(c.get("llm_max_retries", 1)),
            "backoff_base_seconds": float(c.get("llm_backoff_s", 5)),
            "gemini_strip_keys": ["minItems", "maxItems"]}
    if model:
        over[f"{provider}_model"] = model
    if provider == "ollama":
        over["ollama_timeout_s"] = float(c.get("ollama_timeout_s", 600))
    return {**cfg, "llm": {**cfg["llm"], **over}}


_benched: set = set()
_ollama_ok: dict[str, bool] = {}


def _ollama_up(cfg: dict) -> bool:
    """Is the local Ollama server answering? Checked once per process."""
    base = os.environ.get("OLLAMA_BASE_URL",
                          cfg["llm"].get("ollama_base_url", "http://localhost:11434"))
    if base not in _ollama_ok:
        try:
            import requests
            _ollama_ok[base] = requests.get(f"{base}/api/tags", timeout=3).status_code == 200
        except Exception:  # noqa: BLE001 - any failure means "not available"
            _ollama_ok[base] = False
        if not _ollama_ok[base]:
            log.warning("ollama is not running at %s - skipping the offline model", base)
    return _ollama_ok[base]


def ladder(cfg: dict, provider: str | None) -> list[tuple[str, str | None]]:
    """[(provider, model)] to try, in order. An explicit non-gemini provider
    (mock, a test) is used alone. Otherwise create.providers decides: a cloud
    provider without its key and an Ollama that is not running are skipped,
    so the list is only what can actually answer right now."""
    c = _ccfg(cfg)
    if provider != "gemini":
        return [(provider, None)]
    out: list[tuple[str, str | None]] = []
    for name in c.get("providers") or ["gemini"]:
        if name == "groq" and os.environ.get("GROQ_API_KEY"):
            out += [("groq", str(m)) for m in c.get("groq_models") or []]
        elif name == "ollama" and c.get("ollama_model") and _ollama_up(cfg):
            out.append(("ollama", str(c["ollama_model"])))
        elif name == "gemini" and os.environ.get("GEMINI_API_KEY"):
            out += [("gemini", str(m)) for m in c.get("gemini_models") or []]
    return out or [("gemini", None)]


def ask(task: str, prompt: str, cfg: dict, provider: str | None,
        context: dict) -> dict:
    """llm.complete_json (schema name == task) down the provider ladder: the
    first one that answers wins. Free cloud tiers get overloaded (Gemini
    answered 503/504 for whole nights), so an unattended run falls through to
    the next provider and finally to the offline model, which cannot be
    rate-limited. A provider/model that fails with a retryable error is
    benched for the rest of this video. Raises the last LLMError."""
    steps = ladder(cfg, provider)
    usable = [s for s in steps if s not in _benched] or steps[-1:]
    last: LLMError | None = None
    for i, (name, model) in enumerate(usable):
        try:
            return llm.complete_json(task, task, prompt, provider=name,
                                     cfg=llm_cfg(cfg, model, name), context=context)
        except LLMError as e:
            last = e
            if i + 1 < len(usable):
                if e.retryable:
                    _benched.add((name, model))
                log.warning("%s: %s/%s failed (%s) - trying %s/%s", task, name, model,
                            e.message, *usable[i + 1])
    raise last


def write_script(topic: str, cfg: dict, provider: str | None) -> dict:
    data = ask(
        "kids_long_script", script_prompt(topic, cfg), cfg, provider,
        {"topic": topic, "chapters": int(_ccfg(cfg).get("chapters", 8))})
    data["topic"] = topic  # the requested topic is authoritative
    return data


def fact_check_prompt(topic: str, chapter: dict) -> str:
    lines = "\n".join(f"{i + 1}. {ln['text']}" for i, ln in enumerate(chapter["lines"]))
    facts = "\n".join(f"- {f}" for f in chapter.get("facts", []))
    return (
        f"You are a careful fact-checker for a children's educational video about "
        f"{topic}. Check this chapter line by line against well-established "
        "scientific knowledge.\n\n"
        f"Chapter title: {chapter['title']}\nLines:\n{lines}\n"
        f"Claimed facts:\n{facts}\n\n"
        "Rules:\n"
        '- verdict "pass": every statement is accurate (simplifying for children is '
        "fine as long as it is not false).\n"
        '- verdict "fix": some statements are wrong or misleading but can be '
        f"corrected. Return corrected_lines with exactly {len(chapter['lines'])} "
        "strings, in the same order and the same simple style, changing only what is "
        "wrong.\n"
        '- verdict "reject": the chapter\'s main idea is false or cannot be verified.\n'
        "List every problem you found in issues (empty list if none).\n"
        "Return only JSON matching the schema."
    )


def apply_fact_check(chapter: dict, verdict: dict) -> dict | None:
    """Pure: the checked chapter, or None when it must be dropped. A "fix" whose
    corrected_lines count doesn't match the chapter is treated as a reject -
    a partial correction can't be trusted to line up with the visuals."""
    v = verdict.get("verdict")
    if v == "pass":
        return chapter
    if v == "fix":
        fixed = [str(t).strip() for t in verdict.get("corrected_lines") or []]
        # a fix must still fit the script schema, or the final validate() of
        # the whole script would fail the job instead of dropping this chapter
        if len(fixed) != len(chapter["lines"]) or \
                any(not t or len(t) > KIDS_LINE_MAX_CHARS for t in fixed):
            return None
        return {**chapter, "lines": [{**ln, "text": t}
                                     for ln, t in zip(chapter["lines"], fixed)]}
    return None


def checked_script(cfg: dict, provider: str | None, rng: random.Random,
                   topic: str | None = None) -> dict:
    """Script whose surviving chapters all passed (or were fixed by) the
    fact-check. Tries up to create.max_topic_attempts topics (a forced
    --topic is only tried once). Raises CreateError when none qualifies."""
    c = _ccfg(cfg)
    attempts = 1 if topic else int(c.get("max_topic_attempts", 3))
    min_ch = int(c.get("min_chapters", 5))
    tried: set[str] = set()
    last_llm_error: LLMError | None = None
    for attempt in range(1, attempts + 1):
        t = topic or pick_topic(list(c.get("topics") or []), load_used_topics(cfg),
                                rng, exclude=tried)
        tried.add(t)
        log.info("topic attempt %d/%d: %s", attempt, attempts, t)
        try:
            script = write_script(t, cfg, provider)
        except LLMError as e:
            last_llm_error = e
            log.warning("script generation failed for '%s': %s", t, e)
            continue
        kept, report = [], []
        for ch in script["chapters"]:
            try:
                verdict = ask("fact_check", fact_check_prompt(t, ch), cfg,
                              provider, {"topic": t})
            except LLMError as e:
                # cannot verify -> do not publish it
                verdict = {"verdict": "reject", "issues": [f"fact-check failed: {e}"]}
            checked = apply_fact_check(ch, verdict)
            report.append({"chapter": ch["title"], "verdict": verdict["verdict"],
                           "issues": verdict.get("issues", []),
                           "kept": checked is not None})
            if checked is not None:
                kept.append(checked)
        log.info("fact-check '%s': %d/%d chapters kept", t, len(kept),
                 len(script["chapters"]))
        if len(kept) >= min_ch:
            script = {**script, "chapters": kept}
            validate(script, "kids_long_script")
            script["fact_check"] = report
            return script
        log.warning("'%s' kept only %d chapters (< %d) - trying another topic",
                    t, len(kept), min_ch)
    raise CreateError(f"no topic produced {min_ch}+ fact-checked chapters "
                      f"after {attempts} attempt(s)",
                      detail=(f"script generation failed: {last_llm_error}"
                              if last_llm_error else None),
                      retryable=False)


# ------------------------------------------------------------------ timeline

def build_timeline(sections: list[dict], durations: list[float],
                   line_gap: float, section_gap: float, tail: float = 0.6) -> dict:
    """Pure. `sections` = [{"key", "title", "lines": [{text, visual}]}] in order;
    `durations` = narration seconds per line (flattened order). Returns
    {"lines": [{section, title, text, visual, start, end}],
     "sections": [{key, title, start, end, first, last}], "total": seconds}."""
    flat = [(si, ln) for si, s in enumerate(sections) for ln in s["lines"]]
    if len(flat) != len(durations):
        raise CreateError(f"timeline: {len(flat)} lines but {len(durations)} durations")
    if not flat:
        raise CreateError("timeline: no lines")
    lines, t = [], 0.0
    for idx, ((si, ln), dur) in enumerate(zip(flat, durations)):
        if dur <= 0:
            raise CreateError(f"timeline: line {idx} has non-positive duration")
        start, end = t, t + float(dur)
        lines.append({"section": si, "title": sections[si]["title"],
                      "text": ln["text"], "visual": ln["visual"],
                      "start": round(start, 3), "end": round(end, 3)})
        nxt = flat[idx + 1][0] if idx + 1 < len(flat) else None
        t = end + (0.0 if nxt is None else line_gap if nxt == si else section_gap)
    spans = []
    for si, s in enumerate(sections):
        idxs = [i for i, ln in enumerate(lines) if ln["section"] == si]
        if not idxs:
            continue
        spans.append({"key": s["key"], "title": s["title"],
                      "start": lines[idxs[0]]["start"], "end": lines[idxs[-1]]["end"],
                      "first": idxs[0], "last": idxs[-1]})
    return {"lines": lines, "sections": spans,
            "total": round(lines[-1]["end"] + tail, 3)}


def shot_frames(bounds: list[float], fps: float) -> list[int]:
    """Pure: frames per shot for consecutive time boundaries [t0, t1, ..., tn],
    computed from cumulative rounding so the track never drifts from the audio
    (per-shot rounding would accumulate up to half a frame per shot)."""
    try:
        marks = animate.frame_marks(bounds, fps)
    except animate.AnimateError as e:
        raise CreateError(f"shot_frames: {e.message}") from e
    return [b - a for a, b in zip(marks, marks[1:])]


def chapter_timestamps(timeline: dict) -> str:
    """Pure: YouTube chapter list ('0:00 Intro' first, one line per section)."""
    out = []
    for i, s in enumerate(timeline["sections"]):
        start = 0 if i == 0 else int(s["start"])
        out.append(f"{start // 60}:{start % 60:02d} {s['title']}")
    return "\n".join(out)


def build_scenes(sections: list[dict], video_title: str) -> list[dict]:
    """Pure: one animate.py scene per narrated line, in timeline order. The
    intro's first line carries the title card; each chapter's first line its
    'Chapter N' banner."""
    out, ch_no = [], 0
    for s in sections:
        is_ch = s["key"].startswith("ch")
        ch_no += is_ch
        for j, ln in enumerate(s["lines"]):
            banner = None
            if j == 0 and s["key"] == "intro":
                banner = {"kind": "title", "text": video_title, "label": None}
            elif j == 0 and is_ch:
                banner = {"kind": "chapter", "text": s["title"],
                          "label": f"Chapter {ch_no}"}
            out.append({"scene": s.get("scene", "sky"),
                        "icons": list(ln.get("icons") or []), "banner": banner})
    return out


def slice_words(words: list[dict], start: float, end: float) -> list[dict]:
    """Pure: words inside [start, end], re-timed relative to `start`."""
    out = []
    for w in words:
        if w["start"] >= start - 0.05 and w["end"] <= end + 0.05:
            out.append({"word": w["word"],
                        "start": round(max(0.0, w["start"] - start), 3),
                        "end": round(max(0.0, w["end"] - start), 3)})
    return out


# ------------------------------------------------------------------ audio

def assemble_voice(line_wavs: list[str], timeline: dict, out_path: Path) -> Path:
    """Place every line wav at its timeline start into one mono wav."""
    import numpy as np
    import soundfile as sf
    sr = None
    clips = []
    for p in line_wavs:
        try:
            data, rate = sf.read(p, dtype="float32")
        except (OSError, RuntimeError) as e:
            raise CreateError(f"could not read narration {p}", detail=str(e)) from e
        if sr is None:
            sr = rate
        elif rate != sr:
            raise CreateError(f"narration sample rate mismatch in {p}")
        clips.append(data.mean(axis=1) if data.ndim > 1 else data)
    buf = np.zeros(int(round(timeline["total"] * sr)) + 1, dtype="float32")
    for data, ln in zip(clips, timeline["lines"]):
        i = int(round(ln["start"] * sr))
        n = min(len(data), len(buf) - i)
        if n > 0:
            buf[i:i + n] += data[:n]
    sf.write(str(out_path), np.clip(buf, -1.0, 1.0), sr)
    return out_path


def slice_wav(src: Path, start: float, end: float, out_path: Path) -> Path:
    import soundfile as sf
    data, sr = sf.read(str(src), dtype="float32")
    a, b = int(round(start * sr)), int(round(end * sr))
    if b <= a:
        raise CreateError(f"empty audio slice {start:.2f}-{end:.2f}s")
    sf.write(str(out_path), data[a:b], sr)
    return out_path


# ------------------------------------------------------------------ video

def _shot_args(asset: dict | None, frames: int, w: int, h: int, fps: int,
               color: str, out: Path, cfg: dict) -> list:
    """ffmpeg args for one silent shot of exactly `frames` frames at w×h."""
    enc = video_encode_args(cfg, final=False)
    fit = (f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
           f"setsar=1,fps={fps},format=yuv420p")
    if asset is None:
        return ["-f", "lavfi", "-i", f"color=c={color}:s={w}x{h}:r={fps}",
                "-frames:v", frames, "-an", *enc, out]
    if asset["kind"] == "image":
        zw, zh = int(w * 1.12) // 2 * 2, int(h * 1.12) // 2 * 2
        inc = 0.10 / max(1, frames)          # slow 10% push-in over the shot
        vf = (f"scale={zw}:{zh}:force_original_aspect_ratio=increase,crop={zw}:{zh},"
              f"zoompan=z='min(1+{inc:.6f}*on,1.10)':d=1:"
              f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={w}x{h}:fps={fps},"
              "setsar=1,format=yuv420p")
        return ["-loop", "1", "-framerate", fps, "-i", asset["path"], "-vf", vf,
                "-frames:v", frames, "-an", *enc, out]
    need = frames / fps
    start = 0.5 if float(asset.get("duration") or 0) > need + 1.5 else 0.0
    return ["-ss", f"{start:.2f}", "-stream_loop", "-1", "-i", asset["path"],
            "-vf", fit, "-frames:v", frames, "-an", *enc, out]


def render_track(shots: list[dict], bounds: list[float], w: int, h: int,
                 fps: int, work: Path, cfg: dict) -> Path:
    """Render each shot then stream-copy concat them into one silent video.
    `shots[i]` = {"asset": dict|None, "color": str}; `bounds` has len+1 times."""
    frames = shot_frames(bounds, fps)
    work.mkdir(parents=True, exist_ok=True)
    parts = []
    for i, (shot, n) in enumerate(zip(shots, frames)):
        out = work / f"shot_{i:03d}.mp4"
        try:
            run_ffmpeg(_shot_args(shot["asset"], n, w, h, fps, shot["color"], out, cfg))
        except ClipForgeError as e:
            if shot["asset"] is None:
                raise
            # a broken download/codec must not sink the video: colour card instead
            log.warning("shot %d failed (%s) - colour card instead", i, e.message)
            run_ffmpeg(_shot_args(None, n, w, h, fps, shot["color"], out, cfg))
        parts.append(out)
    listing = work / "concat.txt"
    listing.write_text("".join(
        "file '" + str(p.resolve()).replace("\\", "/").replace("'", "'\\''") + "'\n"
        for p in parts), encoding="utf-8")
    track = work / "track.mp4"
    run_ffmpeg(["-f", "concat", "-safe", "0", "-i", listing, "-c", "copy", track])
    return track


def finish_video(track: Path, audio: Path, words: list[dict], clip_dir: Path,
                 cfg: dict, *, captions: bool, preset: str,
                 anchor: float | None, music_path: Path | None,
                 music_db: float) -> Path:
    """Mux narration, burn captions, mix music + loudnorm → clip_dir/final.mp4."""
    from captions import caption_clip
    clip_dir.mkdir(parents=True, exist_ok=True)
    muxed = clip_dir / "muxed.mp4"
    run_ffmpeg(["-i", track, "-i", audio, "-map", "0:v", "-map", "1:a",
                "-c:v", "copy", "-c:a", "aac", "-b:a", cfg["render"]["audio_bitrate"],
                "-shortest", muxed])
    stage = muxed
    if captions and words:
        captioned = clip_dir / "captioned.mp4"
        caption_clip(muxed, words, captioned, cfg, preset_name=preset, anchor=anchor)
        stage = captioned
    final = clip_dir / "final.mp4"
    mixed = music.mix_audio(stage, final, cfg, music_path=music_path,
                            volume_db=music_db)
    if mixed is None:
        shutil.copyfile(stage, final)
    for tmp in (muxed, clip_dir / "captioned.mp4"):
        tmp.unlink(missing_ok=True)
    return final


# ------------------------------------------------------------------ metadata

def _virality(cfg: dict) -> dict:
    v = {"score": float(_ccfg(cfg).get("virality_score", 60)), "verdict": "post",
         "reasons": ["Create mode: original script, fact-checked chapter by chapter"]}
    validate(v, "virality")
    return v


def _credits(assets: list[dict | None], music_attr: str, style: str = "") -> str:
    providers = sorted({a["provider"] for a in assets if a})
    names = {"pexels": "Pexels", "pixabay": "Pixabay", "nasa": "NASA"}
    parts = []
    if style == "illustrated":
        parts.append("Illustrations: AI-generated on our own computer (DreamShaper 8, "
                     "CreativeML OpenRAIL-M). Animation and owl mascot: original.")
    if style == "animated":
        parts.append("Illustrations: Noto Emoji by Google (Apache License 2.0). "
                     "Animation and mascot: original.")
    if providers:
        parts.append("Footage: " + ", ".join(names[p] for p in providers)
                     + " (free-to-use licenses).")
    parts.append("Narration: AI voice. Facts checked before publishing.")
    if music_attr:
        parts.append(music_attr)
    return " ".join(parts)


def _sources(assets: list[dict | None]) -> list[dict]:
    seen, out = set(), []
    for a in assets:
        if a and a["id"] not in seen:
            seen.add(a["id"])
            out.append({"id": a["id"], "provider": a["provider"],
                        "page_url": a.get("page_url", ""),
                        "author": a.get("author", ""), "license": a["license"]})
    return out


def _niche(text: str, meta: dict, cfg: dict, provider: str | None) -> str:
    try:
        import classify
        return classify.classify_niche(text, meta, cfg, provider)
    except Exception as e:  # noqa: BLE001 - niche tagging never blocks a render
        log.warning("niche classification failed (%s)", e)
        return "other"


def _write_clip(clip_dir: Path, index: int, final: Path, meta: dict,
                virality: dict, niche: str, fmt: str, mock: bool = False) -> dict:
    # Kids content is never auto-published: this per-clip flag makes
    # upload_scheduler.approval_ok demand an explicit approve even when the
    # global upload.require_approval is off. Mock-provider runs narrate
    # placeholder text, so they are excluded from upload outright.
    upload = {"require_approval": True, **({"exclude": True} if mock else {})}
    payload = {**meta, "virality": virality, "niche": niche, "upload": upload}
    validate({k: meta[k] for k in ("title", "description", "hashtags")},
             "clip_metadata")
    (clip_dir / "metadata.json").write_text(json.dumps(payload, indent=2),
                                            encoding="utf-8")
    duration = float(probe(final)["duration"])
    return {"index": index, "duration": round(duration, 2), "path": str(final),
            "srt": str(final.with_suffix(".srt")), "metadata": meta,
            "virality": virality, "niche": niche, "format": fmt,
            "hook": meta["title"]}


# ------------------------------------------------------------------ one video

def create_video(cfg: dict, provider: str | None = None, topic: str | None = None,
                 rng: random.Random | None = None, long_on: bool | None = None,
                 shorts_on: bool | None = None,
                 max_shorts: int | None = None) -> Path:
    """Make one long video + chapter Shorts. Returns the job directory. The
    job record is always written (status done/failed); a failed Short never
    fails the long video."""
    from pipeline import new_job_dir
    import transcribe

    c = _ccfg(cfg)
    rng = rng or random.Random()
    long_on = bool(c.get("long", {}).get("enabled", True)) if long_on is None else long_on
    shorts_on = (bool(c.get("shorts", {}).get("enabled", True))
                 if shorts_on is None else shorts_on)
    if not (long_on or shorts_on):
        raise CreateError("both the long video and the Shorts are disabled")
    resolved = llm.resolve_provider(cfg, provider)
    fps = int(c.get("fps", 30))
    visual_style = str(c.get("visual_style", "illustrated"))
    if visual_style not in ("illustrated", "animated", "footage"):
        raise CreateError("create.visual_style must be 'illustrated', 'animated' or "
                          f"'footage', not '{visual_style}'", retryable=False)

    _benched.clear()            # each video gives every model a fresh chance
    job_dir = new_job_dir(cfg, f"create-{topic or 'auto'}")
    fh = add_file_handler(job_dir / "create.log")
    timings: dict = {}
    notes: list[str] = []
    job = {"job_id": uuid.uuid4().hex[:12],
           "created": dt.datetime.now().isoformat(timespec="seconds"),
           "source": f"create:{topic or 'auto'}", "status": "running",
           "settings": {"mode": "create", "provider": resolved, "long": long_on,
                        "shorts": shorts_on, "visual_style": visual_style,
                        "voice": c.get("voice", {}).get("voice", "")},
           "stages": {}, "clips": [], "notes": notes}
    try:
        with stage_timer(log, "script", timings):
            script = checked_script(cfg, resolved, rng, topic)
        topic = script["topic"]
        job["source"] = f"create:{topic}"
        (job_dir / "script.json").write_text(json.dumps(script, indent=2),
                                             encoding="utf-8")

        sections = ([{"key": "intro", "title": "Intro", "scene": script["scene"],
                      "lines": script["intro"]}]
                    + [{"key": f"ch{i}", "title": ch["title"], "scene": ch["scene"],
                        "lines": ch["lines"]}
                       for i, ch in enumerate(script["chapters"], 1)]
                    + [{"key": "outro", "title": "Recap", "scene": script["scene"],
                        "lines": script["outro"]}])
        texts = [ln["text"] for s in sections for ln in s["lines"]]

        with stage_timer(log, "voice", timings):
            synth = voice.synthesize_lines(texts, job_dir / "voice_lines", cfg)
            timeline = build_timeline(sections, [r["duration_s"] for r in synth],
                                      float(c.get("line_gap_s", 0.25)),
                                      float(c.get("chapter_gap_s", 0.7)))
            voice_wav = assemble_voice([r["path"] for r in synth], timeline,
                                       job_dir / "voice.wav")
            (job_dir / "timeline.json").write_text(json.dumps(timeline, indent=2),
                                                   encoding="utf-8")
            min_long = float(c.get("min_long_s", 300))
            if long_on and timeline["total"] < min_long:
                notes.append(f"long video is only {timeline['total']:.0f}s "
                             f"(target >= {min_long:.0f}s): the script came back short")
                log.warning("%s", notes[-1])

        seed = rng.randrange(1_000_000)
        lines = timeline["lines"]
        layers: list[dict] = []     # illustrated mode: one picture per beat
        line_shot: list[int] = []   # timeline line -> beat index
        if visual_style == "illustrated":
            # before Whisper: the drawing model needs ~3.1 GB of the 4 GB GPU
            with stage_timer(log, "art", timings):
                acfg = c.get("art", {})
                beats = art.plan_beats(lines, float(acfg.get("beat_s", 8.0)))
                prompts = [art.art_prompt(b["visual"], sections[b["section"]]["scene"],
                                          str(acfg.get("style", "")))
                           for b in beats]
                pics = art.draw_images([(pr, seed + k) for k, pr in enumerate(prompts)],
                                       cfg)
                layers = [art.cutout(pic, cfg) for pic in pics]
                for k, b in enumerate(beats):
                    line_shot += [k] * (b["last"] - b["first"] + 1)
                (job_dir / "art.json").write_text(json.dumps(
                    [{**b, "prompt": pr, "image": str(pic), **ly}
                     for b, pr, pic, ly in zip(beats, prompts, pics, layers)], indent=2),
                    encoding="utf-8")
                no_cut = sum(1 for ly in layers if ly["fg"] is None)
                if no_cut:
                    notes.append(f"{no_cut}/{len(layers)} pictures had no clear main "
                                 "subject to cut out (camera move only)")

        with stage_timer(log, "transcribe", timings):
            # The text is already known (we wrote it); Whisper only supplies
            # word timings, so a small English model is plenty and many times
            # faster than the clipping-mode large-v3 default.
            wcfg = {**cfg, "whisper": {**cfg["whisper"], "language": "en",
                                       "model_override": str(c.get("whisper_model",
                                                                   "small"))}}
            words = transcribe.transcribe(voice_wav, wcfg)["words"]

        starts = [0.0] + [ln["start"] for ln in lines[1:]]
        ends = starts[1:] + [timeline["total"]]
        shots: list[dict] = []      # footage mode: one stock shot per line
        scenes: list[dict] = []     # animated/illustrated: one cartoon scene per line
        if visual_style == "footage":
            with stage_timer(log, "footage", timings):
                used: set = set()
                colors = list(c.get("footage", {}).get("card_colors") or ["0x1E88E5"])
                for i, ln in enumerate(lines):
                    asset = footage.find_asset(ln["visual"], ends[i] - starts[i],
                                               cfg, used)
                    shots.append({"asset": asset, "color": colors[i % len(colors)]})
                n_cards = sum(1 for s in shots if s["asset"] is None)
                if n_cards:
                    notes.append(f"{n_cards}/{len(shots)} shots used colour cards "
                                 "(no matching footage / no API key)")
        else:
            scenes = build_scenes(sections, script["title"])
            for i, sid in enumerate(line_shot):
                scenes[i]["art"] = {**layers[sid], "shot": sid}

        music_path, music_attr = None, ""
        try:
            track = music.pick_track_by_mood(str(c.get("music_mood", "")))
            music_path = music.ensure_track(track)
            music_attr = music.attribution_for(track)
        except music.MusicError as e:
            notes.append(f"no background music: {e.message}")
            log.warning("no background music: %s", e)
        music_db = float(c.get("music_volume_db", -22))
        assets = [s["asset"] for s in shots]
        credits = _credits(assets, music_attr, style=visual_style)
        sources = _sources(assets)
        virality = _virality(cfg)
        made_for_kids = bool(c.get("made_for_kids", True))

        index = 0
        if long_on:
            with stage_timer(log, "render_long", timings):
                lc = c.get("long", {})
                w, h = int(lc.get("width", 1920)), int(lc.get("height", 1080))
                if visual_style != "footage":
                    track_path = animate.render_track(
                        scenes, starts + [timeline["total"]], w, h, fps, voice_wav,
                        job_dir / "work_long" / "track.mp4", cfg, seed)
                else:
                    track_path = render_track(shots, starts + [timeline["total"]],
                                              w, h, fps, job_dir / "work_long", cfg)
                clip_dir = job_dir / f"clip_{index:02d}"
                final = finish_video(track_path, voice_wav, words, clip_dir, cfg,
                                     captions=bool(lc.get("captions", True)),
                                     preset=str(lc.get("caption_preset",
                                                       "clean-minimal")),
                                     anchor=None, music_path=music_path,
                                     music_db=music_db)
                desc = (f"{script['description'].strip()}\n\nChapters:\n"
                        f"{chapter_timestamps(timeline)}\n\n{credits}")
                meta = {"title": script["title"], "description": desc,
                        "hashtags": script["hashtags"], "format": "long",
                        "made_for_kids": made_for_kids,
                        "create": {"topic": topic, "sources": sources,
                                   "fact_check": script["fact_check"]}}
                niche = _niche(" ".join(texts), meta, cfg, resolved)
                job["clips"].append(_write_clip(clip_dir, index, final, meta,
                                                virality, niche, "long",
                                                mock=resolved == "mock"))
                index += 1

        if shorts_on:
            sc = c.get("shorts", {})
            w, h = int(sc.get("width", 1080)), int(sc.get("height", 1920))
            cap = int(max_shorts if max_shorts is not None
                      else sc.get("max_per_video", 8))
            chapters = [s for s in timeline["sections"] if s["key"].startswith("ch")]
            with stage_timer(log, "render_shorts", timings):
                for k, sec in enumerate(chapters[:cap]):
                    try:
                        cs = sec["start"]
                        nxt = (timeline["lines"][sec["last"] + 1]["start"]
                               if sec["last"] + 1 < len(lines) else timeline["total"])
                        ce = min(sec["end"] + 0.5, nxt)
                        idxs = range(sec["first"], sec["last"] + 1)
                        bounds = [cs] + [starts[i] for i in idxs][1:] + [ce]
                        work = job_dir / f"work_short_{k:02d}"
                        if visual_style != "footage":
                            sub = [dict(scenes[i]) for i in idxs]
                            # a Short stands alone: banner without "Chapter N"
                            sub[0]["banner"] = {"kind": "chapter", "text": sec["title"],
                                                "label": None}
                            track_path = animate.render_track(
                                sub, bounds, w, h, fps, voice_wav,
                                work / "track.mp4", cfg, seed)
                        else:
                            track_path = render_track([shots[i] for i in idxs],
                                                      bounds, w, h, fps, work, cfg)
                        audio = slice_wav(voice_wav, cs, ce, work / "voice.wav")
                        clip_dir = job_dir / f"clip_{index:02d}"
                        final = finish_video(
                            track_path, audio, slice_words(words, cs, ce), clip_dir,
                            cfg, captions=True,
                            preset=str(sc.get("caption_preset", "karaoke-pop")),
                            anchor=float(sc.get("caption_anchor", 0.60)),
                            music_path=music_path, music_db=music_db)
                        first_line = timeline["lines"][sec["first"]]["text"]
                        used_ids = {shots[i]["asset"]["id"] for i in idxs
                                    if shots and shots[i]["asset"]}
                        meta = {"title": sec["title"][:60],
                                "description": (f"{first_line}\n\nFrom our video "
                                                f"about {topic}. {credits}"),
                                "hashtags": script["hashtags"], "format": "short",
                                "made_for_kids": made_for_kids,
                                "create": {"topic": topic, "chapter": sec["title"],
                                           "sources": [s for s in sources
                                                       if s["id"] in used_ids]}}
                        text = " ".join(timeline["lines"][i]["text"] for i in idxs)
                        niche = _niche(text, meta, cfg, resolved)
                        job["clips"].append(_write_clip(clip_dir, index, final, meta,
                                                        virality, niche, "short",
                                                        mock=resolved == "mock"))
                        index += 1
                    except Exception as e:  # noqa: BLE001 - one bad Short never sinks the rest
                        notes.append(f"short '{sec['title']}' failed: {e}")
                        log.error("short '%s' failed: %s", sec["title"], e)

        if not job["clips"]:
            raise CreateError("no clip rendered", retryable=False)
        mark_topic_used(topic, cfg)
        for d in job_dir.glob("work_*"):
            shutil.rmtree(d, ignore_errors=True)
        job["status"] = "done"
    except Exception as e:  # noqa: BLE001 - the job record must capture any failure
        job["status"] = "failed"
        notes.append(f"job failed: {e}")
        log.error("create job failed: %s", e)
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
    log.info("create job %s: %s (%d clip(s)) -> %s", job["job_id"], job["status"],
             len(job["clips"]), job_dir)
    return job_dir


# ------------------------------------------------------------------ CLI

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ClipForge Create mode: kids explainer "
                                             "videos (long-form + chapter Shorts)")
    ap.add_argument("--topic", help="force a topic (otherwise the next unused one)")
    ap.add_argument("--count", type=int, default=1, help="videos to make (default 1)")
    ap.add_argument("--provider", help="LLM provider override (mock|gemini|...)")
    ap.add_argument("--no-long", action="store_true", help="skip the long video")
    ap.add_argument("--no-shorts", action="store_true", help="skip chapter Shorts")
    ap.add_argument("--max-shorts", type=int, help="cap Shorts per video")
    ap.add_argument("--workspace", help="library + YouTube channel (default create.workspace)")
    args = ap.parse_args(argv)
    if args.count < 1:
        ap.error("--count must be at least 1")

    import config as config_mod
    import workspaces
    c = load_config().get("create") or {}
    ws = args.workspace or str(c.get("workspace", "default"))
    workspaces.ensure_workspace(ws, str(c.get("workspace_name", ws.title())))
    config_mod.current_workspace.set(ws)   # paths.output_dir -> this workspace's library
    cfg = load_config()
    verify_ffmpeg(cfg)
    failed = 0
    for n in range(args.count):
        job_dir = create_video(cfg, provider=args.provider,
                               topic=args.topic if n == 0 else None,
                               long_on=False if args.no_long else None,
                               shorts_on=False if args.no_shorts else None,
                               max_shorts=args.max_shorts)
        status = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))["status"]
        print(f"[{n + 1}/{args.count}] {status}: {job_dir}")
        failed += status != "done"
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
