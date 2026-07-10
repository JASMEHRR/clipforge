"""Viral detection v2 — multimodal viral-moment detection (the "new eyes").

Three event sources merge into one deduplicated per-job timeline
(schema: event_timeline, absolute source seconds):

  audio      — local DSP on the already-extracted 16 kHz wav (numpy only, not
               a model): RMS energy spikes + laughter-like noise bursts.
               Free, instant, runs always — even keyless.
  gemini     — primary cloud eyes: the source is split into stream-copy chunks,
               each uploaded via the Gemini Files API and analyzed with a
               structured prompt (schema: viral_events).
  openrouter — fallback when Gemini is exhausted/unavailable: frame batches
               (1 frame per viral_v2.frame_interval_s) through a free vision
               model on OpenRouter's OpenAI-compatible endpoint.

PRIVACY: a local file is never uploaded to any provider unless
viral_v2.allow_upload is explicitly true. YouTube-URL sources are exempt
(already public). When blocked, only the audio DSP source runs.

Free-tier guards: per-chunk results are cached under cache/viral_events/
keyed by chunk hash + prompt hash + provider (idempotent, resumable), and
cache/viral_v2_usage.json enforces viral_v2.max_daily_minutes of cloud
analysis per day.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import wave
from datetime import date
from pathlib import Path

import numpy as np

import llm
from config import load_config
from errors import LLMError
from ffutil import run_ffmpeg
from logutil import get_logger
from schemas import VIRAL_EVENT_TYPES, validate

log = get_logger("video_events")

# Event types whose timing marks a reaction beat (used by highlights/reframe).
REACTION_TYPES = ("laughter", "strong_reaction")

PROMPT_TEMPLATE = """You are analyzing a video chunk to find every moment with viral potential.

Watch/inspect the provided media and list ALL notable moments. For each moment give:
- type: one of {types}
- t_start / t_end: timestamps WITHIN THIS CHUNK as MM:SS (e.g. "1:23")
- description: one short sentence of what happens
- intensity_1_10: how strong/viral the moment is (10 = exceptional)
- actors_hint: who is involved (e.g. "man on the left", "host"), empty if unclear

Look especially for: laughter (individual or group), strong reactions
(shock, disbelief, hype), physical events (falls, collisions, stunts),
reveals (food, objects), expression shifts, sudden energy spikes,
profound statements, conflict, celebrations.

