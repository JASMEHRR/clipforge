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
import time
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


# ---------------------------------------------------- render timing history
# Single-clip UI renders don't go through history.db, so per-stage wall-clock
# timings for the Avatar Host tab's ETA live in their own small JSON file,
# keyed by (engine, stage, audio-second bucket). Seeded with hand-measured
# guesses until a real render lands on this machine — see estimate below.
AVATAR_TIMINGS_PATH = ROOT / "cache" / "avatar_timings.json"
_TIMINGS_KEEP = 8   # last N durations per (engine, stage, bucket)

# Rough seeds (seconds) until a real timing exists on THIS hardware. Static
# tts/composite get measured and replaced within one render; the animated
# lip-sync numbers are hand-measured guesses from config.yaml comments
# (LivePortrait ~9.5min/seg on a GTX1650; MuseTalk ~8min cold load + inference,
# ~10-14min/segment). They only prime the countdown before the first render.
_TIMING_SEED = {
    "static":       {"tts": 4.0, "composite": 25.0},
    "liveportrait": {"tts": 4.0, "lipsync_intro": 570.0,
                     "lipsync_outro": 570.0, "composite": 30.0},
    "musetalk":     {"tts": 4.0, "lipsync_intro": 480.0,
                     "lipsync_outro": 480.0, "composite": 30.0},
}
# Ordered stages shown in the UI per engine. ponytail: caption timing and
# freeze-frame extraction are sub-second — folded into composite, not tracked.
_TIMING_STAGES = {
    "static":       ["tts", "composite"],
    "liveportrait": ["tts", "lipsync_intro", "lipsync_outro", "composite"],
    "musetalk":     ["tts", "lipsync_intro", "lipsync_outro", "composite"],
}
_STAGE_LABELS = {
    "tts": "Voice synthesis", "lipsync_intro": "Lip-sync (intro)",
    "lipsync_outro": "Lip-sync (outro)", "composite": "Compositing video",
}


WORDS_PER_SECOND = 2.5   # rough TTS pace; matches the frontend heuristic


def estimate_audio_seconds(*texts: str) -> float:
    """Rough spoken length of the scripts, used to bucket ETA history. Shared
    by the render-estimate endpoint, the render hint, and the timing persist so
    all three land in the same bucket."""
    words = sum(len((t or "").split()) for t in texts)
    return words / WORDS_PER_SECOND


def avatar_engine_key(cfg: dict) -> str:
    """Which timing bucket a render falls into: 'static' (PNG overlay),
    'liveportrait' (motion only), or 'musetalk' (motion + lip-sync)."""
    anim = cfg.get("avatar", {}).get("animation", {})
    if not anim.get("enabled"):
        return "static"
    if anim.get("lip_sync", {}).get("enabled"):
        return "musetalk"
    return "liveportrait"


def _audio_bucket(audio_s: float) -> str:
    return str(int(round(max(0.0, float(audio_s)) / 2.0) * 2))


def read_avatar_timings() -> dict:
    try:
        return json.loads(AVATAR_TIMINGS_PATH.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def append_avatar_timing(engine: str, stage: str, audio_s: float,
                         seconds: float) -> None:
    """Record one measured stage duration. Best-effort — never raises into a
    render. Keeps the last _TIMINGS_KEEP per (engine, stage, audio bucket)."""
    if not seconds or seconds <= 0:
        return
    try:
        data = read_avatar_timings()
        bucket = _audio_bucket(audio_s)
        cell = data.setdefault(engine, {}).setdefault(stage, {}) \
                   .setdefault(bucket, [])
        cell.append(round(float(seconds), 2))
        del cell[:-_TIMINGS_KEEP]
        AVATAR_TIMINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = AVATAR_TIMINGS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data), "utf-8")
        tmp.replace(AVATAR_TIMINGS_PATH)
    except OSError as e:  # noqa: BLE001 — timing is a nicety, never fatal
        log.warning("could not persist avatar timing (%s) — ETA unaffected", e)


