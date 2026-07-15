"""Avatar Host mode: generated intro/outro scripts, cloned-voice TTS, and the
three-segment composite (frozen intro + avatar -> full-screen clip -> frozen
outro + avatar).

This module owns the whole feature:
  - per-clip intro/outro script generation (LLM via llm.complete_json, with a
    deterministic template fallback — script generation NEVER fails a clip)
  - a specificity gate: every script must share at least one non-filler
    content word with the clip transcript, or it is regenerated/replaced
  - TTS orchestration against tts_worker.py running in the ISOLATED .venv-tts
    (chatterbox-tts pins torch and friends; they must never enter the main
    venv — see avatar.tts.python in config.yaml)
  - the ffmpeg composite that wraps a finished clip in avatar segments

The avatar and the source speaker never overlap by construction: TTS audio
exists only over frozen frames. Everything is gated on `avatar.enabled`
(default false); disabled runs are byte-identical to pre-feature output.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

import llm
from config import ROOT, load_config, save_config
from errors import ClipForgeError, LLMError
from logutil import get_logger
from metadata import FILLER_WORDS
from schemas import SCHEMAS, is_valid, validate

log = get_logger("avatar")

VOICE_DIR = ROOT / "assets" / "user_voice"   # gitignored — never committed
TTS_VENV_DIR = ROOT / ".venv-tts"
WORKER_PATH = ROOT / "tts_worker.py"


class AvatarError(ClipForgeError):
    stage = "avatar"


SCRIPT_PROMPT = """TASK: Write a spoken intro and outro for an avatar host presenting this short clip.
The host speaks BEFORE and AFTER the clip — never over it.
Ground both lines in the transcript: name the actual topic, person, or claim.
Generic filler ("check this out", "you won't believe what happens") is forbidden.
CONSTRAINTS:
- intro: at most {intro_max_words} words. Educational, concise, spoken style.
  Tease the SPECIFIC thing the viewer is about to learn, using a concrete
  detail from the transcript.
- outro: at most {outro_max_words} words. One takeaway from the clip, then a
  soft close.
- No hashtags, no emojis, no quotation marks, plain speakable sentences.
OUTPUT SCHEMA (respond with ONLY this JSON):
{schema}
CLIP HOOK: {hook}
CLIP TRANSCRIPT:
{text}
"""


def _content_words(text: str) -> list[str]:
    """Lowercase words of 4+ letters that aren't conversational filler — the
    vocabulary a script must draw from to count as clip-specific."""
    return [w for w in re.findall(r"[a-z']{4,}", (text or "").lower())
            if w not in FILLER_WORDS]


def script_is_specific(script: str, clip_text: str) -> bool:
    """True when the script shares at least one content word with the clip
    transcript. A transcript with no content words (music, sparse speech)
    can't ground anything, so every script passes vacuously."""
    transcript_words = set(_content_words(clip_text))
    if not transcript_words:
        return True
    return bool(set(_content_words(script)) & transcript_words)


