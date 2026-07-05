"""Pipeline orchestrator: ingest → transcribe → scenes → highlights →
per-clip (cut → reframe → captions → metadata) → rescore.

- per-stage progress + timings (table printed at the end)
- resume: completion markers (.done_<stage>.json) skip finished stages
  unless --force
- --sample: configured sample source (mirrors → synthetic fallback)
- --provider: override config provider (gates force mock)
- a failing clip never kills the job; a failing job never kills a queue"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import uuid
from pathlib import Path

from config import ROOT, load_config
from errors import ClipForgeError
from ffutil import verify_ffmpeg
from logutil import add_file_handler, get_logger, remove_file_handler, stage_timer
from schemas import validate

log = get_logger("pipeline")


def _slug(source: str) -> str:
    base = Path(str(source)).stem if not str(source).startswith("http") else "url"
    return re.sub(r"[^A-Za-z0-9_-]+", "-", base)[:40] or "job"


def new_job_dir(cfg: dict, source: str) -> Path:
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    d = ROOT / cfg["paths"]["output_dir"] / f"{ts}_{_slug(source)}"
    d.mkdir(parents=True, exist_ok=False)
    return d


class _Stages:
    """Completion-marker helper: marker file caches the stage's JSON result."""

    def __init__(self, job_dir: Path, force: bool, timings: dict):
        self.job_dir, self.force, self.timings = job_dir, force, timings

    def run(self, name: str, fn):
        marker = self.job_dir / f".done_{name}.json"
        if marker.exists() and not self.force:
            log.info("stage %s: marker present — skipped (use --force to redo)",
                     name)
            self.timings[name] = {"status": "skipped", "seconds": 0.0}
            return json.loads(marker.read_text(encoding="utf-8"))
        with stage_timer(log, name, self.timings):
            result = fn()
        marker.write_text(json.dumps(result), encoding="utf-8")
        return result