def estimate_avatar_stages(engine: str, audio_s: float) -> dict:
    """Ordered per-stage ETA for `engine` at this TTS audio length. EMA of past
    real timings where available, else a seed. `has_history` is False until at
    least one real timing exists (the UI shows a 'rough' note in that case)."""
    import progress
    engine = engine if engine in _TIMING_STAGES else "static"
    data = read_avatar_timings()
    bucket = _audio_bucket(audio_s)
    seed = _TIMING_SEED[engine]
    stages, has_history = [], False
    for key in _TIMING_STAGES[engine]:
        durations = data.get(engine, {}).get(key, {}).get(bucket)
        est = progress.ema(durations) if durations else None
        if est is not None:
            has_history = True
        else:
            est = float(seed.get(key, 10.0))
        stages.append({"key": key, "name": _STAGE_LABELS.get(key, key),
                       "est_s": round(est, 1)})
    return {"stages": stages,
            "total_s": round(sum(s["est_s"] for s in stages), 1),
            "has_history": has_history}


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


def _kokoro_synth_batch(jobs: list[dict], cfg: dict) -> list[dict]:
    """Synthesize every job in-process via kokoro-onnx (avatar.tts.engine:
    kokoro) — no isolated venv, unlike Chatterbox: kokoro-onnx is
    onnxruntime-based and has no torch pin to conflict with the main venv.
    Raises AvatarError on missing model files, missing package, or a
    per-job synth failure."""
    kcfg = cfg.get("avatar", {}).get("tts", {}).get("kokoro", {})
    model_path = ROOT / str(kcfg.get("model_path", "kokoro-v1.0.onnx"))
    voices_path = ROOT / str(kcfg.get("voices_path", "voices-v1.0.bin"))
    voice = str(kcfg.get("voice", "af_nicole"))
    speed = float(kcfg.get("speed", 1.0))
    lang = str(kcfg.get("lang", "en-us"))
    if not model_path.is_file():
        raise AvatarError(f"kokoro model not found: {model_path} — set "
                          "avatar.tts.kokoro.model_path")
    if not voices_path.is_file():
        raise AvatarError(f"kokoro voices file not found: {voices_path} — "
                          "set avatar.tts.kokoro.voices_path")
    try:
        from kokoro_onnx import Kokoro
        import soundfile as sf
    except ImportError as e:
        raise AvatarError(f"kokoro-onnx not installed: {e} — "
                          "pip install kokoro-onnx")

    log.info("tts: synthesizing %d line(s) via kokoro-onnx (voice=%s)",
             len(jobs), voice)
    kokoro = Kokoro(str(model_path), str(voices_path))
    results = []
    for job in jobs:
        out_path = Path(str(job["out_path"]))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            samples, sr = kokoro.create(str(job["text"]), voice=voice,
                                        speed=speed, lang=lang)
        except Exception as e:
            raise AvatarError(f"kokoro synthesis failed: {e}")
        sf.write(str(out_path), samples, sr)
        if not out_path.is_file() or out_path.stat().st_size == 0:
            raise AvatarError(f"kokoro output missing or empty: {out_path}")
        duration_s = len(samples) / float(sr)
        if duration_s <= 0:
            raise AvatarError(f"kokoro reported zero duration for {out_path.name}")
        results.append({"out_path": str(out_path), "duration_s": duration_s})
    return results


def _edge_synth_batch(jobs: list[dict], cfg: dict) -> list[dict]:
    """Synthesize every job in-process via edge-tts (avatar.tts.engine: edge) —
    Microsoft's free online neural voices (no API key, includes Hindi/Hinglish),
    no torch pin, no local model. Needs internet. Output is transcoded to a real
    PCM wav so downstream (Whisper timing + ffmpeg composite) is unaffected by
    the mp3 edge-tts emits. Raises AvatarError on missing package or synth
    failure (an offline machine surfaces here with a clear message)."""
    import asyncio

    import ffutil
    ecfg = cfg.get("avatar", {}).get("tts", {}).get("edge", {})
    voice = str(ecfg.get("voice", "en-US-AriaNeural"))
    rate = str(ecfg.get("rate", "+0%"))
    try:
        import edge_tts
    except ImportError as e:
        raise AvatarError(f"edge-tts not installed: {e} — pip install edge-tts")

    log.info("tts: synthesizing %d line(s) via edge-tts (voice=%s)",
             len(jobs), voice)
    results = []
    for job in jobs:
        out_path = Path(str(job["out_path"]))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        text = str(job["text"]).strip()
        if not text:
            raise AvatarError("empty TTS text")
        mp3 = out_path.with_name(out_path.stem + ".edge.mp3")

        async def _run(t=text, m=str(mp3)):
            await edge_tts.Communicate(t, voice, rate=rate).save(m)
        try:
            asyncio.run(_run())
        except Exception as e:  # noqa: BLE001 — network/synth failures → friendly
            raise AvatarError(f"edge-tts synthesis failed (need internet?): {e}")
        if not mp3.is_file() or mp3.stat().st_size == 0:
            raise AvatarError("edge-tts produced no audio — check your internet "
                              "connection")
        # transcode mp3 → wav (24kHz mono) so the pipeline gets a real PCM wav.
        # -f wav forces the container: out_path may be a .tmp (preview writes to
        # a temp name then atomic-renames), which ffmpeg can't infer a format from.
        ffutil.run_ffmpeg(["-i", str(mp3), "-ar", "24000", "-ac", "1",
                           "-f", "wav", str(out_path)])
        mp3.unlink(missing_ok=True)
        if not out_path.is_file() or out_path.stat().st_size == 0:
            raise AvatarError(f"edge-tts output missing or empty: {out_path}")
        duration_s = float(ffutil.probe_audio(out_path)["duration"])
        if duration_s <= 0:
            raise AvatarError(f"edge-tts reported zero duration for {out_path.name}")
        results.append({"out_path": str(out_path), "duration_s": duration_s})
    return results