def _cap_words(text: str, max_words: int) -> str:
    """Truncate to max_words, preferring the last sentence boundary inside the
    kept words so the spoken line doesn't stop mid-thought."""
    text = re.sub(r"\s+", " ", (text or "")).strip()
    words = text.split(" ")
    if len(words) <= max_words:
        return text
    kept = " ".join(words[:max_words])
    m = re.match(r"^(.+[.!?])[^.!?]*$", kept)
    if m and len(m.group(1).split(" ")) >= max(2, max_words // 3):
        return m.group(1)
    return kept.rstrip(",;:") + "."


def _template_script(hook: str, clip_text: str) -> dict:
    """Deterministic fallback (LLM failed or stayed generic): grounded in the
    clip's most frequent content words so it always passes the specificity
    gate whenever the transcript has content words at all."""
    top = [w for w, _ in Counter(_content_words(clip_text)).most_common(3)]
    topic = top[0] if top else "this moment"
    second = top[1] if len(top) > 1 else topic
    hook_s = re.sub(r"\s+", " ", (hook or "")).strip().rstrip(".!?")
    if hook_s:
        intro = f"Here is a clip about {topic}: {hook_s[:120]}."
    else:
        intro = f"Here is a clip about {topic}, and it is worth your attention."
    outro = (f"And that is the key point about {topic}. "
             f"Notice how {second} shaped what happened.")
    return {"intro": intro[:220], "outro": outro[:200]}


def generate_script(clip_text: str, hook: str, cfg: dict | None = None,
                    provider: str | None = None) -> dict:
    """Returns {'intro', 'outro'} (schema avatar_script), always valid and
    clip-specific: LLM first, ONE corrective retry naming concrete transcript
    words when the answer is generic, deterministic template fallback on any
    LLM failure. Never raises."""
    cfg = cfg or load_config()
    scfg = cfg.get("avatar", {}).get("script", {})
    intro_max = int(scfg.get("intro_max_words", 30))
    outro_max = int(scfg.get("outro_max_words", 25))
    prompt = SCRIPT_PROMPT.format(
        schema=json.dumps(SCHEMAS["avatar_script"]),
        intro_max_words=intro_max, outro_max_words=outro_max,
        hook=(hook or "")[:200], text=(clip_text or "")[:2000])
    context = {"text": clip_text, "hook": hook}

    data: dict | None = None
    try:
        data = llm.complete_json("avatar_script", "avatar_script", prompt,
                                 provider=provider, context=context, cfg=cfg)
        if not (script_is_specific(data["intro"], clip_text)
                and script_is_specific(data["outro"], clip_text)):
            anchors = list(dict.fromkeys(_content_words(clip_text)))[:5]
            log.warning("avatar script too generic — one corrective retry "
                        "(anchors: %s)", ", ".join(anchors))
            retry_prompt = (prompt + "\n\nIMPORTANT: your previous answer was "
                            "too generic. Both intro and outro MUST mention at "
                            "least one of these words from the transcript: "
                            + ", ".join(anchors))
            data = llm.complete_json("avatar_script", "avatar_script",
                                     retry_prompt, provider=provider,
                                     context=context, cfg=cfg)
    except LLMError as e:
        log.warning("avatar script LLM failed (%s) — template fallback", e)
        data = None

    if data is None or not (script_is_specific(data["intro"], clip_text)
                            and script_is_specific(data["outro"], clip_text)):
        if data is not None:
            log.warning("avatar script still generic after retry — "
                        "template fallback")
        data = _template_script(hook, clip_text)

    result = {"intro": _cap_words(data["intro"], intro_max),
              "outro": _cap_words(data["outro"], outro_max)}
    if not is_valid(result, "avatar_script"):
        # word-capping a pathological answer can undershoot minLength; the
        # template is always schema-valid
        result = _template_script(hook, clip_text)
        result = {"intro": _cap_words(result["intro"], intro_max),
                  "outro": _cap_words(result["outro"], outro_max)}
    validate(result, "avatar_script")
    return result


# ------------------------------------------------------------------- TTS

def _tts_python(cfg: dict) -> Path:
    """Interpreter of the isolated TTS venv (avatar.tts.python), resolved
    against ROOT like config ffmpeg.binary is in ffutil."""
    raw = str(cfg.get("avatar", {}).get("tts", {}).get(
        "python", ".venv-tts/Scripts/python.exe"))
    p = Path(raw)
    return p if p.is_absolute() else ROOT / p


def _resolve_ref_audio(cfg: dict) -> Path:
    ref = str(cfg.get("avatar", {}).get("tts", {}).get("ref_audio", "")).strip()
    if not ref:
        raise AvatarError(
            "avatar.tts.ref_audio is not set — record a 5-10s voice sample "
            "and run: python avatar.py setup-voice <path-to-wav>")
    p = Path(ref)
    p = p if p.is_absolute() else ROOT / p
    if not p.is_file():
        raise AvatarError(f"voice reference not found: {p} — re-run "
                          "`python avatar.py setup-voice <wav>`")
    return p


def synthesize_batch(jobs: list[dict], cfg: dict | None = None) -> list[dict]:
    """Synthesize every job [{'text', 'out_path'}] in ONE tts_worker.py run
    (the model loads once) inside the isolated .venv-tts. Returns the worker's
    [{'out_path', 'duration_s'}] in job order after verifying each wav landed
    on disk. Raises AvatarError on any failure: missing venv/reference,
    nonzero exit, malformed reply, timeout, or a missing/empty output file."""
    cfg = cfg or load_config()
    tts_cfg = cfg.get("avatar", {}).get("tts", {})
    py = _tts_python(cfg)
    if not py.is_file():
        raise AvatarError(
            "TTS venv not found — run `python avatar.py setup-venv` once "
            "(downloads chatterbox-tts, ~2-3 GB)", detail=str(py))
    ref = _resolve_ref_audio(cfg)
    if not jobs:
        return []
    payload = {
        "ref_audio": str(ref),
        "device": str(tts_cfg.get("device", "auto")),
        "jobs": [{"text": str(j["text"]), "out_path": str(j["out_path"])}
                 for j in jobs],
    }
    timeout = float(tts_cfg.get("timeout_s", 1800))
    log.info("tts: synthesizing %d line(s) via %s (device=%s)",
             len(jobs), py, payload["device"])
    try:
        proc = subprocess.run(
            [str(py), str(WORKER_PATH)], input=json.dumps(payload),
            capture_output=True, text=True, encoding="utf-8",
            timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise AvatarError(
            f"TTS worker timed out after {timeout:.0f}s "
            "(raise avatar.tts.timeout_s or set avatar.tts.device: cpu)",
            detail=(e.stderr or "")[-1000:] if isinstance(e.stderr, str)
            else None)
    except OSError as e:
        raise AvatarError(f"could not launch TTS worker: {e}", detail=str(py))

    stderr_tail = (proc.stderr or "")[-1000:]
    lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
    reply = None
    if lines:
        try:
            reply = json.loads(lines[-1])
        except json.JSONDecodeError:
            reply = None
    if reply is None:
        # no parseable protocol line — report by exit code / raw output
        if proc.returncode != 0 or not lines:
            raise AvatarError(f"TTS worker exited {proc.returncode}",
                              detail=stderr_tail)
        raise AvatarError("TTS worker returned malformed JSON",
                          detail=stderr_tail or lines[-1][:500])
    if not reply.get("ok"):
        # worker exits 1 with {"ok": false, "error": ...} — surface its message
        raise AvatarError(f"TTS failed: {reply.get('error', 'unknown error')}",
                          detail=stderr_tail)
    if proc.returncode != 0:
        raise AvatarError(f"TTS worker exited {proc.returncode}",
                          detail=stderr_tail)
    results = reply.get("results", [])
    if len(results) != len(jobs):
        raise AvatarError(
            f"TTS worker returned {len(results)} results for {len(jobs)} jobs",
            detail=stderr_tail)
    for job, res in zip(jobs, results):
        out = Path(str(res.get("out_path", "")))
        if not out.is_file() or out.stat().st_size == 0:
            raise AvatarError(f"TTS output missing or empty: {out}",
                              detail=stderr_tail)
        if float(res.get("duration_s", 0)) <= 0:
            raise AvatarError(f"TTS reported zero duration for {out.name}",
                              detail=stderr_tail)
    return results


# ------------------------------------------------------- stage + composite

def segment_durations(tts_s: float, kind: str, cfg: dict) -> float:
    """Freeze-segment duration for a TTS line: wav length + pad, clamped to
    the configured [min, max] for `kind` ('intro' | 'outro')."""
    t = cfg.get("avatar", {}).get("timing", {})
    pad = float(t.get("pad_s", 0.4))
    lo = float(t.get(f"{kind}_min_s", 4.0 if kind == "intro" else 3.0))
    hi = float(t.get(f"{kind}_max_s", 12.0 if kind == "intro" else 10.0))
    return round(min(max(float(tts_s) + pad, lo), hi), 3)


def prepare_avatar(candidates: list[dict], transcript: dict, job_dir: Path,
                   cfg: dict, provider: str | None = None) -> list[dict]:
    """The `avatar` pipeline stage: per candidate, generate the intro/outro
    scripts, then synthesize EVERY line in one tts_worker.py run (the model
    loads once — never inside the parallel render workers). Returns one
    JSON-serializable item per candidate (the stage marker payload):
      {intro_script, outro_script, intro_wav, outro_wav, intro_s, outro_s}
    Raises AvatarError when TTS fails — an avatar job without a voice is
    wrong content, so the stage fails loudly rather than shipping silent
    intros."""
    avatar_dir = job_dir / "avatar"
    avatar_dir.mkdir(exist_ok=True)
    items: list[dict] = []
    jobs: list[dict] = []
    for i, cand in enumerate(candidates):
        words = [w["word"] for w in transcript["words"]
                 if w["start"] >= cand["start"] - 0.05
                 and w["end"] <= cand["end"] + 0.05]
        clip_text = " ".join(words)
        script = generate_script(clip_text, cand.get("hook", ""), cfg,
                                 provider=provider)
        item = {"intro_script": script["intro"],
                "outro_script": script["outro"],
                "intro_wav": str(avatar_dir / f"clip_{i:02d}_intro.wav"),
                "outro_wav": str(avatar_dir / f"clip_{i:02d}_outro.wav")}
        jobs.append({"text": script["intro"], "out_path": item["intro_wav"]})
        jobs.append({"text": script["outro"], "out_path": item["outro_wav"]})
        items.append(item)
        log.info("clip %02d scripts: intro=%d words, outro=%d words", i,
                 len(script["intro"].split()), len(script["outro"].split()))
    results = synthesize_batch(jobs, cfg)
    for i, item in enumerate(items):
        item["intro_s"] = float(results[2 * i]["duration_s"])
        item["outro_s"] = float(results[2 * i + 1]["duration_s"])

    # Word timings for the avatar karaoke captions: Whisper each short wav
    # HERE (serial stage — the parallel render workers must never load a
    # model). Best-effort: a timing failure only drops captions for that
    # line, never the clip. transcribe() caches per wav hash, so resumes
    # are instant.
    if cfg.get("avatar", {}).get("captions", {}).get("enabled", True):
        import copy
        import transcribe as transcribe_mod
        wcfg = copy.deepcopy(cfg)
        wcfg.setdefault("whisper", {})["model_override"] = str(
            cfg.get("avatar", {}).get("captions", {}).get("whisper_model",
                                                          "small"))
        for i, item in enumerate(items):
            for kind in ("intro", "outro"):
                try:
                    t = transcribe_mod.transcribe(item[f"{kind}_wav"], wcfg)
                    item[f"{kind}_words"] = t["words"]
                except Exception as e:  # noqa: BLE001 — captions are an extra
                    log.warning("clip %02d %s caption timing failed (%s) — "
                                "avatar captions skipped for that line",
                                i, kind, e)
    return items


def build_composite_graph(w: int, h: int, layout: dict, intro_dur: float,
                          outro_dur: float, fps: float,
                          ass_path: Path | None = None,
                          fontsdir: Path | None = None) -> str:
    """Pure filter_complex builder for the 3-segment composite. Input order is
    fixed: [0] finished clip, [1] first-frame PNG (looped), [2] last-frame PNG
    (looped), [3] avatar PNG, [4] intro TTS wav, [5] outro TTS wav. Emits
    [vout] + [acat]. TTS longer than its segment is trimmed with a short fade;
    shorter is padded with silence, so A/V stays in sync by construction."""
    cw = int(w * float(layout.get("clip_scale", 0.62))) // 2 * 2
    aw = int(w * float(layout.get("avatar_scale", 0.42))) // 2 * 2
    cy = int(h * float(layout.get("clip_y", 0.07)))
    m = int(layout.get("margin_px", 48))
    canvas = str(layout.get("canvas_color", "0x101014"))
    left, right = f"{m}", f"W-w-{m}"
    ix = left if layout.get("intro_side", "left") != "right" else right
    ox = right if layout.get("outro_side", "right") != "left" else left

    def _seg(tag: str, src: int, av: str, x: str, dur: float) -> list[str]:
        fade = max(0.0, dur - 0.15)
        return [
            f"color=c={canvas}:s={w}x{h}:d={dur:.3f}:r={fps:.3f}[{tag}bg]",
            f"[{src}:v]scale={cw}:-2,setsar=1[{tag}fr]",
            f"[{tag}bg][{tag}fr]overlay=(W-w)/2:{cy}[{tag}b1]",
            f"[{tag}b1][{av}]overlay={x}:H-h-{m}[{tag}b2]",
            f"[{tag}b2]fps={fps:.3f},format=yuv420p[{tag}v]",
            f"[{4 if tag == 'intro' else 5}:a]aresample=44100,"
            "aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"atrim=0:{dur:.3f},afade=t=out:st={fade:.3f}:d=0.15,"
            f"apad=whole_dur={dur:.3f}[{tag}a]",
        ]

    parts = [f"[3:v]format=rgba,scale={aw}:-1,split[avI][avO]"]
    parts += _seg("intro", 1, "avI", ix, intro_dur)
    parts += _seg("outro", 2, "avO", ox, outro_dur)
    parts += [
        f"[0:v]setsar=1,fps={fps:.3f},format=yuv420p[mainv]",
        "[0:a]aresample=44100,"
        "aformat=sample_fmts=fltp:channel_layouts=stereo[maina]",
        "[introv][introa][mainv][maina][outrov][outroa]"
        "concat=n=3:v=1:a=1[vcat][acat]",
    ]
    if ass_path is not None:
        import ffutil
        sub = f"subtitles=filename='{ffutil.filter_path(ass_path)}'"
        if fontsdir is not None:
            sub += f":fontsdir='{ffutil.filter_path(fontsdir)}'"
        parts.append(f"[vcat]{sub}[vout]")
    else:
        parts.append("[vcat]null[vout]")
    return ";".join(parts)


def write_avatar_ass(intro_words: list[dict], outro_words: list[dict],
                     ass_path: Path, cfg: dict, intro_dur: float,
                     outro_dur: float, main_dur: float,
                     play_w: int = 1080, play_h: int = 1920) -> None:
    """Karaoke ASS for the avatar's speech, in COMPOSITE time: intro events
    start at 0, outro events are offset by intro_dur + main_dur. One Avatar
    style derived from the active caption preset (smaller, positioned in the
    avatar zone via avatar.captions.*_anchor — deliberately outside the main
    captions' 0.52-0.66 law; the frozen background guarantees clean space)."""
    import captions
    ccfg = cfg["captions"]
    preset = ccfg["presets"][ccfg["preset"]]
    family, bold = captions._font(preset["font"])
    size = max(36, int(preset["font_size"] * 0.7))
    acfg = cfg.get("avatar", {}).get("captions", {})
    scale = int(preset.get("highlight_scale", 100))
    max_words = int(ccfg["max_words_per_line"])
    cx = play_w // 2

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {play_w}
PlayResY: {play_h}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Avatar,{family},{size},{preset['primary_color']},{preset['highlight_color']},{preset['outline_color']},&H80000000,{bold},0,0,0,100,100,0,0,1,{preset['outline']},{preset['shadow']},5,90,90,0,1
"""
    header += ("\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, "
               "MarginR, MarginV, Effect, Text\n")

    def _events(words, offset, limit, anchor):
        pos = rf"{{\an5\pos({cx},{int(float(anchor) * play_h)})}}"
        kept = [w for w in words if w["start"] < limit - 0.05]
        out = []
        for line in captions.build_caption_lines(kept, max_words):
            tokens = [captions._esc(w["word"]) for w in line["words"]]
            for k, w in enumerate(line["words"]):
                start = line["start"] if k == 0 else w["start"]
                end = (line["words"][k + 1]["start"]
                       if k + 1 < len(line["words"]) else line["end"])
                end = min(end, limit)
                if end - start < 0.01:
                    continue
                parts = []
                for j, tok in enumerate(tokens):
                    if j != k:
                        parts.append(tok)
                    else:
                        parts.append(r"{\c" + preset["highlight_color"] + "&"
                                     + rf"\fscx{scale}\fscy{scale}" + "}"
                                     + tok + r"{\r}")
                out.append((offset + start, offset + end,
                            pos + " ".join(parts)))
        return out

    events = _events(intro_words or [], 0.0, intro_dur,
                     acfg.get("intro_anchor", 0.80))
    events += _events(outro_words or [], intro_dur + main_dur, outro_dur,
                      acfg.get("outro_anchor", 0.80))
    with open(ass_path, "w", encoding="utf-8-sig") as f:
        f.write(header)
        for start, end, text in events:
            f.write(f"Dialogue: 0,{captions._ts(start)},{captions._ts(end)},"
                    f"Avatar,,0,0,0,,{text}\n")


def _resolve_avatar_image(cfg: dict) -> Path:
    img = str(cfg.get("avatar", {}).get("image", "")).strip()
    if not img:
        raise AvatarError("avatar.image is not set — point it at an avatar "
                          "PNG (alpha), e.g. assets/user_branding/avatar.png")
    p = Path(img)
    p = p if p.is_absolute() else ROOT / p
    if not p.is_file():
        raise AvatarError(f"avatar image not found: {p}")
    return p


def apply_avatar(final: Path, clip_dir: Path, item: dict,
                 cfg: dict) -> dict:
    """Wrap the finished clip in the avatar intro/outro segments (in place:
    `final` is replaced). Freeze frames come from the pre-caption reframed.mp4
    when present so no burned caption fragment freezes on screen. Raises
    AvatarError on any failure — an avatar clip without its avatar is wrong,
    so this is NOT best-effort like bumpers."""
    import ffutil
    final = Path(final)
    avatar_png = _resolve_avatar_image(cfg)
    info = ffutil.probe(final)
    if not info["has_audio"]:
        raise AvatarError(f"{final.name} has no audio stream")
    w, h, fps, main_dur = (info["width"], info["height"], info["fps"],
                           info["duration"])
    intro_dur = segment_durations(item["intro_s"], "intro", cfg)
    outro_dur = segment_durations(item["outro_s"], "outro", cfg)

    # freeze frames from the pre-caption render when available (its duration
    # can differ from final's once bumpers are appended — use its own probe)
    freeze_src = clip_dir / "reframed.mp4"
    if not freeze_src.is_file():
        freeze_src = final
    src_dur = ffutil.probe(freeze_src)["duration"]
    first_png = clip_dir / "avatar_first.png"
    last_png = clip_dir / "avatar_last.png"
    ffutil.run_ffmpeg(["-i", freeze_src, "-frames:v", "1", "-update", "1",
                       first_png])
    ffutil.run_ffmpeg(["-ss", f"{max(0.0, src_dur - 0.15):.3f}",
                       "-i", freeze_src, "-frames:v", "1", "-update", "1",
                       last_png])

    # avatar karaoke captions (intro/outro only): burned by the composite pass
    ass_path, fontsdir = None, None
    if cfg.get("avatar", {}).get("captions", {}).get("enabled", True) and \
            (item.get("intro_words") or item.get("outro_words")):
        import fontreg
        ass_path = clip_dir / "avatar.ass"
        write_avatar_ass(item.get("intro_words") or [],
                         item.get("outro_words") or [],
                         ass_path, cfg, intro_dur, outro_dur, main_dur,
                         play_w=w, play_h=h)
        fontsdir = fontreg.fonts_dir(cfg)

    layout = cfg.get("avatar", {}).get("layout", {})
    graph = build_composite_graph(w, h, layout, intro_dur, outro_dur, fps,
                                  ass_path=ass_path, fontsdir=fontsdir)
    out = clip_dir / "final_avatar.mp4"
    args = ["-i", final,
            "-loop", "1", "-t", f"{intro_dur:.3f}", "-i", first_png,
            "-loop", "1", "-t", f"{outro_dur:.3f}", "-i", last_png,
            "-i", avatar_png,
            "-i", item["intro_wav"], "-i", item["outro_wav"],
            "-filter_complex", graph, "-map", "[vout]", "-map", "[acat]"]
    args += ffutil.video_encode_args(cfg, final=True)
    args += ["-c:a", "aac", "-b:a", cfg["render"]["audio_bitrate"],
             "-movflags", "+faststart", out]
    ffutil.run_ffmpeg(args, progress_label=f"avatar {clip_dir.name}")

    total = intro_dur + main_dur + outro_dur
    got = ffutil.probe(out)["duration"]
    if abs(got - total) > 1.5:   # same tolerance as cut.cut_segments
        raise AvatarError(
            f"avatar composite duration {got:.2f}s != expected {total:.2f}s")
    try:
        out.replace(final)
    except OSError as e:
        raise AvatarError(f"could not replace {final.name}: {e}")
    log.info("%s: avatar composite applied (intro %.1fs + clip %.1fs + "
             "outro %.1fs)", clip_dir.name, intro_dur, main_dur, outro_dur)
    return {"intro_s": intro_dur, "outro_s": outro_dur,
            "intro_script": item["intro_script"],
            "outro_script": item["outro_script"]}


# ---------------------------------------------------------- one-time setup

def _run_step(args: list, label: str) -> None:
    """Run one setup command with inherited stdio (so pip progress is visible)
    and fail loudly with the command on a nonzero exit."""
    log.info("%s: %s", label, " ".join(str(a) for a in args))
    try:
        proc = subprocess.run([str(a) for a in args])
    except OSError as e:
        raise AvatarError(f"{label} failed to launch: {e}",
                          detail=" ".join(str(a) for a in args))
    if proc.returncode != 0:
        raise AvatarError(f"{label} exited {proc.returncode}",
                          detail=" ".join(str(a) for a in args))


def setup_venv() -> None:
    """Create .venv-tts (Python 3.11, matching chatterbox-tts) and install
    chatterbox-tts into it. Idempotent: re-running upgrades in place."""
    exe = TTS_VENV_DIR / "Scripts" / "python.exe"
    print("Setting up the isolated TTS venv (.venv-tts).")
    print("This downloads chatterbox-tts and its torch stack (~2-3 GB) — "
          "one time only.")
    if not exe.is_file():
        launcher = shutil.which("py")
        if launcher:
            _run_step([launcher, "-3.11", "-m", "venv", str(TTS_VENV_DIR)],
                      "create venv")
        else:
            # no py launcher: the running interpreter is already 3.11
            # (config.check_python_version enforces it at startup)
            _run_step([sys.executable, "-m", "venv", str(TTS_VENV_DIR)],
                      "create venv")
    _run_step([exe, "-m", "pip", "install", "--upgrade", "pip"], "upgrade pip")
    _run_step([exe, "-m", "pip", "install", "chatterbox-tts"],
              "install chatterbox-tts")
    print(f"TTS venv ready: {exe}")
    print("Next: python avatar.py setup-voice <path-to-your-voice.wav>")


def setup_voice(src: str) -> Path:
    """Validate and install the user's voice reference (5-10s of clean speech
    is ideal; 3-30s accepted), then persist its path to config.local.yaml."""
    import ffutil
    src_path = Path(src).expanduser()
    if not src_path.is_file():
        raise AvatarError(f"voice file not found: {src_path}")
    info = ffutil.probe(src_path)
    if not info.get("has_audio"):
        raise AvatarError(f"no audio stream in {src_path.name}")
    dur = float(info.get("duration", 0))
    if not 3.0 <= dur <= 30.0:
        raise AvatarError(
            f"voice reference is {dur:.1f}s — record 3-30s of clean speech "
            "(5-10s is ideal)")
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    dest = VOICE_DIR / f"ref{src_path.suffix.lower()}"
    try:
        shutil.copy2(src_path, dest)
    except OSError as e:
        raise AvatarError(f"could not copy voice reference to {dest}: {e}")
    rel = dest.relative_to(ROOT).as_posix()
    save_config({"avatar": {"tts": {"ref_audio": rel}}})
    log.info("voice reference installed: %s (%.1fs)", rel, dur)
    print(f"Voice reference saved to {rel} ({dur:.1f}s) and set in "
          "config.local.yaml.")
    print('Next: python avatar.py say "This is my cloned voice test."')
    return dest


def _cmd_say(text: str, out: str | None = None) -> Path:
    """Smoke-test the whole TTS path: one line to a wav, prints the duration."""
    cfg = load_config()
    out_path = Path(out) if out else ROOT / "output" / "_smoke_avatar_tts.wav"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    res = synthesize_batch([{"text": text, "out_path": str(out_path)}], cfg)
    print(f"wrote {res[0]['out_path']} ({res[0]['duration_s']:.1f}s)")
    return out_path


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="Avatar Host setup & smoke tools (script gen + TTS)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup-venv",
                   help="create .venv-tts and install chatterbox-tts (~2-3GB)")
    p_voice = sub.add_parser("setup-voice",
                             help="install a voice reference wav (3-30s)")
    p_voice.add_argument("wav", help="path to your recorded voice sample")
    p_say = sub.add_parser("say", help="synthesize one line (smoke test)")
    p_say.add_argument("text")
    p_say.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "setup-venv":
            setup_venv()
        elif a.cmd == "setup-voice":
            setup_voice(a.wav)
        elif a.cmd == "say":
            _cmd_say(a.text, a.out)
    except ClipForgeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
