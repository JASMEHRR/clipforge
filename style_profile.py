"""Reference style analyzer: watch example Shorts, distil their style into a
StyleProfile the refiner steers toward.

Reuses the existing pipeline pieces (ingest for files/URLs, transcribe, scenes)
so references are cached like everything else. Extracts, per reference:
  - opening hook type (llm.py classify, rule fallback / mock)
  - silence-gap stats from word timestamps (median, p90)
  - pacing (scene cuts per minute, words/sec)
  - ending resolution + CTA presence
and averages them into profiles/<name>.json. Three sample frames per reference
are dumped to cache/style_frames/ so the profile can be eyeballed and the JSON
hand-tuned. Caption vertical position is NOT read from references — it is fixed
by the CAPTION POSITION LAW and only tunable within [0.52, 0.66].

CLI:
  python style_profile.py refs/ --name mystyle
  python style_profile.py <url> <url> --name mystyle
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np

from config import ROOT, load_config
from errors import StyleError
from ffutil import run_ffmpeg
from ingest import ingest, is_url
from logutil import get_logger
from scenes import detect_scenes
from style_refiner import rule_ending_complete
from transcribe import transcribe
from schemas import validate

log = get_logger("style_profile")

_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
_CTA_PHRASES = ("follow", "subscribe", "comment", "like and", "link in bio",
                "share this", "hit the", "tap the", "check the link")


def _collect_refs(inputs: list[str]) -> list[str]:
    """Expand directories to their video files; pass URLs and files through."""
    refs: list[str] = []
    for item in inputs:
        if is_url(item):
            refs.append(item)
            continue
        p = Path(item)
        if p.is_dir():
            refs.extend(sorted(str(f) for f in p.iterdir()
                               if f.suffix.lower() in _VIDEO_EXTS))
        elif p.exists():
            refs.append(str(p))
        else:
            log.warning("reference not found, skipping: %s", item)
    return refs


def _ref_key(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]


def _extract_frames(video_path: str, duration: float, name: str, idx: int,
                    cfg: dict) -> list[str]:
    frames_dir = ROOT / cfg["paths"]["cache_dir"] / "style_frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    out = []
    for k, frac in enumerate((0.25, 0.5, 0.75)):
        t = max(0.0, duration * frac)
        fp = frames_dir / f"{name}_{idx}_{k}.jpg"
        try:
            run_ffmpeg(["-ss", f"{t:.3f}", "-i", str(video_path),
                        "-frames:v", "1", "-q:v", "3", str(fp)])
            out.append(str(fp))
        except Exception as e:  # noqa: BLE001 — frames are advisory
            log.warning("frame extract failed at %.1fs: %s", t, e)
    return out


def _analyze_one(source: str, idx: int, name: str, cfg: dict,
                 provider: str | None) -> dict:
    from style_refiner import classify_hook
    job_dir = ROOT / cfg["paths"]["cache_dir"] / "style_refs" / _ref_key(source)
    info = ingest(source, job_dir, cfg)
    transcript = transcribe(info["audio_path"], cfg)
    scenes = detect_scenes(info["video_path"], cfg)

    words = transcript["words"]
    sentences = transcript["sentences"]
    duration = max(1e-6, float(info["duration"]))

    # Silence gaps between consecutive words.
    gaps = [b["start"] - a["end"] for a, b in zip(words, words[1:])
            if b["start"] - a["end"] > 0]
    median_gap = float(np.median(gaps)) if gaps else 0.0
    p90_gap = float(np.percentile(gaps, 90)) if gaps else 0.0

    n_cuts = max(0, len(scenes["scenes"]) - 1)
    scene_cuts_per_min = n_cuts / (duration / 60.0)
    words_per_sec = len(words) / duration

    hook_type = "statement"
    if sentences:
        hook_type = classify_hook(sentences[0]["text"], cfg, provider)["hook_type"]

    last_text = sentences[-1]["text"] if sentences else ""
    resolves = rule_ending_complete(last_text, int(cfg["style"].get("min_ending_words", 4)))
    tail_text = " ".join(s["text"] for s in sentences[-2:]).lower()
    has_cta = any(p in tail_text for p in _CTA_PHRASES)

    frames = _extract_frames(info["video_path"], duration, name, idx, cfg)
    log.info("ref %d: hook=%s median_gap=%.2f p90=%.2f cuts/min=%.1f wps=%.2f "
             "resolves=%s cta=%s", idx, hook_type, median_gap, p90_gap,
             scene_cuts_per_min, words_per_sec, resolves, has_cta)
    return {
        "source": source, "hook_type": hook_type,
        "median_gap": median_gap, "p90_gap": p90_gap,
        "scene_cuts_per_min": scene_cuts_per_min, "words_per_sec": words_per_sec,
        "resolves": resolves, "has_cta": has_cta, "frames": frames,
    }


def build_profile(inputs: list[str], name: str, cfg: dict | None = None,
                  provider: str | None = None) -> dict:
    """Analyze references, average them into a StyleProfile, save + return it."""
    cfg = cfg or load_config()
    refs = _collect_refs(inputs)
    if not refs:
        raise StyleError("no valid references to analyze")

    per = []
    for i, src in enumerate(refs):
        try:
            per.append(_analyze_one(src, i, name, cfg, provider))
        except Exception as e:  # noqa: BLE001 — one bad ref never kills the run
            log.warning("reference failed, skipping: %s (%s)", src, e)
    if not per:
        raise StyleError("all references failed to analyze")

    types = [p["hook_type"] for p in per]
    dist = {t: round(types.count(t) / len(types), 4) for t in set(types)}
    dominant = max(dist, key=dist.get)

    anchor = float(cfg["style"]["captions"]["vertical_anchor"])
    anchor = min(0.66, max(0.52, anchor))
    profile = {
        "name": name,
        "references": [p["source"] for p in per],
        "hook": {"dominant_type": dominant, "type_distribution": dist},
        "silence": {
            "median_gap_s": round(float(np.mean([p["median_gap"] for p in per])), 3),
            "p90_gap_s": round(float(np.mean([p["p90_gap"] for p in per])), 3),
        },
        "pacing": {
            "scene_cuts_per_min": round(float(np.mean([p["scene_cuts_per_min"] for p in per])), 3),
            "words_per_sec": round(float(np.mean([p["words_per_sec"] for p in per])), 3),
        },
        "ending": {
            "resolves_ratio": round(sum(p["resolves"] for p in per) / len(per), 3),
            "cta_ratio": round(sum(p["has_cta"] for p in per) / len(per), 3),
        },
        "captions": {
            "vertical_anchor": round(anchor, 4),
            "words_per_line": int(cfg["style"]["captions"]["words_per_line"]),
            "emphasis": "",
        },
        "frames": [f for p in per for f in p["frames"]],
    }
    validate(profile, "style_profile")

    out_dir = ROOT / "profiles"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{name}.json"
    out_path.write_text(json.dumps(profile, indent=2), encoding="utf-8")
    log.info("wrote %s from %d references (frames in cache/style_frames/)",
             out_path, len(per))
    return profile


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="analyze reference Shorts into a StyleProfile")
    ap.add_argument("inputs", nargs="+", help="refs/ dir, video files, and/or URLs")
    ap.add_argument("--name", default="user", help="profile name -> profiles/<name>.json")
    ap.add_argument("--provider", default=None, help="LLM provider override for hook classify")
    a = ap.parse_args()
    prof = build_profile(a.inputs, a.name, provider=a.provider)
    print(json.dumps(prof, indent=2))