def synthesize_batch(jobs: list[dict], cfg: dict | None = None,
                     tracker=None) -> list[dict]:
    """Synthesize every job [{'text', 'out_path'}] in ONE tts_worker.py run
    (the model loads once) inside the isolated .venv-tts. Returns the worker's
    [{'out_path', 'duration_s'}] in job order after verifying each wav landed
    on disk. Raises AvatarError on any failure: missing venv/reference,
    nonzero exit, malformed reply, timeout, or a missing/empty output file."""
    cfg = cfg or load_config()
    tts_cfg = cfg.get("avatar", {}).get("tts", {})
    if not jobs:
        return []
    if tracker:
        tracker.item("avatar", "voice synthesis (TTS)", 0.0)
    engine = str(tts_cfg.get("engine", "chatterbox"))
    if engine in ("kokoro", "edge"):
        result = (_edge_synth_batch(jobs, cfg) if engine == "edge"
                  else _kokoro_synth_batch(jobs, cfg))
        if tracker:
            tracker.item("avatar", "voice synthesis (TTS)", 1.0)
        return result
    py = _tts_python(cfg)
    if not py.is_file():
        raise AvatarError(
            "TTS venv not found — run `python avatar.py setup-venv` once "
            "(downloads chatterbox-tts, ~2-3 GB)", detail=str(py))
    ref = _resolve_ref_audio(cfg)
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
    if tracker:
        tracker.item("avatar", "voice synthesis (TTS)", 1.0)
    return results


def generate_script_for_clip(job_dir: Path, clip_index: int, cfg: dict,
                             provider: str | None = None) -> dict:
    """generate_script() for one already-rendered clip, looking up its
    transcript span the same way rerender.regenerate_metadata does — for the
    Avatar Host UI tab's script preview (no TTS/render side-effects)."""
    import rerender
    job = rerender.load_job(job_dir)
    clip = next((c for c in job["clips"] if c["index"] == clip_index), None)
    if clip is None:
        raise AvatarError(f"no clip index {clip_index} in {job_dir}")
    transcript = rerender._load_marker(job_dir, "transcribe")
    clip_text = " ".join(w["word"] for w in transcript["words"]
                         if w["start"] >= clip["start"] - 0.05
                         and w["end"] <= clip["end"] + 0.05)
    script = generate_script(clip_text, clip.get("hook", ""), cfg,
                             provider=provider)
    return {**script, "transcript": clip_text}


