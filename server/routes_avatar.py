"""Avatar Host UI tab: voice list, avatar image library, per-clip script
preview, and applying the avatar composite to an already-rendered clip
(re-uses the same worker-thread + /ws/runs/{id} progress mechanism as
rerender)."""
from __future__ import annotations

import copy
import hashlib
import json
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from config import ROOT, load_config, save_config
from logutil import get_logger
from server import jobs
from server.copy import friendly
from server.routes_library import safe_job_path

log = get_logger("server")
router = APIRouter()

AVATAR_DIR = ROOT / "assets" / "user_avatars"

# ponytail: hardcoded kokoro-onnx v1.0 preset voice ids — no asset
# introspection. Add real enumeration if the voice set ever changes.
KOKORO_VOICES = ["af_nicole", "af_bella", "af_sarah", "am_adam", "am_michael"]

# bump to invalidate every cached voice preview at once
_PREVIEW_VERSION = "1"


def _tts_cache_dir(cfg: dict) -> Path:
    d = ROOT / str(cfg.get("paths", {}).get("cache_dir", "cache")) / "avatar_tts"
    d.mkdir(parents=True, exist_ok=True)
    return d


@router.get("/api/avatar/voices")
def list_avatar_voices():
    cfg = load_config()
    voices = [{"id": v, "label": v} for v in KOKORO_VOICES]
    if str(cfg.get("avatar", {}).get("tts", {}).get("ref_audio", "")).strip():
        voices.append({"id": "cloned", "label": "Your cloned voice"})
    animation = cfg.get("avatar", {}).get("animation", {})
    engine = (str(animation.get("engine", "liveportrait"))
              if animation.get("enabled") else "static image")
    return {"voices": voices, "engine": engine}


@router.post("/api/jobs/{job_name}/clips/{index}/avatar/script")
def avatar_script(job_name: str, index: int):
    import avatar as avatar_mod
    job_dir = safe_job_path(job_name)
    if not (job_dir / "job.json").exists():
        raise HTTPException(404, "That run's files can't be found.")
    try:
        script = avatar_mod.generate_script_for_clip(job_dir, index,
                                                      load_config())
    except Exception as e:  # noqa: BLE001 — surface a friendly message
        raise HTTPException(400, friendly(e, "Generating the avatar script"))
    return script


# regenerate is field-scoped on the frontend: this always regenerates both
# scripts together (one LLM call, same as avatar_script above) and the caller
# picks which single field to apply — see avatar.py generate_script_for_clip.
@router.post("/api/jobs/{job_name}/clips/{index}/avatar/script/regenerate")
def avatar_script_regenerate(job_name: str, index: int):
    import avatar as avatar_mod
    job_dir = safe_job_path(job_name)
    if not (job_dir / "job.json").exists():
        raise HTTPException(404, "That run's files can't be found.")
    try:
        script = avatar_mod.generate_script_for_clip(job_dir, index,
                                                      load_config())
    except Exception as e:  # noqa: BLE001 — surface a friendly message
        raise HTTPException(400, friendly(e, "Regenerating the avatar script"))
    return script


@router.get("/api/avatar/images")
def list_avatar_images():
    cfg = load_config()
    last_used = str(cfg.get("avatar", {}).get("image", "")).strip()
    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    images = [
        {"path": str(p.relative_to(ROOT)).replace("\\", "/"), "name": p.name,
         "url": f"/api/avatar/images/{p.name}"}
        for p in sorted(AVATAR_DIR.glob("*.png"))
    ]
    # only preselect last_used if the thumbnail endpoint can actually serve it
    # (it serves AVATAR_DIR by basename). A config pointing at a missing or
    # non-servable file (e.g. a user_branding/ path) otherwise 404s the panel's
    # avatar thumbnail on every load.
    if last_used and not (AVATAR_DIR / Path(last_used).name).is_file():
        last_used = ""
    return {"images": images, "last_used": last_used or None}


@router.get("/api/avatar/images/{name}")
def avatar_image_file(name: str):
    """Serve one uploaded avatar PNG for the picker's thumbnail grid and the
    live compositing preview. Sandboxed to AVATAR_DIR by filename only — no
    path segments accepted."""
    if "/" in name or "\\" in name or name in (".", ".."):
        raise HTTPException(404, "That image can't be found.")
    p = AVATAR_DIR / name
    if not p.is_file():
        raise HTTPException(404, "That image can't be found.")
    return FileResponse(str(p))


class VoicePreviewRequest(BaseModel):
    voice: str
    text: str


