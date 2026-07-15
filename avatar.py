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