Respond with ONLY a JSON object: {{"events": [...]}}. If nothing notable
happens, return {{"events": []}}.
{frames_note}"""

FRAMES_NOTE = ("\nThe media is a sequence of frames sampled every "
               "{interval:.0f} seconds; frame N is at N*{interval:.0f}s "
               "into the chunk.")


class QuotaExhausted(Exception):
    """Cloud provider quota hit — orchestrator moves to the next provider."""


# ------------------------------------------------------------- audio (DSP)

def _read_wav(audio_path: str | Path) -> tuple[np.ndarray, int] | None:
    """Mono float64 samples + sample rate, or None if unreadable."""
    try:
        with wave.open(str(audio_path), "rb") as wf:
            sr = wf.getframerate()
            ch = wf.getnchannels()
            width = wf.getsampwidth()
            frames = wf.readframes(wf.getnframes())
    except (wave.Error, OSError, EOFError) as e:
        log.warning("audio events: wav read failed (%s) — skipping audio source", e)
        return None
    if width != 2 or not frames:
        log.warning("audio events: unexpected wav format (width=%d) — skipping",
                    width)
        return None
    # float32: a 6-hour 16 kHz source stays ~1.4 GB smaller than float64 here
    # and in the framed FFT below, with no effect on event detection.
    data = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1)
    return data, sr


def audio_events(audio_path: str | Path, cfg: dict) -> list[dict]:
    """Energy spikes and laughter-like bursts from plain DSP on the 16 kHz wav.

    Per 250 ms frame: RMS (loudness), zero-crossing rate (noisiness) and
    spectral flatness (broadband vs tonal). Loud frames that are noisy AND
    broadband look like laughter/crowd; loud frames that are not are energy
    spikes (impacts, shouts, music hits)."""
    r = _read_wav(audio_path)
    if r is None:
        return []
    data, sr = r
    frame_n = max(1, int(sr * 0.25))
    n_frames = len(data) // frame_n
    if n_frames < 8:
        return []
    x = data[: n_frames * frame_n].reshape(n_frames, frame_n)

    rms = np.sqrt(np.mean(np.square(x), axis=1))
    zcr = np.mean(np.abs(np.diff(np.signbit(x).astype(np.int8), axis=1)), axis=1)
    # framed FFT in batches: the full spectrum matrix of a multi-hour wav
    # would not fit in memory (n_frames x 2001 floats)
    flatness = np.empty(n_frames, dtype=np.float32)
    batch = 4096
    for b in range(0, n_frames, batch):
        spec = np.abs(np.fft.rfft(x[b:b + batch], axis=1)) + 1e-12
        flatness[b:b + batch] = (np.exp(np.mean(np.log(spec), axis=1))
                                 / np.mean(spec, axis=1))

    # Robust baseline: loud events must not inflate their own threshold
    # (median/MAD instead of mean/std).
    med = float(np.median(rms))
    mad = float(np.median(np.abs(rms - med))) * 1.4826
    scale = max(mad, 0.05 * med)  # floor for near-constant beds
    if scale < 1e-6:
        scale = float(rms.std())
    if scale < 1e-6:  # silence / constant signal: nothing to find
        return []
    z = (rms - med) / scale
    active = z > 4.0
    laugh = active & (zcr > 0.15) & (flatness > 0.3)
    spike = active & ~laugh

    frame_s = frame_n / sr
    events: list[dict] = []
    for mask, etype, desc in ((laugh, "laughter", "laughter-like noise burst"),
                              (spike, "energy_spike", "audio energy spike")):
        for i0, i1 in _runs(mask):
            if (i1 - i0) * frame_s < 0.4:  # single-frame blips are noise
                continue
            peak_z = float(z[i0:i1].max())
            events.append({
                "type": etype,
                "t_start_s": round(i0 * frame_s, 2),
                "t_end_s": round(i1 * frame_s, 2),
                "description": desc,
                "intensity_1_10": round(min(10.0, max(1.0, 1.0 + peak_z / 2.0)), 1),
                "actors_hint": "",
                "source": "audio",
            })
    log.info("audio DSP found %d events", len(events))
    return events


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """Contiguous True runs in a boolean array as [start, end) index pairs."""
    edges = np.flatnonzero(np.diff(np.concatenate(([False], mask, [False]))))
    return list(zip(edges[::2], edges[1::2]))


# --------------------------------------------------------------- merging

def merge_events(*event_lists: list[dict], gap_s: float = 2.0) -> list[dict]:
    """One deduplicated timeline: events overlapping or within gap_s merge —
    span union, max intensity; type/description/actors from the stronger one."""
    events = sorted((e for lst in event_lists for e in lst),
                    key=lambda e: (e["t_start_s"], e["t_end_s"]))
    merged: list[dict] = []
    for e in events:
        if merged and e["t_start_s"] <= merged[-1]["t_end_s"] + gap_s:
            m = merged[-1]
            winner = e if e["intensity_1_10"] > m["intensity_1_10"] else m
            m.update({
                "t_end_s": max(m["t_end_s"], e["t_end_s"]),
                "intensity_1_10": winner["intensity_1_10"],
                "type": winner["type"],
                "description": winner["description"],
                "actors_hint": winner.get("actors_hint", ""),
                "source": winner["source"],
            })
        else:
            merged.append(dict(e))
    return merged


# ------------------------------------------------------ chunking + prompts

def chunk_video(video_path: str | Path, duration: float, cfg: dict,
                work_dir: Path) -> list[dict]:
    """Stream-copy the source into chunk_minutes pieces (fast, no re-encode).
    Returns [{path, start_s, seconds, sha}]."""
    chunk_s = float(cfg["viral_v2"].get("chunk_minutes", 10)) * 60.0
    work_dir.mkdir(parents=True, exist_ok=True)
    chunks: list[dict] = []
    t = 0.0
    i = 0
    while t < duration - 0.5:
        seconds = min(chunk_s, duration - t)
        out = work_dir / f"chunk_{i:03d}{Path(video_path).suffix or '.mp4'}"
        run_ffmpeg(["-ss", f"{t:.3f}", "-i", str(video_path), "-t",
                    f"{seconds:.3f}", "-c", "copy", str(out)],
                   progress_label=f"events chunk {i}")
        chunks.append({"path": out, "start_s": t, "seconds": seconds,
                       "sha": _file_sha16(out)})
        t += chunk_s
        i += 1
    return chunks


def _file_sha16(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()[:16]


def build_prompt(cfg: dict, frames: bool = False) -> str:
    note = ""
    if frames:
        note = FRAMES_NOTE.format(
            interval=float(cfg["viral_v2"].get("frame_interval_s", 2.0)))
    return PROMPT_TEMPLATE.format(types=", ".join(VIRAL_EVENT_TYPES),
                                  frames_note=note)


def _mmss_to_s(txt: str) -> float:
    """Tolerant MM:SS (or H:MM:SS) -> seconds."""
    parts = [p.strip() for p in str(txt).split(":")]
    try:
        nums = [float(p) for p in parts if p != ""]
    except ValueError:
        return 0.0
    if not nums:
        return 0.0
    s = 0.0
    for n in nums:
        s = s * 60.0 + n
    return max(0.0, s)


def _to_absolute(raw_events: list[dict], chunk_start: float,
                 chunk_seconds: float, source: str) -> list[dict]:
    """Chunk-relative MM:SS events -> absolute-seconds timeline events."""
    out = []
    for e in raw_events:
        t0 = min(_mmss_to_s(e["t_start"]), chunk_seconds)
        t1 = min(max(_mmss_to_s(e["t_end"]), t0), chunk_seconds)
        out.append({
            "type": e["type"],
            "t_start_s": round(chunk_start + t0, 2),
            "t_end_s": round(chunk_start + t1, 2),
            "description": e["description"],
            "intensity_1_10": float(e["intensity_1_10"]),
            "actors_hint": e.get("actors_hint", ""),
            "source": source,
        })
    return out


# ------------------------------------------------- quota + per-chunk cache

def _cache_dir(cfg: dict) -> Path:
    return Path(cfg["paths"]["cache_dir"]) / "viral_events"


def _usage_path(cfg: dict) -> Path:
    return Path(cfg["paths"]["cache_dir"]) / "viral_v2_usage.json"


def _load_usage(cfg: dict) -> dict:
    p = _usage_path(cfg)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, json.JSONDecodeError) as e:
        log.warning("usage file unreadable (%s) — starting fresh", e)
        return {}


def quota_remaining_minutes(cfg: dict) -> float:
    limit = float(cfg["viral_v2"].get("max_daily_minutes", 120))
    used = float(_load_usage(cfg).get(date.today().isoformat(), 0.0))
    return max(0.0, limit - used)


def _quota_add(cfg: dict, minutes: float) -> None:
    usage = _load_usage(cfg)
    key = date.today().isoformat()
    usage[key] = round(float(usage.get(key, 0.0)) + minutes, 2)
    p = _usage_path(cfg)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(usage, indent=2), encoding="utf-8")
    except OSError as e:
        log.warning("could not persist usage file: %s", e)
    log.info("viral_v2 cloud usage today: %.1f min (limit %.0f)",
             usage[key], float(cfg["viral_v2"].get("max_daily_minutes", 120)))


def _chunk_cache_key(chunk: dict, prompt: str, provider: str) -> str:
    p8 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:8]
    return f"{chunk['sha']}_{p8}_{provider}"


def _cache_get(cfg: dict, key: str) -> list[dict] | None:
    p = _cache_dir(cfg) / f"{key}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))["events"]
    except (OSError, json.JSONDecodeError, KeyError) as e:
        log.warning("chunk cache %s unreadable (%s) — re-analyzing", key, e)
        return None


def _cache_put(cfg: dict, key: str, events: list[dict]) -> None:
    d = _cache_dir(cfg)
    try:
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{key}.json").write_text(json.dumps({"events": events}, indent=2),
                                       encoding="utf-8")
    except OSError as e:
        log.warning("could not write chunk cache %s: %s", key, e)


def _is_quota_error(err: Exception) -> bool:
    s = f"{err} {getattr(err, 'detail', '')}"
    return ("429" in s or "RESOURCE_EXHAUSTED" in s or "quota" in s.lower()
            or "rate limit" in s.lower())


# ------------------------------------------------------ cloud chunk paths

def gemini_chunk_events(chunk: dict, cfg: dict, prompt: str) -> list[dict]:
    key = _chunk_cache_key(chunk, prompt, "gemini")
    cached = _cache_get(cfg, key)
    if cached is not None:
        log.info("chunk %s: gemini cache hit (%d events)",
                 chunk["path"].name, len(cached))
        return cached
    if quota_remaining_minutes(cfg) < chunk["seconds"] / 60.0:
        raise QuotaExhausted("viral_v2.max_daily_minutes reached")
    retries = int(cfg["llm"].get("max_retries", 2))
    backoff = float(cfg["llm"].get("backoff_base_seconds", 1.5))
    try:
        for attempt in range(retries + 1):
            try:
                handle = llm.upload_media(chunk["path"], cfg)
                break
            except LLMError as e:
                if attempt >= retries or not e.retryable:
                    raise
                time.sleep(backoff * (2 ** attempt))
        data = llm.complete_json(
            "viral_events", "viral_events", prompt, provider="gemini",
            cfg=cfg, media=[{"kind": "gemini_file", "handle": handle}])
    except LLMError as e:
        if _is_quota_error(e):
            raise QuotaExhausted(str(e)) from e
        raise
    events = _to_absolute(data["events"], chunk["start_s"], chunk["seconds"],
                          "gemini")
    _cache_put(cfg, key, events)
    _quota_add(cfg, chunk["seconds"] / 60.0)
    return events


def openrouter_chunk_events(chunk: dict, cfg: dict, prompt: str) -> list[dict]:
    key = _chunk_cache_key(chunk, prompt, "openrouter")
    cached = _cache_get(cfg, key)
    if cached is not None:
        log.info("chunk %s: openrouter cache hit (%d events)",
                 chunk["path"].name, len(cached))
        return cached
    if quota_remaining_minutes(cfg) < chunk["seconds"] / 60.0:
        raise QuotaExhausted("viral_v2.max_daily_minutes reached")
    media = _extract_frames(chunk, cfg)
    if not media:
        log.warning("chunk %s: no frames extracted — skipping openrouter",
                    chunk["path"].name)
        return []
    try:
        data = llm.complete_json(
            "viral_events", "viral_events", prompt, provider="openrouter",
            cfg=cfg, media=media)
    except LLMError as e:
        if _is_quota_error(e):
            raise QuotaExhausted(str(e)) from e
        raise
    events = _to_absolute(data["events"], chunk["start_s"], chunk["seconds"],
                          "openrouter")
    _cache_put(cfg, key, events)
    _quota_add(cfg, chunk["seconds"] / 60.0)
    return events


def _extract_frames(chunk: dict, cfg: dict) -> list[dict]:
    """1 frame per frame_interval_s at 360p as jpeg media parts."""
    interval = float(cfg["viral_v2"].get("frame_interval_s", 2.0))
    frames_dir = chunk["path"].parent / f"{chunk['path'].stem}_frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(["-i", str(chunk["path"]),
                "-vf", f"fps=1/{interval},scale=-2:360",
                "-q:v", "5", str(frames_dir / "f_%04d.jpg")],
               progress_label="events frames")
    media = []
    for p in sorted(frames_dir.glob("f_*.jpg")):
        media.append({"kind": "image", "mime": "image/jpeg",
                      "data": p.read_bytes()})
    shutil.rmtree(frames_dir, ignore_errors=True)
    return media


# ------------------------------------------------------------ orchestrator

def detect_events(video_path: str | Path, audio_path: str | Path,
                  duration: float, ingest_info: dict, cfg: dict | None = None,
                  provider: str | None = None, debug_dir: Path | None = None,
                  progress_cb=None) -> list[dict]:
    """Full per-job event timeline (absolute seconds, schema event_timeline).

    Audio DSP always runs. Cloud analysis runs only when a provider is
    available AND the privacy gate passes; provider order comes from
    viral_v2.providers, falling through on quota exhaustion."""
    cfg = cfg or load_config()
    vcfg = cfg["viral_v2"]
    events = audio_events(audio_path, cfg)

    name = llm.resolve_provider(cfg, provider)
    if name == "mock":
        events = merge_events(events, _mock_cloud_events(duration, cfg),
                              gap_s=float(vcfg.get("merge_gap_s", 2.0)))
    else:
        upload_ok = (ingest_info.get("source_type") == "url"
                     or bool(vcfg.get("allow_upload", False)))
        if not upload_ok:
            log.info("viral_v2: local source and viral_v2.allow_upload=false — "
                     "cloud video analysis skipped (set allow_upload: true to "
                     "enable); using audio-only events")
        else:
            cloud = _cloud_events(video_path, duration, cfg, progress_cb)
            events = merge_events(events, cloud,
                                  gap_s=float(vcfg.get("merge_gap_s", 2.0)))

    events = [e for e in events if e["t_start_s"] < duration]
    for e in events:
        e["t_end_s"] = min(e["t_end_s"], duration)
    events.sort(key=lambda e: e["t_start_s"])
    validate({"events": events}, "event_timeline")
    log.info("event timeline: %d events (%s)", len(events),
             ", ".join(sorted({e["source"] for e in events})) or "none")
    return events


def _mock_cloud_events(duration: float, cfg: dict) -> list[dict]:
    """Keyless gate path: canned events from the mock provider, chunked
    virtually (no ffmpeg, no upload)."""
    chunk_s = float(cfg["viral_v2"].get("chunk_minutes", 10)) * 60.0
    prompt = build_prompt(cfg)
    events: list[dict] = []
    t = 0.0
    while t < duration - 0.5:
        seconds = min(chunk_s, duration - t)
        data = llm.complete_json(
            "viral_events", "viral_events", prompt, provider="mock", cfg=cfg,
            context={"chunk_start": t, "chunk_seconds": seconds})
        events += _to_absolute(data["events"], t, seconds, "mock")
        t += chunk_s
    return events


def _available_cloud_providers(cfg: dict) -> list[str]:
    keys = {"gemini": "GEMINI_API_KEY", "openrouter": "OPENROUTER_API_KEY"}
    out = []
    for p in cfg["viral_v2"].get("providers", ["gemini", "openrouter"]):
        env = keys.get(p)
        if env is None:
            log.warning("viral_v2.providers: unknown provider '%s' ignored", p)
        elif os.environ.get(env):
            out.append(p)
        else:
            log.info("viral_v2: provider '%s' skipped (%s not set)", p, env)
    return out


def _cloud_events(video_path, duration, cfg, progress_cb) -> list[dict]:
    providers = _available_cloud_providers(cfg)
    if not providers:
        log.info("viral_v2: no cloud provider key available — audio-only events")
        return []
    work_dir = Path(video_path).parent / "events_chunks"
    try:
        chunks = chunk_video(video_path, duration, cfg, work_dir)
    except Exception as e:
        log.warning("viral_v2: chunking failed (%s) — audio-only events", e)
        return []

    fns = {"gemini": gemini_chunk_events, "openrouter": openrouter_chunk_events}
    prompts = {"gemini": build_prompt(cfg), "openrouter": build_prompt(cfg, frames=True)}
    events: list[dict] = []
    try:
        for i, chunk in enumerate(chunks):
            if progress_cb:
                progress_cb(i / max(1, len(chunks)),
                            f"analyzing chunk {i + 1}/{len(chunks)}")
            while providers:
                p = providers[0]
                try:
                    events += fns[p](chunk, cfg, prompts[p])
                    break
                except QuotaExhausted as e:
                    log.warning("viral_v2: %s exhausted (%s) — falling through",
                                p, e)
                    providers.pop(0)
                except LLMError as e:
                    log.warning("viral_v2: %s failed on chunk %d: %s — "
                                "continuing", p, i, e)
                    break  # transient failure: skip chunk, keep provider
            if not providers:
                log.warning("viral_v2: all cloud providers exhausted after "
                            "chunk %d/%d — partial timeline", i, len(chunks))
                break
    finally:
        if not (cfg.get("debug") or False):
            shutil.rmtree(work_dir, ignore_errors=True)
    return events


if __name__ == "__main__":
    # Smoke self-check on the pure logic (no ffmpeg, no network).
    assert _mmss_to_s("1:23") == 83.0
    assert _mmss_to_s("12:03") == 723.0
    assert _mmss_to_s("1:02:03") == 3723.0
    a = {"type": "laughter", "t_start_s": 10.0, "t_end_s": 12.0,
         "description": "a", "intensity_1_10": 5.0, "actors_hint": "",
         "source": "audio"}
    b = dict(a, t_start_s=13.0, t_end_s=15.0, intensity_1_10=8.0,
             source="gemini", description="b")
    m = merge_events([a], [b])
    assert len(m) == 1 and m[0]["intensity_1_10"] == 8.0
    assert m[0]["t_end_s"] == 15.0 and m[0]["source"] == "gemini"
    c = dict(a, t_start_s=30.0, t_end_s=31.0)
    assert len(merge_events([a, c])) == 2
    print("video_events self-check OK")