def run_job(source: str, cfg: dict | None = None, provider: str | None = None,
            job_dir: str | Path | None = None, force: bool = False,
            preset: str | None = None, aspect: str = "9:16",
            debug: bool | None = None, progress_cb=None) -> dict:
    import captions as captions_mod
    import cut as cut_mod
    import highlights as hl
    import ingest as ingest_mod
    import metadata as metadata_mod
    import reframe as reframe_mod
    import scenes as scenes_mod
    import transcribe as transcribe_mod
    from llm import resolve_provider

    cfg = cfg or load_config()
    verify_ffmpeg(cfg)
    debug = cfg.get("debug", False) if debug is None else debug
    job_dir = Path(job_dir) if job_dir else new_job_dir(cfg, source)
    job_dir.mkdir(parents=True, exist_ok=True)
    debug_dir = (job_dir / "debug") if debug else None
    fh = add_file_handler(job_dir / "job.log")
    timings: dict = {}
    notes: list[str] = []
    stages = _Stages(job_dir, force, timings)
    resolved = resolve_provider(cfg, provider)
    log.info("job start: source=%s provider=%s aspect=%s dir=%s",
             source, resolved, aspect, job_dir.name)

    def report(stage: str, frac: float, msg: str = ""):
        if progress_cb:
            progress_cb(stage, frac, msg)

    job = {
        "job_id": uuid.uuid4().hex[:12],
        "created": dt.datetime.now().isoformat(timespec="seconds"),
        "source": str(source),
        "status": "running",
        "settings": {"provider": resolved, "aspect": aspect,
                     "preset": preset or cfg["captions"]["preset"],
                     "debug": bool(debug)},
        "stages": {}, "clips": [], "notes": notes,
    }

    try:
        report("ingest", 0.02, "downloading / normalizing input")
        info = stages.run("ingest", lambda: ingest_mod.ingest(
            source, job_dir, cfg,
            progress_cb=lambda f, msg: report("ingest", 0.02 + 0.12 * f, msg)))

        report("transcribe", 0.15, "transcribing audio")
        transcript = stages.run("transcribe", lambda: transcribe_mod.transcribe(
            info["audio_path"], cfg, debug_dir=debug_dir,
            progress_cb=lambda f: report(
                "transcribe", 0.15 + 0.15 * f,
                f"transcribing {f * 100:.0f}% "
                f"({f * info['duration'] / 60:.0f}/{info['duration'] / 60:.0f} min)")))
        if not transcript["sentences"]:
            notes.append("empty transcript — mechanical windows used "
                         "(passed mechanically; re-verify with a real sample)")

        report("scenes", 0.30, "detecting shots")
        scene_data = stages.run("scenes", lambda: scenes_mod.detect_scenes(
            info["video_path"], cfg))

        report("highlights", 0.38, "selecting highlights")
        candidates = stages.run("highlights", lambda: hl.select_highlights(
            transcript, scene_data, info["duration"], cfg,
            provider=provider, debug_dir=debug_dir))
        if len(candidates) <= cfg["clips"]["min_keep"]:
            notes.append(f"only {len(candidates)} candidates — all kept "
                         "(min-keep rule)")

        clips = []
        n = max(1, len(candidates))
        workers = _worker_count(cfg, n)
        with stage_timer(log, "render_clips", timings):
            from concurrent.futures import ThreadPoolExecutor, as_completed
            done = 0
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(
                    _render_one, i, cand, info, transcript, scene_data,
                    job_dir, cfg, provider, preset, aspect, debug_dir,
                    cut_mod, reframe_mod, captions_mod, metadata_mod,
                    scenes_mod): i for i, cand in enumerate(candidates)}
                for fut in as_completed(futures):
                    i = futures[fut]
                    done += 1
                    report("render", 0.45 + 0.45 * done / n,
                           f"clip {done}/{n} finished")
                    try:
                        clips.append(fut.result())
                    except ClipForgeError as e:
                        log.error("clip %02d failed: %s — continuing", i, e)
                        notes.append(f"clip {i:02d} failed: {e}")
            clips.sort(key=lambda c: c["index"])
        if not clips:
            raise ClipForgeError("no clips rendered successfully")

        report("rescore", 0.93, "re-scoring clips")
        with stage_timer(log, "rescore", timings):
            clips = hl.rescore_clips(clips, transcript, cfg, provider)

        job["clips"] = clips
        job["status"] = "done"
    except Exception as e:  # noqa: BLE001 — job must record failure, not crash callers
        job["status"] = "failed"
        notes.append(f"job failed: {e}")
        log.error("job failed: %s", e)
    finally:
        job["stages"] = timings
        validate(job, "job_record")
        (job_dir / "job.json").write_text(json.dumps(job, indent=2),
                                          encoding="utf-8")
        try:
            from history import record_job
            record_job(job, job_dir, cfg)
        except Exception as e:  # noqa: BLE001 — history is best-effort
            log.warning("history record failed: %s", e)
        _print_timings(timings)
        remove_file_handler(fh)

    report("done", 1.0, job["status"])
    if job["status"] == "failed":
        raise ClipForgeError(f"job {job['job_id']} failed", detail="; ".join(notes))
    log.info("job done: %d clips (%d kept) in %s",
             len(job["clips"]),
             sum(1 for c in job["clips"] if c.get("kept")), job_dir)
    job["job_dir"] = str(job_dir)
    return job