def apply_avatar_to_clip(job_dir: Path, clip_index: int, cfg: dict,
                         intro_script: str, outro_script: str,
                         tracker=None) -> dict:
    """Single-clip version of prepare_avatar + apply_avatar, for the Avatar
    Host UI tab: takes already-generated/edited scripts, synthesizes TTS for
    just this clip, and composites them onto an ALREADY-RENDERED clip's
    final.mp4 in place — no whole-job candidate scan, no full pipeline run."""
    import rerender
    job = rerender.load_job(job_dir)
    clip = next((c for c in job["clips"] if c["index"] == clip_index), None)
    if clip is None:
        raise AvatarError(f"no clip index {clip_index} in {job_dir}")
    clip_dir = job_dir / f"clip_{clip_index:02d}"
    final = clip_dir / "final.mp4"
    if not final.is_file():
        raise AvatarError(
            f"clip {clip_index} has no final.mp4 to apply avatar to")

    avatar_dir = job_dir / "avatar"
    avatar_dir.mkdir(exist_ok=True)
    item = {"intro_script": intro_script, "outro_script": outro_script,
            "intro_wav": str(avatar_dir / f"clip_{clip_index:02d}_intro_ui.wav"),
            "outro_wav": str(avatar_dir / f"clip_{clip_index:02d}_outro_ui.wav")}
    _tts_t = time.perf_counter()
    results = synthesize_batch(
        [{"text": intro_script, "out_path": item["intro_wav"]},
         {"text": outro_script, "out_path": item["outro_wav"]}], cfg,
        tracker=tracker)
    tts_wall = time.perf_counter() - _tts_t
    item["intro_s"] = float(results[0]["duration_s"])
    item["outro_s"] = float(results[1]["duration_s"])

    if cfg.get("avatar", {}).get("captions", {}).get("enabled", True):
        import copy
        import transcribe as transcribe_mod
        if tracker:
            tracker.item("avatar", "caption timing", 0.0)
        wcfg = copy.deepcopy(cfg)
        wcfg.setdefault("whisper", {})["model_override"] = str(
            cfg.get("avatar", {}).get("captions", {}).get(
                "whisper_model", "small"))
        for kind in ("intro", "outro"):
            try:
                t = transcribe_mod.transcribe(item[f"{kind}_wav"], wcfg)
                item[f"{kind}_words"] = t["words"]
            except Exception as e:  # noqa: BLE001 — captions are an extra
                log.warning("clip %02d %s caption timing failed (%s) — "
                            "avatar captions skipped for that line",
                            clip_index, kind, e)
        if tracker:
            tracker.item("avatar", "caption timing", 1.0)

    preset_name = job.get("settings", {}).get("preset") or \
        cfg["captions"]["preset"]
    avatar_meta = apply_avatar(final, clip_dir, item, cfg,
                               preset_name=preset_name, tracker=tracker)

    # persist per-stage wall-clock for the UI ETA (best-effort). Bucket by the
    # SAME word-count estimate the render-estimate endpoint queries with (not
    # the actual TTS seconds) so a repeat of the same clip reuses its own
    # history. ponytail: audio-word bucket is a coarse proxy — composite time
    # tracks clip length; key on that if ETA accuracy ever matters.
    engine = avatar_engine_key(cfg)
    audio_s = estimate_audio_seconds(intro_script, outro_script)
    append_avatar_timing(engine, "tts", audio_s, tts_wall)
    for stage_key, secs in (avatar_meta.get("stage_timings") or {}).items():
        append_avatar_timing(engine, stage_key, audio_s, secs)

    clip["avatar"] = avatar_meta
    rerender._save_job(job_dir, job)
    return avatar_meta


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
                          fontsdir: Path | None = None,
                          animated: bool = False) -> str:
    """Pure filter_complex builder for the 3-segment composite. Input order is
    fixed: [0] finished clip, [1] first-frame PNG (looped), [2] last-frame PNG
    (looped), [3] avatar PNG, [4] intro TTS wav, [5] outro TTS wav, and when
    `animated` two more: [6] intro avatar video (alpha), [7] outro avatar
    video (alpha). Emits [vout] + [acat]. TTS longer than its segment is
    trimmed with a short fade; shorter is padded with silence, so A/V stays in
    sync by construction."""
    aw = int(w * float(layout.get("avatar_scale", 0.42))) // 2 * 2
    m = int(layout.get("margin_px", 48))
    left, right = f"{m}", f"W-w-{m}"
    x = left if layout.get("side", "left") != "right" else right

    def _seg(tag: str, src: int, av: str, dur: float) -> list[str]:
        # Frozen frame fills the full clip frame (no shrink/letterbox) —
        # avatar overlays directly on top of it, same side both segments.
        fade = max(0.0, dur - 0.15)
        return [
            f"[{src}:v]scale={w}:{h},setsar=1[{tag}fr]",
            f"[{tag}fr][{av}]overlay={x}:H-h-{m}[{tag}b1]",
            f"[{tag}b1]fps={fps:.3f},format=yuv420p[{tag}v]",
            f"[{4 if tag == 'intro' else 5}:a]aresample=44100,"
            "aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"atrim=0:{dur:.3f},afade=t=out:st={fade:.3f}:d=0.15,"
            f"apad=whole_dur={dur:.3f}[{tag}a]",
        ]

    if animated:
        parts = [f"[6:v]format=rgba,scale={aw}:-1[avI]",
                f"[7:v]format=rgba,scale={aw}:-1[avO]"]
    else:
        parts = [f"[3:v]format=rgba,scale={aw}:-1,split[avI][avO]"]
    parts += _seg("intro", 1, "avI", intro_dur)
    parts += _seg("outro", 2, "avO", outro_dur)
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
                     play_w: int = 1080, play_h: int = 1920,
                     preset_name: str | None = None) -> None:
    """Karaoke ASS for the avatar's speech, in COMPOSITE time: intro events
    start at 0, outro events are offset by intro_dur + main_dur. One Avatar
    style derived from the active caption preset (smaller, positioned in the
    avatar zone via avatar.captions.*_anchor — deliberately outside the main
    captions' 0.52-0.66 law; the frozen background guarantees clean space)."""
    import captions
    ccfg = cfg["captions"]
    preset = ccfg["presets"][preset_name or ccfg["preset"]]
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


