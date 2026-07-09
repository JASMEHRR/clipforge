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
import time
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
            debug: bool | None = None, target_count: int | None = None,
            full_transcribe: bool = False, music: str | None = None,
            music_volume_db: float = -22.0, progress_cb=None,
            tracker=None, style_refine: bool | None = None,
            subs_mode: str | None = None) -> dict:
    import captions as captions_mod
    import cut as cut_mod
    import highlights as hl
    import ingest as ingest_mod
    import metadata as metadata_mod
    import music as music_mod
    import reframe as reframe_mod
    import scenes as scenes_mod
    import segment as segment_mod
    import style_refiner as style_mod
    import subtitle_detect as subs_mod
    import transcribe as transcribe_mod
    from config import config_hash
    from llm import resolve_provider

    from progress import ProgressTracker

    cfg = cfg or load_config()
    # legacy_cb keeps the existing progress_cb bar working; a caller may also
    # pass its own tracker to drive the rich progress board.
    tracker = tracker or ProgressTracker(legacy_cb=progress_cb)
    tracker.start("init", "loading configuration")
    if target_count is None:  # 0 / unset in config means "auto" (keep-ratio rule)
        target_count = cfg["clips"].get("target_count") or None
    debug = cfg.get("debug", False) if debug is None else debug
    job_dir = Path(job_dir) if job_dir else new_job_dir(cfg, source)
    job_dir.mkdir(parents=True, exist_ok=True)
    debug_dir = (job_dir / "debug") if debug else None
    fh = add_file_handler(job_dir / "job.log")
    timings: dict = {}
    notes: list[str] = []
    stages = _Stages(job_dir, force, timings)
    tracker.finish("init")
    tracker.start("deps", "verifying ffmpeg")
    verify_ffmpeg(cfg)
    resolved = resolve_provider(cfg, provider)
    tracker.finish("deps", "ffmpeg OK")
    log.info("job start: source=%s provider=%s aspect=%s dir=%s",
             source, resolved, aspect, job_dir.name)

    def _stage(name, label, fn):
        """Run a marker-cached stage while keeping the tracker in sync."""
        marker = job_dir / f".done_{name}.json"
        if marker.exists() and not force:
            result = stages.run(name, fn)   # returns cached JSON, logs skip
            tracker.skip(name)
            return result
        tracker.start(name, label)
        result = stages.run(name, fn)
        tracker.finish(name)
        return result

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
        src_name = Path(str(source)).name if not str(source).startswith("http") \
            else str(source)
        info = _stage("ingest", "downloading / normalizing input",
                      lambda: ingest_mod.ingest(
                          source, job_dir, cfg,
                          progress_cb=lambda f, msg: tracker.update(
                              "ingest", f, msg, current_file=src_name)))
        info["source_name"] = src_name          # for clip provenance display

        scene_data = _stage("scenes", "detecting shots",
                            lambda: scenes_mod.detect_scenes(
                                info["video_path"], cfg))

        # segment-first: shortlist the spans worth transcribing (long inputs)
        target = target_count or cfg["clips"]["max_candidates"]
        spans = None
        if not full_transcribe:
            spans = segment_mod.shortlist_spans(
                info["duration"], scene_data, info["audio_path"], cfg, target)
        if spans:
            notes.append(f"segment-first: transcribing {len(spans)} span(s) "
                         "instead of the whole video")

        # v2's transcribe manages the Whisper model internally (no separate
        # download/load phases) — mark those tracker stages skipped so the
        # overall percentage still reaches 100%.
        tracker.skip("model_download")
        tracker.skip("model_load")

        transcript = _stage(
            "transcribe", "transcribing audio",
            lambda: transcribe_mod.transcribe(
                info["audio_path"], cfg, spans=spans, debug_dir=debug_dir,
                progress_cb=lambda f: tracker.update(
                    "transcribe", f, f"transcribing {f * 100:.0f}%",
                    current_file="audio.wav")))
        if not transcript["sentences"]:
            notes.append("empty transcript — mechanical windows used "
                         "(passed mechanically; re-verify with a real sample)")

        candidates = _stage("highlights", "selecting highlights",
                            lambda: hl.select_highlights(
                                transcript, scene_data, info["duration"], cfg,
                                provider=provider, debug_dir=debug_dir,
                                max_candidates=target_count))
        if target_count and len(candidates) < target_count:
            notes.append(f"requested {target_count} clips but only "
                         f"{len(candidates)} candidates available")
        elif not target_count and len(candidates) <= cfg["clips"]["min_keep"]:
            notes.append(f"only {len(candidates)} candidates — all kept "
                         "(min-keep rule)")

        # style refinement: rewrite each rough window into an EditPlan (hooks,
        # pacing, endings, burned-sub handling) BEFORE rendering, so the render
        # path still produces each clip exactly once. Skipped entirely when
        # style.enabled is false — output then matches pre-feature behaviour.
        style_on = (cfg.get("style", {}).get("enabled", False)
                    if style_refine is None else style_refine)
        job["settings"]["style_refine"] = bool(style_on)
        edit_plans = None
        if style_on:
            profile = style_mod.load_profile(cfg)
            prof_tag = (profile or {}).get("name", "none")
            refine_key = config_hash(cfg, "style", "clips") + "_" + prof_tag
            marker = job_dir / ".done_refine.json"
            if marker.exists() and not force and \
                    json.loads(marker.read_text(encoding="utf-8")).get("key") == refine_key:
                edit_plans = json.loads(marker.read_text(encoding="utf-8"))["plans"]
                tracker.skip("refine")
                timings["refine"] = {"status": "skipped", "seconds": 0.0}
                log.info("stage refine: marker present — skipped")
            else:
                tracker.start("refine", "refining clip timelines")
                with stage_timer(log, "refine", timings):
                    edit_plans = []
                    for cand in candidates:
                        subs = subs_mod.detect_subtitles(
                            info["video_path"], cand["start"], cand["end"], cfg)
                        plan = style_mod.refine_clip(
                            cand, transcript, scene_data, subs, profile, cfg,
                            provider=provider, subs_mode=subs_mode)
                        edit_plans.append(plan)
                marker.write_text(json.dumps({"key": refine_key, "plans": edit_plans}),
                                  encoding="utf-8")
                tracker.finish("refine")
                notes.append(f"style refinement applied to {len(edit_plans)} clips "
                             f"(profile: {prof_tag})")

        # resolve background music once per job (one backing track), download
        # on first use; any failure disables music without failing the job
        music_path, music_attr = None, ""
        if music:
            try:
                track = music_mod.resolve(music, transcript.get("text", ""))
                if track:
                    music_path = str(music_mod.ensure_track(track))
                    music_attr = music_mod.attribution_for(track)
                    notes.append(f"background music: {track['title']} "
                                 f"({track['license']})")
            except Exception as e:  # noqa: BLE001 — music is best-effort
                notes.append(f"music disabled: {e}")
                log.warning("music setup failed: %s", e)

        clips = []
        n = max(1, len(candidates))
        workers = _worker_count(cfg, n)
        tracker.start("render", f"rendering {n} clips ({workers} workers)")
        # Seed an upfront render ETA from this machine's past throughput so the
        # UI shows an estimate before the first clip finishes.
        try:
            from history import render_rate_history
            from progress import estimate_eta
            out_secs = sum(max(0.0, c["end"] - c["start"]) for c in candidates)
            hist = render_rate_history(cfg, limit=10)
            hint = estimate_eta(out_secs / max(1, workers), history_rates=hist)
            if hint:
                tracker.set_hint("render", hint)
        except Exception as e:  # noqa: BLE001 — ETA hint is cosmetic
            log.debug("render ETA hint skipped: %s", e)
        for i in range(len(candidates)):
            tracker.item("render", f"clip_{i:02d}", 0.0)
        with stage_timer(log, "render_clips", timings):
            from concurrent.futures import ThreadPoolExecutor, as_completed
            done = 0
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(
                    _render_one, i, cand, info, transcript, scene_data,
                    job_dir, cfg, provider, preset, aspect, debug_dir,
                    cut_mod, reframe_mod, captions_mod, metadata_mod,
                    scenes_mod, music_path, music_attr, music_volume_db,
                    tracker, edit_plans[i] if edit_plans else None): i
                    for i, cand in enumerate(candidates)}
                for fut in as_completed(futures):
                    i = futures[fut]
                    done += 1
                    tracker.item("render", f"clip_{i:02d}", 1.0)
                    tracker.update("render", done / n,
                                   f"clip {done}/{n} finished",
                                   current_file=f"clip_{i:02d}/final.mp4")
                    try:
                        clips.append(fut.result())
                    except ClipForgeError as e:
                        log.error("clip %02d failed: %s — continuing", i, e)
                        notes.append(f"clip {i:02d} failed: {e}")
            clips.sort(key=lambda c: c["index"])
        tracker.finish("render")
        if not clips:
            raise ClipForgeError("no clips rendered successfully")

        tracker.start("rescore", "re-scoring rendered clips")
        with stage_timer(log, "rescore", timings):
            clips = hl.rescore_clips(clips, transcript, cfg, provider,
                                     target_count=target_count)
        tracker.finish("rescore")

        job["clips"] = clips
        job["status"] = "done"
    except Exception as e:  # noqa: BLE001 — job must record failure, not crash callers
        job["status"] = "failed"
        notes.append(f"job failed: {e}")
        log.error("job failed: %s", e)
        for row in tracker.snapshot()["stages"]:
            if row["state"] == "running":
                tracker.fail(row["key"], str(e)[:200])
    finally:
        tracker.start("cleanup", "writing job record")
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
        tracker.finish("cleanup")

    if job["status"] == "failed":
        tracker.fail("done", "failed")
        raise ClipForgeError(f"job {job['job_id']} failed", detail="; ".join(notes))
    tracker.finish("done", "completed")
    log.info("job done: %d clips (%d kept) in %s",
             len(job["clips"]),
             sum(1 for c in job["clips"] if c.get("kept")), job_dir)
    job["job_dir"] = str(job_dir)
    return job