def _render_one(i, cand, info, transcript, scene_data, job_dir, cfg, provider,
                preset, aspect, debug_dir, cut_mod, reframe_mod, captions_mod,
                metadata_mod, scenes_mod) -> dict:
    clip_dir = job_dir / f"clip_{i:02d}"
    clip_dir.mkdir(exist_ok=True)
    start, end = cand["start"], cand["end"]

    cut_path = cut_mod.cut_clip(info["video_path"], start, end,
                                clip_dir / "cut.mp4", cfg)
    if aspect == "16:9":
        source_for_captions, metrics = cut_path, {"aspect": "16:9",
                                                  "passthrough": True}
    else:
        cuts_rel = [t - start for t in
                    scenes_mod.scene_cuts_in_range(scene_data, start, end)]
        metrics = reframe_mod.reframe_clip(
            cut_path, clip_dir / "reframed.mp4", cuts_rel, cfg,
            aspect=aspect, debug_dir=debug_dir)
        source_for_captions = clip_dir / "reframed.mp4"

    words = [{"word": w["word"],
              "start": round(max(0.0, w["start"] - start), 3),
              "end": round(max(0.0, w["end"] - start), 3)}
             for w in transcript["words"]
             if w["start"] >= start - 0.05 and w["end"] <= end + 0.05]
    final = captions_mod.caption_clip(source_for_captions, words,
                                      clip_dir / "final.mp4", cfg,
                                      preset_name=preset)

    clip_text = " ".join(w["word"] for w in words)
    meta = metadata_mod.generate_metadata(clip_text, cand["hook"], cfg, provider)
    (clip_dir / "metadata.json").write_text(json.dumps(meta, indent=2),
                                            encoding="utf-8")

    thumbs: list[str] = []
    try:
        from thumbnails import extract_thumbnails
        thumbs = [str(p) for p in extract_thumbnails(final)]
    except Exception as e:  # noqa: BLE001 — thumbnails are best-effort
        log.warning("thumbnails failed for clip %02d: %s", i, e)

    return {"index": i, "start": start, "end": end,
            "duration": round(end - start, 3),
            "hook": cand["hook"], "reason": cand.get("reason", ""),
            "candidate_score": cand.get("score", 0),
            "path": str(final), "srt": str(final.with_suffix(".srt")),
            "thumbnails": thumbs, "metadata": meta, "reframe": metrics,
            "preset": preset or cfg["captions"]["preset"], "aspect": aspect}


def _worker_count(cfg: dict, n_clips: int) -> int:
    import os as _os
    setting = cfg["render"].get("parallel_workers", "auto")
    if setting != "auto":
        return max(1, int(setting))
    return max(1, min(n_clips, (_os.cpu_count() or 2) // 2))


def _print_timings(timings: dict) -> None:
    print("\n=== per-stage timings ===")
    total = 0.0
    for name, t in timings.items():
        print(f"  {name:<14} {t['status']:<8} {t['seconds']:>8.1f}s")
        total += t["seconds"]
    print(f"  {'TOTAL':<14} {'':<8} {total:>8.1f}s\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description="ClipForge pipeline")
    ap.add_argument("source", nargs="?", help="video file or URL")
    ap.add_argument("--sample", action="store_true",
                    help="use the configured sample source")
    ap.add_argument("--provider", default=None,
                    help="override LLM provider (mock/gemini/groq/ollama)")
    ap.add_argument("--job-dir", default=None, help="resume an existing job dir")
    ap.add_argument("--force", action="store_true",
                    help="re-run stages even if completion markers exist")
    ap.add_argument("--debug", action="store_true",
                    help="persist all intermediate artifacts")
    ap.add_argument("--preset", default=None, help="caption preset name")
    ap.add_argument("--aspect", default="9:16",
                    choices=["9:16", "1:1", "16:9"])
    a = ap.parse_args(argv)

    cfg = load_config()
    if a.sample:
        from sample_source import resolve_sample
        source = str(resolve_sample(cfg))
    elif a.source:
        source = a.source
    else:
        ap.error("provide a source or --sample")

    job = run_job(source, cfg, provider=a.provider, job_dir=a.job_dir,
                  force=a.force, preset=a.preset, aspect=a.aspect,
                  debug=a.debug or None)
    kept = [c for c in job["clips"] if c.get("kept")]
    print(f"job {job['job_id']}: {len(kept)} clips kept of "
          f"{len(job['clips'])} rendered -> {job['job_dir']}")
    for c in kept:
        print(f"  [{c['weighted_score']:.2f}] {c['duration']:.0f}s "
              f"{c['metadata']['title']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
