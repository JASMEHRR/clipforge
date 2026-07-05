"""Scene detection: PySceneDetect shot boundaries (cut snapping + reframe
resets). Cached by video hash + detector config."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from config import ROOT, file_hash, load_config
from errors import SceneError
from ffutil import probe
from logutil import get_logger
from schemas import validate

log = get_logger("scenes")


def detect_scenes(video_path: str | Path, cfg: dict | None = None) -> dict:
    """Returns SceneList dict (schema-validated). Zero detections → one scene
    spanning the whole video (never an empty list)."""
    cfg = cfg or load_config()
    video_path = Path(video_path)
    if not video_path.exists():
        raise SceneError(f"video not found: {video_path}")

    cache_dir = ROOT / cfg["paths"]["cache_dir"] / "scenes"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"{file_hash(video_path)[:24]}.json"
    if cache_file.exists():
        log.info("scene cache hit: %s", cache_file.name)
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        validate(data, "scene_list")
        return data

    duration = probe(video_path)["duration"]
    try:
        from scenedetect import ContentDetector, detect
        raw = detect(str(video_path), ContentDetector(threshold=27.0),
                     show_progress=False)
    except Exception as e:  # noqa: BLE001
        raise SceneError("PySceneDetect failed", detail=str(e)[:500]) from e

    scenes = [{"index": i,
               "start": round(s.seconds, 3),
               "end": round(e.seconds, 3)}
              for i, (s, e) in enumerate(raw)]
    if not scenes:
        scenes = [{"index": 0, "start": 0.0, "end": round(duration, 3)}]
        log.info("no cuts detected — single scene [0, %.1f]", duration)

    data = {"scenes": scenes}
    validate(data, "scene_list")
    cache_file.write_text(json.dumps(data), encoding="utf-8")
    log.info("detected %d scenes over %.1fs", len(scenes), duration)
    return data


def scene_cuts_in_range(scenes: dict, start: float, end: float) -> list[float]:
    """Scene-boundary times strictly inside (start, end) — reframe resets."""
    return [s["start"] for s in scenes["scenes"]
            if start + 0.05 < s["start"] < end - 0.05]


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="smoke: detect scenes")
    ap.add_argument("video")
    a = ap.parse_args()
    d = detect_scenes(a.video)
    print(json.dumps({"count": len(d["scenes"]), "first3": d["scenes"][:3]},
                     indent=2))