def _render_one(i, cand, info, transcript, scene_data, job_dir, cfg, provider,
                preset, aspect, debug_dir, cut_mod, reframe_mod, captions_mod,
                metadata_mod, scenes_mod, music_path=None, music_attr="",
                music_volume_db=-22.0, tracker=None, edit_plan=None) -> dict:
    def _sub(frac: float) -> None:
        if tracker:
            tracker.item("render", f"clip_{i:02d}", frac)

    _t0 = time.perf_counter()
    clip_dir = job_dir / f"clip_{i:02d}"
    clip_dir.mkdir(exist_ok=True)
    source = info["video_path"]

    # EditPlan (when present) supplies refined segments, remapped words, caption
    # anchor/CTA/fades, and burned-sub reframe hints. Absent → today's path.
    if edit_plan:
        segments = edit_plan["segments"]
        out_start, out_end = segments[0][0], segments[-1][1]
        excl = edit_plan["existing_subs"]["bottom_exclusion_ratio"]
        hbias = edit_plan["existing_subs"]["h_bias_center"]
        multi = len(segments) > 1
    else:
        out_start, out_end = cand["start"], cand["end"]
        excl, hbias, multi, segments = 0.0, -1.0, False, [[out_start, out_end]]

    if aspect == "16:9":
        # passthrough: a frame-accurate cut is the clip (captions burn onto it)
        if multi:
            cut_path = cut_mod.cut_segments(source, segments, clip_dir / "cut.mp4", cfg)
        else:
            cut_path = cut_mod.cut_clip(source, out_start, out_end,
                                        clip_dir / "cut.mp4", cfg)
        source_for_captions, metrics = cut_path, {"aspect": "16:9",
                                                  "passthrough": True}
    elif multi:
        # cut the multi-segment concat first, then reframe the contiguous file
        # (keeps a single reframe re-encode; scene resets omitted across joins)
        cut_path = cut_mod.cut_segments(source, segments, clip_dir / "cut.mp4", cfg)
        cut_dur = reframe_mod.probe(cut_path)["duration"]
        metrics = reframe_mod.reframe_clip(
            cut_path, 0.0, cut_dur, clip_dir / "reframed.mp4", [], cfg,
            aspect=aspect, debug_dir=debug_dir, info=None,
            bottom_exclusion_ratio=excl, h_bias_center=hbias)
        source_for_captions = clip_dir / "reframed.mp4"
    else:
        # single re-encode straight from the source (no full-res intermediate)
        cuts_rel = [t - out_start for t in
                    scenes_mod.scene_cuts_in_range(scene_data, out_start, out_end)]
        metrics = reframe_mod.reframe_clip(
            source, out_start, out_end, clip_dir / "reframed.mp4", cuts_rel, cfg,
            aspect=aspect, debug_dir=debug_dir,
            info=(info if out_start == cand["start"] and out_end == cand["end"] else None),
            bottom_exclusion_ratio=excl, h_bias_center=hbias)
        source_for_captions = clip_dir / "reframed.mp4"
    _sub(0.5)

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
        cap_kwargs = captions_mod.cta_from_cfg(cfg)
    final = captions_mod.caption_clip(source_for_captions, words,
                                      clip_dir / "final.mp4", cfg,
                                      preset_name=preset, **cap_kwargs)
    _sub(0.75)

    if music_path:
        import music as music_mod
        tmp = clip_dir / "final_music.mp4"
        music_mod.add_music(final, music_path, tmp, cfg, music_volume_db)
        tmp.replace(final)
    _sub(0.85)

    duration = edit_plan["output_duration"] if edit_plan else round(out_end - out_start, 3)
    clip_text = " ".join(w["word"] for w in words)
    meta = metadata_mod.generate_metadata(clip_text, cand["hook"], cfg, provider)
    if music_attr:  # license requires attribution in the description
        meta = {**meta, "description": f"{meta['description']} {music_attr}"}

    import style_refiner as style_mod
    import virality as virality_mod
    refine_summary = style_mod.summarize(edit_plan) if edit_plan else None
    # cuts within this clip (real pacing signal for the engagement score)
    n_cuts = len(scenes_mod.scene_cuts_in_range(scene_data, out_start, out_end))
    cuts_per_min = (n_cuts / (duration / 60.0)) if duration > 0 else None
    try:
        profile = style_mod.load_profile(cfg)
    except Exception:  # noqa: BLE001 — profile is optional context
        profile = None
    vir = virality_mod.rate_virality(
        clip_text, cand["hook"], duration, cfg, provider,
        refine=refine_summary, profile=profile,
        extra={"cuts_per_min": cuts_per_min,
               "captions_enabled": (edit_plan["captions_enabled"]
                                    if edit_plan else True)})
    # Provenance: the ORIGINAL source window this clip came from, before the
    # refiner's pause-removal / hook-shift / ending-extend changed the bounds.
    # Distinct from start/end below (the final rendered window).
    orig_start, orig_end = cand["start"], cand["end"]
    source_name = info.get("source_name", "")
    payload = {**meta, "virality": vir,
               "original_source_start_s": orig_start,
               "original_source_end_s": orig_end,
               "source_name": source_name}
    if refine_summary:
        payload["style"] = refine_summary
        log.info("clip %02d refined: %s", i, json.dumps(payload["style"]))
    (clip_dir / "metadata.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8")

    return {"index": i, "start": out_start, "end": out_end,
            "original_source_start_s": orig_start,
            "original_source_end_s": orig_end,
            "source_name": source_name,
            "duration": duration,
            "render_s": round(time.perf_counter() - _t0, 2),
            "hook": cand["hook"], "reason": cand.get("reason", ""),
            "candidate_score": cand.get("score", 0),
            "path": str(final), "srt": str(final.with_suffix(".srt")),
            "metadata": meta, "virality": vir, "reframe": metrics,
            "style": payload.get("style"),
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
    ap.add_argument("--full-transcribe", action="store_true",
                    help="transcribe the whole video (disable segment-first "
                         "shortlisting)")
    ap.add_argument("--clips", type=int, default=None,
                    help="keep exactly N clips (1-20); default uses config")
    ap.add_argument("--music", default=None,
                    help="background music: 'auto' (mood match) or a track id "
                         "(see music.py --list)")
    ap.add_argument("--music-volume", type=float, default=-22.0,
                    help="background music volume in dB (default -22)")
    ap.add_argument("--zip", action="store_true",
                    help="also write a clips_bundle.zip of kept clips")
    ap.add_argument("--no-style", action="store_true",
                    help="disable the style refinement stage (config style.enabled)")
    ap.add_argument("--subs-mode", default=None,
                    choices=["auto", "replace", "keep", "ignore"],
                    help="burned-in subtitle handling (default: config style.existing_subs.mode)")
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
                  debug=a.debug or None, target_count=a.clips,
                  full_transcribe=a.full_transcribe, music=a.music,
                  music_volume_db=a.music_volume,
                  style_refine=False if a.no_style else None,
                  subs_mode=a.subs_mode)
    kept = [c for c in job["clips"] if c.get("kept")]
    print(f"job {job['job_id']}: {len(kept)} clips kept of "
          f"{len(job['clips'])} rendered -> {job['job_dir']}")
    for c in kept:
        print(f"  [{c['weighted_score']:.2f}] {c['duration']:.0f}s "
              f"{c['metadata']['title']}")
    if a.zip and kept:
        from bundle import zip_job
        print(f"bundle -> {zip_job(job['job_dir'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
