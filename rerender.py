"""Clip editing: adjust a clip's start/end (snapped to sentence boundaries)
and re-render JUST that clip (cut → reframe → captions) without re-running
transcription; plus per-clip metadata regeneration.

Relies on the pipeline's completion markers: .done_transcribe.json,
.done_scenes.json and .done_ingest.json inside the job dir."""
from __future__ import annotations

import json
from pathlib import Path

from config import load_config
from errors import ClipForgeError
from logutil import get_logger

log = get_logger("rerender")


def _load_marker(job_dir: Path, stage: str) -> dict:
    marker = job_dir / f".done_{stage}.json"
    if not marker.exists():
        raise ClipForgeError(f"cannot re-render: marker .done_{stage}.json "
                             f"missing in {job_dir}")
    return json.loads(marker.read_text(encoding="utf-8"))


def load_job(job_dir: str | Path) -> dict:
    job_dir = Path(job_dir)
    p = job_dir / "job.json"
    if not p.exists():
        raise ClipForgeError(f"no job.json in {job_dir}")
    return json.loads(p.read_text(encoding="utf-8"))


def _save_job(job_dir: Path, job: dict) -> None:
    from schemas import validate
    validate(job, "job_record")
    (job_dir / "job.json").write_text(json.dumps(job, indent=2),
                                      encoding="utf-8")


def snap_bounds(job_dir: str | Path, start: float, end: float) -> tuple[float, float]:
    """Snap requested bounds to sentence boundaries (no 30-60 enforcement for
    manual edits — the user is in charge; sanity range 3–180s)."""
    transcript = _load_marker(Path(job_dir), "transcribe")
    sents = transcript["sentences"]
    if not sents:
        return round(max(0.0, start), 3), round(max(start + 3.0, end), 3)
    s = min((x["start"] for x in sents), key=lambda v: abs(v - start))
    candidates = [x["end"] for x in sents if 3.0 <= x["end"] - s <= 180.0]
    e = (min(candidates, key=lambda v: abs(v - end)) if candidates
         else min((x["end"] for x in sents), key=lambda v: abs(v - end)))
    if e <= s:
        raise ClipForgeError(f"snapped range collapsed ({s}–{e})")
    return round(s, 3), round(e, 3)


