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
import music
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
        "TONE - this is the whole point: a fast-talking, swaggering, funny narrator who "
        "talks straight to the viewer, like a charming hustler in a crime comedy or a "
        "wild Wall Street party story. Tell it as a STORY with a small invented crew (2-3 "
        "original characters with nicknames, for example 'Big Tony', 'Lucky Lou', "
        "'Vinnie Two-Cards') who make the money moves: one plays it smart, one gets "
        "burned. Short punchy sentences, dramatic one-liners, playful asides ('and that, "
        "my friend, is where the bank gets you'), money-as-the-mob metaphors (interest is "
        "'the vig', the bank 'always gets its cut'). Keep it clean: no swearing, no real "
        "people, no movie or show titles, no quotes from films, and nobody breaks the "
        "law - the gangster flavour is only a joke about how money works. Every joke "
        "still teaches the real idea; no jargon without a one-line explanation.\n\n"
        f"Return exactly {slides} slides. Each slide has:\n"
        "- layout: one of title (the opening hook: a big heading and 1-3 objects), "
        "compare (2-4 options side by side), list (2-4 points), big_number (one striking "
        "number that counts up), flow (2-4 steps joined by arrows), bars (2-4 values as a "
        "bar chart; every bar the SAME kind of amount, e.g. the monthly cost of each "
        "option, never a per-mile fee next to a total). The first slide is title, the "
        "last is a list recap. Use a variety.\n"
        "- kicker: a 1-3 word case-file label shown above the heading (for example "
        "'THE SETUP', 'THE CATCH', 'THE VIG', 'THE VERDICT').\n"
        "- heading: at most 6 words, punchy.\n"
        "- highlight: 1-3 words copied exactly from the heading, the key idea, shown "
        "in gold.\n"
        "- sticker: a red rubber stamp of 1-2 words (for example 'DENIED', 'APPROVED', "
        "'BUSTED', 'PAID IN FULL', 'RIP-OFF') on about every third slide; otherwise "
        "\"\".\n"
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
        "listed.\n"
        "- facts: every factual claim the slide makes.\n\n"
        "Accuracy: only well-established, widely documented facts. Numbers are "
        "illustrative examples ('for example', 'around', 'can'), round and realistic; "
        "never present today's rates or prices as facts. No brand names. This is "
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
        "numbers are fine). The narration is deliberately playful: invented characters, "
        "jokes and gangster metaphors ('the vig', 'the bank gets its cut') are fine - "
        "judge only the money facts behind them.\n"
        '- verdict "fix": something is wrong or misleading but fixable. Return '
        f"corrected_lines with exactly {len(slide['lines'])} strings, same order and "
        "style, changing only what is wrong.\n"
        '- verdict "reject": the slide\'s main point is false or reads as personal '
        "financial advice.\n"
        "List every problem in issues (empty list if none).\n"
        "Return only JSON matching the schema."
    )


def checked_script(topic: str, n_slides: int, cfg: dict, provider: str | None) -> dict:
    """Script whose slides passed (or were fixed by) the fact-check. Rejected
    slides are dropped; more than a quarter rejected fails the video, since
    the remaining slides would no longer tell a coherent story."""
    script = create.ask("explainer_script", script_prompt(topic, n_slides), cfg, provider,
                        {"topic": topic, "slides": n_slides})
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


_FLUENT = ("https://raw.githubusercontent.com/microsoft/fluentui-emoji/main/assets/"
           "{folder}/3D/{file}_3d.png")      # MIT (Microsoft Fluent Emoji)


def icon_png(token: str, cfg: dict) -> str | None:
    """A PNG for one emoji: Microsoft Fluent Emoji 3D (MIT) when its folder
    matches the emoji's Unicode name, else Noto Emoji (Apache-2.0, via
    animate.icon_sprite's cache). None when neither exists - text-only card."""
    import unicodedata
    import urllib.error
    import urllib.parse
    import urllib.request
    import animate
    from config import ROOT
    emoji = animate.resolve_emoji(token or "")
    if not emoji:
        return None
    folder = unicodedata.name(emoji[0], "").capitalize()
    if folder and len(emoji.replace("️", "")) == 1:
        d = ROOT / cfg["paths"]["cache_dir"] / "emoji" / "fluent"
        stem = folder.lower().replace(" ", "_")
        path = d / f"{stem}_3d.png"
        miss = d / f"{stem}.missing"
        if path.is_file():
            return str(path)
        if not miss.exists():
            url = _FLUENT.format(folder=urllib.parse.quote(folder), file=urllib.parse.quote(stem))
            d.mkdir(parents=True, exist_ok=True)
            part = d / f"{stem}.part"
            try:
                with urllib.request.urlopen(url, timeout=30) as resp:
                    part.write_bytes(resp.read())
                part.replace(path)
                return str(path)
            except urllib.error.HTTPError as err:
                if err.code == 404:
                    miss.touch()          # its CLDR folder name differs: use Noto
            except OSError as err:
                log.warning("Fluent icon download failed for %r: %s", token, err)
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


