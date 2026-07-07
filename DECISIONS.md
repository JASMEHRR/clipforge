# ClipForge v2 — build decisions log

Mode 1 (autonomous build). Sensible defaults chosen where the spec was open;
each choice logged here.

## Global
- Branch `v2.0`, one commit per upgrade (`v2: <name>`).
- Universal (Win/Mac/Linux, GPU/CPU): pathlib + `shutil.which`, CPU fallbacks kept.
- Existing architecture preserved: flat modules, `schemas.py` validation at
  boundaries, typed errors, `logutil` logging, stage completion markers.

## Upgrade 1 — Speed
- **Segment-first transcription** is the headline win. New `segment.py`:
  reads the 16 kHz `audio.wav` directly with the stdlib `wave` module (no extra
  ffmpeg pass), computes per-second RMS energy, and combines that with scene
  boundaries to shortlist candidate spans (~3x the target clip count). Only
  those spans are transcribed.
- Pipeline reordered: ingest -> scenes -> shortlist -> transcribe(spans) ->
  highlights -> render. Scenes now run before transcribe (cheap, needed for
  the shortlist).
- `transcribe()` gains a `spans` arg. Each span is cut from the wav with a fast
  `-ss`-before-`-i` copy, transcribed, and its word/sentence times offset back
  to absolute. Sentences are built per-span so grouping never bridges a gap.
  Cache key includes the spans hash. `spans=None` keeps the old whole-audio
  path (used by `rerender.py`, which reuses the cached transcript).
- Whisper: added `condition_on_previous_text=False` (spec). Model matrix, VAD,
  beam=1 already matched the spec.
- **Reframe on a 360p proxy**: MediaPipe tracking now runs on a small proxy
  (`scale=-2:360`, reduced fps) generated with a fast seek; crop coords scale
  back to full res by width ratio and are interpolated by timestamp. The final
  render is a **single** re-encode straight from the source
  (`-ss` before `-i` + crop/scale filter), replacing the old cut+reframe
  double full-res encode. Frame-accurate because we re-encode from source.
- **Known limitation:** because only shortlisted spans are transcribed, the Edit
  tab's manual re-cut to bounds *outside* a transcribed span will have no
  caption words there. Acceptable tradeoff for the speed win; whole-video
  transcription is still available via `--full-transcribe`.
- `cut.py` gains a `mode="copy"` fast stream-copy trim, used only for the 16:9
  passthrough tracking-free path where captions re-encode anyway.

## Upgrade 2 — Remove thumbnails
- Deleted `thumbnails.py`, its pipeline call + `thumbnails` clip key, the app
  Gallery, `scripts/gate2.py` thumbnail asserts, and doc mentions. No config
  keys existed for thumbnails.

## Upgrade 3 — Clip count selector
- New `clips.target_count` config key (default 0 = auto/legacy keep-ratio rule).
- `--clips N` CLI flag, UI slider 0-20 (0 = Auto, kept so the keep-ratio rule
  stays reachable from the UI). When N>0, exactly N clips are kept (or all
  available with a note when fewer exist). Threaded through `run_job` ->
  `select_highlights(max_candidates=N)` + `rescore_clips(target_count=N)`.

## Upgrade 4 — Virality rating
- New `virality.py`: 0-100 score from hook strength (LLM, first ~3s), speech
  pacing, emotional-keyword density, and length sweet spot (~35-50s). LLM
  returns `{score, verdict, reasons[]}`; deterministic rule-based fallback for
  keyless mode. New `virality` schema. Saved into each clip's `metadata.json`
  under a `virality` key and onto the clip dict. UI badge + sort by score.

## Upgrade 5 — Background music
- New `music.py` + `assets/music/manifest.json`. CC0/verified tracks only, one
  license record per track. First-use download from the pinned CC0 source URLs.
  ffmpeg mix: sidechain duck under speech, -22 dB default, 1 s fades,
  loop/trim to clip length. Auto-match picks a mood bucket from the transcript.
  Attribution appended to the description when the license requires it.

## Upgrade 6 — Bulk download
- `zip_job(job_dir)` helper zips every kept clip's final.mp4 + .srt +
  metadata.json. "Download all" button on Create + History, "Download
  everything" on Batch, and a `--zip` CLI flag.

## Upgrade 7 — GPU + model selection UI
- New Settings tab. Compute = Auto/Force GPU/Force CPU, persisted to
  `config.yaml` (`render.compute`), honored by `transcribe` + encoder.
  Detected hardware shown. Whisper model picker (tiny->large-v3) and LLM
  provider+model picker persisted to config. `save_config()` added to
  `config.py` (round-trips yaml, preserves the singleton).