def rerender_clip(job_dir: str | Path, clip_index: int, start: float,
                  end: float, preset: str | None = None,
                  cfg: dict | None = None, provider: str | None = None,
                  style_refine: bool | None = None,
                  subs_mode: str | None = None) -> dict:
    """Re-render one clip with new (snapped) bounds. Style refinement is applied
    to the edited bounds the same way the pipeline does, unless disabled.
    Returns the updated clip record; job.json is updated in place."""
    import captions as captions_mod
    import cut as cut_mod
    import reframe as reframe_mod
    import scenes as scenes_mod
    import style_refiner as style_mod
    import subtitle_detect as subs_detect

    cfg = cfg or load_config()
    job_dir = Path(job_dir)
    job = load_job(job_dir)
    clip = next((c for c in job["clips"] if c["index"] == clip_index), None)
    if clip is None:
        raise ClipForgeError(f"no clip index {clip_index} in {job_dir}")

    info = _load_marker(job_dir, "ingest")
    transcript = _load_marker(job_dir, "transcribe")
    scene_data = _load_marker(job_dir, "scenes")
    start, end = snap_bounds(job_dir, start, end)
    aspect = clip.get("aspect", "9:16")
    preset = preset or clip.get("preset") or cfg["captions"]["preset"]
    clip_dir = job_dir / f"clip_{clip_index:02d}"
    log.info("re-render clip %02d: %.2f–%.2f preset=%s", clip_index, start,
             end, preset)

    # Refine the edited bounds unless disabled (mirrors pipeline _render_one).
    style_on = (cfg.get("style", {}).get("enabled", False)
                if style_refine is None else style_refine)
    edit_plan = None
    if style_on:
        cand = {"start": start, "end": end, "hook": clip.get("hook", ""),
                "reason": clip.get("reason", ""), "score": clip.get("candidate_score", 0)}
        subs = subs_detect.detect_subtitles(info["video_path"], start, end, cfg)
        edit_plan = style_mod.refine_clip(cand, transcript, scene_data, subs,
                                          style_mod.load_profile(cfg), cfg,
                                          provider=provider, subs_mode=subs_mode)

    if edit_plan:
        segments = edit_plan["segments"]
        out_start, out_end = segments[0][0], segments[-1][1]
        excl = edit_plan["existing_subs"]["bottom_exclusion_ratio"]
        hbias = edit_plan["existing_subs"]["h_bias_center"]
        multi = len(segments) > 1
    else:
        out_start, out_end, excl, hbias, multi = start, end, 0.0, -1.0, False
        segments = [[start, end]]

    if aspect == "16:9":
        cut_path = (cut_mod.cut_segments(info["video_path"], segments, clip_dir / "cut.mp4", cfg)
                    if multi else
                    cut_mod.cut_clip(info["video_path"], out_start, out_end, clip_dir / "cut.mp4", cfg))
        src, metrics = cut_path, {"aspect": "16:9", "passthrough": True}
    elif multi:
        cut_path = cut_mod.cut_segments(info["video_path"], segments, clip_dir / "cut.mp4", cfg)
        cut_dur = reframe_mod.probe(cut_path)["duration"]
        metrics = reframe_mod.reframe_clip(
            cut_path, 0.0, cut_dur, clip_dir / "reframed.mp4", [], cfg,
            aspect=aspect, info=None, bottom_exclusion_ratio=excl, h_bias_center=hbias)
        src = clip_dir / "reframed.mp4"
    else:
        cuts_rel = [t - out_start for t in
                    scenes_mod.scene_cuts_in_range(scene_data, out_start, out_end)]
        metrics = reframe_mod.reframe_clip(
            info["video_path"], out_start, out_end, clip_dir / "reframed.mp4",
            cuts_rel, cfg, aspect=aspect,
            info=(info if out_start == start and out_end == end else None),
            bottom_exclusion_ratio=excl, h_bias_center=hbias)
        src = clip_dir / "reframed.mp4"

    if edit_plan:
        words = edit_plan["words"]
        cap_kwargs = dict(anchor=edit_plan["caption_anchor"], cta=edit_plan["cta"],
                          captions_enabled=edit_plan["captions_enabled"],
                          fades=edit_plan["fades"], zoom_punch=edit_plan["zoom_punch"])
    else:
        words = [{"word": w["word"],
                  "start": round(max(0.0, w["start"] - out_start), 3),
                  "end": round(max(0.0, w["end"] - out_start), 3)}
                 for w in transcript["words"]
                 if w["start"] >= out_start - 0.05 and w["end"] <= out_end + 0.05]
        cap_kwargs = {}
    final = captions_mod.caption_clip(src, words, clip_dir / "final.mp4", cfg,
                                      preset_name=preset, **cap_kwargs)

    duration = edit_plan["output_duration"] if edit_plan else round(out_end - out_start, 3)
    clip.update({"start": out_start, "end": out_end,
                 "duration": duration, "preset": preset,
                 "reframe": metrics, "path": str(final),
                 "srt": str(final.with_suffix(".srt")),
                 "style": style_mod.summarize(edit_plan) if edit_plan else None})
    job.setdefault("notes", []).append(
        f"clip {clip_index:02d} re-rendered to {out_start:.2f}-{out_end:.2f}"
        + (" (refined)" if edit_plan else ""))
    _save_job(job_dir, job)
    return clip


def regenerate_metadata(job_dir: str | Path, clip_index: int,
                        cfg: dict | None = None,
                        provider: str | None = None) -> dict:
    """Regenerate one clip's metadata (provider 'auto' semantics: None →
    config resolution; falls back to the deterministic template)."""
    import metadata as metadata_mod

    cfg = cfg or load_config()
    job_dir = Path(job_dir)
    job = load_job(job_dir)
    clip = next((c for c in job["clips"] if c["index"] == clip_index), None)
    if clip is None:
        raise ClipForgeError(f"no clip index {clip_index} in {job_dir}")
    transcript = _load_marker(job_dir, "transcribe")
    text = " ".join(w["word"] for w in transcript["words"]
                    if w["start"] >= clip["start"] - 0.05
                    and w["end"] <= clip["end"] + 0.05)
    meta = metadata_mod.generate_metadata(text, clip.get("hook", ""), cfg,
                                          provider)
    clip["metadata"] = meta
    (job_dir / f"clip_{clip_index:02d}" / "metadata.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8")
    _save_job(job_dir, job)
    return meta