# ------------------------------------------------------------------ one video

def make_video(cfg: dict, topic: str, n_slides: int | None = None,
               provider: str | None = None) -> Path:
    """One explainer video. Returns the job dir; job.json always written."""
    from pipeline import new_job_dir
    import llm
    import transcribe

    e = _ecfg(cfg)
    n_slides = int(n_slides or e.get("slides", 12))
    resolved = llm.resolve_provider(cfg, provider)
    fps = int(e.get("fps", 30))
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
        slides = script["slides"]
        sections = [{"key": f"s{i}", "title": s["heading"],
                     "lines": [{"text": t, "visual": ""} for t in s["lines"]]}
                    for i, s in enumerate(slides)]
        texts = [t for s in slides for t in s["lines"]]

        with stage_timer(log, "voice", timings):
            vcfg = {**cfg, "create": {**cfg["create"],
                                      "voice": {**cfg["create"]["voice"], **e.get("voice", {})}}}
            synth = voice.synthesize_lines(texts, job_dir / "voice_lines", vcfg)
            timeline = create.build_timeline(sections, [r["duration_s"] for r in synth],
                                             float(e.get("line_gap_s", 0.2)),
                                             float(e.get("slide_gap_s", 0.6)))
            voice_wav = create.assemble_voice([r["path"] for r in synth], timeline,
                                              job_dir / "voice.wav")

        with stage_timer(log, "art", timings):     # before Whisper: GPU memory
            pictures = draw_objects([it["object"] for s in slides for it in s["items"]], cfg)

        with stage_timer(log, "transcribe", timings):
            wcfg = {**cfg, "whisper": {**cfg["whisper"], "language": "en",
                                       "model_override": str(e.get("whisper_model", "small"))}}
            words = transcribe.transcribe(voice_wav, wcfg)["words"]

        secs = timeline["sections"]
        bounds = [0.0] + [s["start"] for s in secs[1:]] + [timeline["total"]]
        plan = []
        for i, s in enumerate(slides):
            times = cue_times(words, bounds[i], bounds[i + 1], [it["cue"] for it in s["items"]])
            plan.append({"layout": s["layout"], "heading": s["heading"],
                         "kicker": s["kicker"], "highlight": s["highlight"],
                         "sticker": s["sticker"], "start": bounds[i], "end": bounds[i + 1],
                         "items": [{**it, "at": t,
                                    "picture": (pictures.get(it["object"].strip().lower())
                                                or icon_png(it["icon"], cfg))}
                                   for it, t in zip(s["items"], times)]})
        (job_dir / "slides.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")

        with stage_timer(log, "render", timings):
            track = slide_render.render_track(plan, bounds, w, h, fps,
                                              job_dir / "work" / "track.mp4", cfg)
            music_path, music_attr = None, ""
            if e.get("music", False):
                try:
                    tr = music.pick_track_by_mood(str(e.get("music_mood", "")))
                    music_path, music_attr = music.ensure_track(tr), music.attribution_for(tr)
                except music.MusicError as err:
                    notes.append(f"no background music: {err.message}")
            audio = voice_wav
            if e.get("sfx", True):
                audio = mix_sfx(voice_wav, sfx_cues(plan, bounds),
                                job_dir / "work" / "voice_sfx.wav",
                                float(e.get("sfx_db", 0.0)))
            clip_dir = job_dir / "clip_00"
            final = create.finish_video(track, audio, words, clip_dir, cfg,
                                        captions=bool(e.get("captions", False)),
                                        preset=str(e.get("caption_preset", "clean-minimal")),
                                        anchor=None, music_path=music_path,
                                        music_db=float(e.get("music_volume_db", -24)))
        credits = ("Pictures: AI-generated on our own computer; icons: Microsoft Fluent "
                   "Emoji (MIT) and Google Noto Emoji (Apache 2.0). Narration: AI voice. "
                   "For education only, not financial advice. " + music_attr).strip()
        meta = {"title": script["title"], "description":
                f"{script['description'].strip()}\n\n{credits}",
                "hashtags": script["hashtags"], "format": "long", "made_for_kids": False,
                "create": {"topic": topic, "mode": "explain",
                           "fact_check": script["fact_check"]}}
        job["clips"].append(create._write_clip(clip_dir, 0, final, meta, create._virality(cfg),
                                               "finance", "long", mock=resolved == "mock"))
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
    ap = argparse.ArgumentParser(description="ClipForge Explainer mode: PPT-style money "
                                             "explainers synced to narration")
    ap.add_argument("--topic", required=True)
    ap.add_argument("--slides", type=int, help="slide count (default explain.slides)")
    ap.add_argument("--provider", help="LLM provider override (mock|gemini|...)")
    args = ap.parse_args(argv)
    cfg = load_config()
    verify_ffmpeg(cfg)
    job_dir = make_video(cfg, args.topic, args.slides, args.provider)
    status = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))["status"]
    print(f"{status}: {job_dir}")
    return 0 if status == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