def validate_avatar_image_alpha(p: Path) -> None:
    """Raises AvatarError unless `p` is a real image with actual (non-fully-
    opaque) transparency — shared by the render-time resolver below and the
    upload endpoint, so a bad avatar PNG is rejected at upload time too."""
    from PIL import Image
    with Image.open(p) as im:
        if im.mode not in ("RGBA", "LA"):
            raise AvatarError(
                f"avatar image {p} has no alpha channel (mode={im.mode}) — "
                "export a PNG with real transparency, not an opaque one")
        alpha = im.convert("RGBA").getchannel("A")
        if alpha.getextrema()[0] == 255:
            raise AvatarError(
                f"avatar image {p} has an alpha channel but every pixel is "
                "fully opaque — export it with the background actually "
                "made transparent")


def _resolve_avatar_image(cfg: dict) -> Path:
    img = str(cfg.get("avatar", {}).get("image", "")).strip()
    if not img:
        raise AvatarError("avatar.image is not set — point it at an avatar "
                          "PNG (alpha), e.g. assets/user_branding/avatar.png")
    p = Path(img)
    p = p if p.is_absolute() else ROOT / p
    if not p.is_file():
        raise AvatarError(f"avatar image not found: {p}")
    validate_avatar_image_alpha(p)
    return p