@router.post("/api/avatar/voice-preview")
def avatar_voice_preview(req: VoicePreviewRequest):
    """Synthesize one line in one voice via Kokoro (fast, in-process) and cache
    it by (voice, text) so replays are instant and only edits regenerate. Only
    the built-in voices are previewable — the cloned/chatterbox path is minutes
    per line, unusable on demand."""
    import avatar as avatar_mod
    text = (req.text or "").strip()
    voice = (req.voice or "").strip()
    if not text:
        raise HTTPException(422, "Nothing to say yet — write some script text "
                                 "first.")
    if voice not in KOKORO_VOICES:
        raise HTTPException(422, "Preview is only available for the built-in "
                                 "voices.")
    key = hashlib.sha256(json.dumps(
        {"voice": voice, "text": text, "engine": "kokoro",
         "v": _PREVIEW_VERSION}, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    wav = _tts_cache_dir(load_config()) / f"{key}.wav"
    if not wav.is_file():
        cfg = copy.deepcopy(load_config())
        tts = cfg.setdefault("avatar", {}).setdefault("tts", {})
        tts["engine"] = "kokoro"
        tts.setdefault("kokoro", {})["voice"] = voice
        # synth to a unique temp then atomic-replace: a failed or concurrent
        # synth never leaves a partial/empty wav that later serves as a "hit"
        tmp = wav.with_name(f"{key}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            avatar_mod.synthesize_batch(
                [{"text": text, "out_path": str(tmp)}], cfg)
            tmp.replace(wav)
        except Exception as e:  # noqa: BLE001 — surface a friendly message
            tmp.unlink(missing_ok=True)
            raise HTTPException(400, friendly(e, "Generating the voice preview"))
    return {"url": f"/api/avatar/voice-preview/{key}.wav"}


@router.get("/api/avatar/voice-preview/{name}")
def avatar_voice_preview_file(name: str):
    """Serve one cached preview wav. Sandboxed to the cache dir by filename."""
    if "/" in name or "\\" in name or name in (".", "..") \
            or not name.endswith(".wav"):
        raise HTTPException(404, "That preview can't be found.")
    p = _tts_cache_dir(load_config()) / name
    if not p.is_file():
        raise HTTPException(404, "That preview can't be found.")
    return FileResponse(str(p))


@router.get("/api/avatar/render-estimate")
def avatar_render_estimate(audio_s: float = 0.0):
    """Per-stage ETA for the current avatar engine at a given TTS audio length
    (seconds). The frontend estimates audio_s from the script word counts."""
    import avatar as avatar_mod
    cfg = load_config()
    cfg.setdefault("avatar", {})["enabled"] = True
    engine = avatar_mod.avatar_engine_key(cfg)
    return avatar_mod.estimate_avatar_stages(engine, max(0.0, float(audio_s)))


class AvatarRenderRequest(BaseModel):
    intro_script: str
    outro_script: str
    voice: str | None = None
    side: str | None = None
    avatar_scale: float | None = None
    avatar_image: str | None = None


@router.post("/api/jobs/{job_name}/clips/{index}/avatar/render")
def avatar_render(job_name: str, index: int, req: AvatarRenderRequest):
    import avatar as avatar_mod
    job_dir = safe_job_path(job_name)
    if not (job_dir / "job.json").exists():
        raise HTTPException(404, "That run's files can't be found.")

    cfg = copy.deepcopy(load_config())
    avatar_cfg = cfg.setdefault("avatar", {})
    avatar_cfg["enabled"] = True
    if req.voice:
        avatar_cfg.setdefault("tts", {}).setdefault("engine", "kokoro")
        avatar_cfg["tts"].setdefault("kokoro", {})["voice"] = req.voice
    layout = avatar_cfg.setdefault("layout", {})
    if req.side in ("left", "right"):
        layout["side"] = req.side
    if req.avatar_scale:
        layout["avatar_scale"] = float(req.avatar_scale)
    if req.avatar_image:
        # must stay inside the repo (assets/user_avatars/ or user_branding/,
        # same sandboxing rule as safe_job_path) — never an absolute/escaping
        # path from client input
        candidate = (ROOT / req.avatar_image).resolve()
        if not candidate.is_relative_to(ROOT) or not candidate.is_file():
            raise HTTPException(422, "That avatar image can't be found.")
        avatar_cfg["image"] = req.avatar_image
        save_config({"avatar": {"image": req.avatar_image}})

    # rough TTS audio length from word counts (same heuristic as the UI) → ETA
    audio_s = avatar_mod.estimate_audio_seconds(req.intro_script, req.outro_script)
    engine = avatar_mod.avatar_engine_key(cfg)
    total_est = avatar_mod.estimate_avatar_stages(engine, audio_s)["total_s"]

    handle = jobs.create(f"av_{uuid.uuid4().hex[:8]}")

    def work(h: jobs.RunHandle) -> None:
        try:
            # mark the stage running so its server-side elapsed is tracked
            # (survives a page refresh) and seed the upfront ETA hint
            h.tracker.start("avatar", "hosting clip")
            h.tracker.set_hint("avatar", total_est)
            meta = avatar_mod.apply_avatar_to_clip(
                job_dir, index, cfg, req.intro_script, req.outro_script,
                tracker=h.tracker)
            h.finish("done", result=meta)
        except Exception as e:  # noqa: BLE001 — worker must record, never raise
            h.finish("error", error=friendly(e, "Applying the avatar"))

    jobs.launch(handle, work)
    return {"run_id": handle.id}