def apply_avatar(final: Path, clip_dir: Path, item: dict, cfg: dict,
                 preset_name: str | None = None, tracker=None) -> dict:
    """Wrap the finished clip in the avatar intro/outro segments (in place:
    `final` is replaced). Freeze frames come from the pre-caption reframed.mp4
    when present so no burned caption fragment freezes on screen. Raises
    AvatarError on any failure — an avatar clip without its avatar is wrong,
    so this is NOT best-effort like bumpers."""
    import ffutil
    final = Path(final)
    st_times: dict[str, float] = {}   # per-stage wall-clock, for the UI ETA
    avatar_png = _resolve_avatar_image(cfg)
    acfg_trace = cfg.get("avatar", {})
    anim_trace = acfg_trace.get("animation", {})
    log.info("%s: avatar config (merged) — enabled=%s image=%s "
             "animation.enabled=%s animation.engine=%s "
             "animation.driving_video=%s", clip_dir.name,
             acfg_trace.get("enabled"), avatar_png,
             anim_trace.get("enabled"), anim_trace.get("engine"),
             anim_trace.get("driving_video"))
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
    if tracker:
        tracker.item("avatar", "freeze frames", 1.0)

    # avatar karaoke captions (intro/outro only): burned by the composite pass
    ass_path, fontsdir = None, None
    if cfg.get("avatar", {}).get("captions", {}).get("enabled", True) and \
            (item.get("intro_words") or item.get("outro_words")):
        import fontreg
        ass_path = clip_dir / "avatar.ass"
        write_avatar_ass(item.get("intro_words") or [],
                         item.get("outro_words") or [],
                         ass_path, cfg, intro_dur, outro_dur, main_dur,
                         play_w=w, play_h=h, preset_name=preset_name)
        fontsdir = fontreg.fonts_dir(cfg)

    # Animated avatar (LivePortrait) is opt-in and NOT best-effort like the
    # static path — a failure here either falls back to the static PNG (with
    # a clear log line) or propagates, per avatar.animation.fallback_to_static.
    # Never a silent fallback.
    anim_cfg = cfg.get("avatar", {}).get("animation", {})
    animated = bool(anim_cfg.get("enabled", False))
    log.info("%s: avatar.animation.enabled=%s (raw config value; "
             "animated branch %s)", clip_dir.name, animated,
             "will run" if animated else "SKIPPED — static PNG path used")
    anim_intro_path = anim_outro_path = None
    if animated:
        import avatar_anim
        try:
            renderer = avatar_anim.AnimatedAvatarRenderer(cfg)
            if tracker:
                tracker.item("avatar", "lip-sync (intro)", 0.0)
            _t = time.perf_counter()
            anim_intro_path = renderer.render_intro(item, clip_dir,
                                                     avatar_png, intro_dur)
            st_times["lipsync_intro"] = time.perf_counter() - _t
            if tracker:
                tracker.item("avatar", "lip-sync (intro)", 1.0)
                tracker.item("avatar", "lip-sync (outro)", 0.0)
            _t = time.perf_counter()
            anim_outro_path = renderer.render_outro(item, clip_dir,
                                                     avatar_png, outro_dur)
            st_times["lipsync_outro"] = time.perf_counter() - _t
            if tracker:
                tracker.item("avatar", "lip-sync (outro)", 1.0)
        except AvatarError as e:
            import traceback
            if anim_cfg.get("fallback_to_static", True):
                log.warning(
                    "%s: avatar animation FAILED — falling back to static "
                    "avatar image. Reason: %s\n%s", clip_dir.name, e,
                    traceback.format_exc())
                animated = False
            else:
                raise
        else:
            for label, p in (("intro", anim_intro_path),
                             ("outro", anim_outro_path)):
                if not Path(p).is_file():
                    raise AvatarError(
                        f"animated {label} video reported success but is "
                        f"missing on disk: {p}")
            log.info("%s: animated avatar videos ready — intro=%s outro=%s",
                     clip_dir.name, anim_intro_path, anim_outro_path)

    layout = cfg.get("avatar", {}).get("layout", {})
    graph = build_composite_graph(w, h, layout, intro_dur, outro_dur, fps,
                                  ass_path=ass_path, fontsdir=fontsdir,
                                  animated=animated)
    out = clip_dir / "final_avatar.mp4"
    args = ["-i", final,
            "-loop", "1", "-t", f"{intro_dur:.3f}", "-i", first_png,
            "-loop", "1", "-t", f"{outro_dur:.3f}", "-i", last_png,
            "-i", avatar_png,
            "-i", item["intro_wav"], "-i", item["outro_wav"]]
    if animated:
        # Input-level -t cap, same as the PNG loop inputs above. Without it,
        # ffmpeg's duration estimation runs away across the whole command
        # when several other inputs are present but unreferenced by this
        # segment's -map (reproduced directly: dropping this -t blew a 4s
        # segment out to 3075s) — even though the .mov files themselves are
        # ~4.1s/~3.1s, correctly bounded.
        args += ["-t", f"{intro_dur:.3f}", "-i", anim_intro_path,
                 "-t", f"{outro_dur:.3f}", "-i", anim_outro_path]
        log.info("%s: ffmpeg overlay source = ANIMATED (%s, %s)",
                 clip_dir.name, anim_intro_path, anim_outro_path)
    else:
        log.info("%s: ffmpeg overlay source = STATIC PNG (%s)",
                 clip_dir.name, avatar_png)
    args += ["-filter_complex", graph, "-map", "[vout]", "-map", "[acat]"]
    args += ffutil.video_encode_args(cfg, final=True)
    args += ["-c:a", "aac", "-b:a", cfg["render"]["audio_bitrate"],
             "-movflags", "+faststart", out]
    if tracker:
        tracker.item("avatar", "compositing (ffmpeg)", 0.0)
    _t = time.perf_counter()
    ffutil.run_ffmpeg(args, progress_label=f"avatar {clip_dir.name}")
    st_times["composite"] = time.perf_counter() - _t
    if tracker:
        tracker.item("avatar", "compositing (ffmpeg)", 1.0)

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
            "outro_script": item["outro_script"],
            "animated": animated, "stage_timings": st_times}


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
    info = ffutil.probe_audio(src_path)
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
    sub.add_parser("setup-anim-venv",
                   help="create .venv-avatar-anim and clone/install "
                        "LivePortrait (~2-3GB)")
    sub.add_parser("setup-musetalk-venv",
                   help="create .venv-musetalk and clone/install MuseTalk "
                        "lip-sync (~3-4GB + weights)")
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
        elif a.cmd == "setup-anim-venv":
            import avatar_anim
            avatar_anim.setup_anim_venv()
        elif a.cmd == "setup-musetalk-venv":
            import avatar_anim
            avatar_anim.setup_musetalk_venv()
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
