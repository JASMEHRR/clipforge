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
- New Settings tab. Compute = auto/gpu/cpu, honored by `transcribe.model_config`
  (Whisper device) and `ffutil.video_encode_args` (encoder). Detected hardware
  shown (CUDA / NVENC / CPU cores). Whisper model picker (blank = matrix
  default, else tiny->large-v3 via `whisper.model_override`) and LLM
  provider+model fields.
- Persistence is **non-destructive**: `save_config()` writes only the changed
  keys to `config.local.yaml` (gitignored), which `load_config()` deep-merges
  over `config.yaml` on load. This keeps `config.yaml`'s comments intact
  instead of rewriting it with a comment-stripping yaml dump.

## Overnight run 2026-07-09 (feature/overnight-upgrades)
- **Branch base**: feature/style-refiner was green (123 passed, 1 skipped;
  sample pipeline OK), but `origin/main` had diverged (NVENC probe, ETA-log,
  gitignore commits) and merging it produced a 12-hunk conflict in pipeline.py.
  Resolving a divergent merge that touches core pipeline logic unattended risks
  corrupting decision behavior, which the protocol forbids. DECISION: build on
  the known-green feature/style-refiner via new branch feature/overnight-upgrades;
  **defer main reconciliation with origin/main to a human** (see REPORT.md).
- Deleted stray root file `tatus` (accidental `git status >` redirect artifact).


## Viral detection v2 (feature/viral-v2, 2026-07-10)
- **Multimodal behind the existing LLM ladder**: `complete_json` gained an
  optional `media=` list of provider-neutral parts instead of a parallel entry
  point, so retry -> JSON repair -> schema validation apply to video/image
  calls unchanged. Gemini file handles and base64 images are the two part
  kinds; groq/ollama reject media with a clean LLMError.
- **Privacy is a hard gate, not a preference**: `viral_v2.allow_upload`
  defaults to false and blocks BOTH the Gemini chunk upload and the OpenRouter
  frame extraction for local files (frames are the video content). URL sources
  are exempt (already public). Audio DSP is fully local and always runs, so a
  blocked run still yields events.
- **Free-tier survival**: per-chunk results cached by chunk-hash + prompt-hash
  + provider (a resumed 6-hour job re-pays nothing); `max_daily_minutes`
  enforced from cache/viral_v2_usage.json; quota errors demote gemini ->
  openrouter -> audio-only instead of failing the job.
- **DSP baseline uses median/MAD (with a 5%-of-median floor), not mean/std** —
  during testing a loud tone inflated the mean/std enough to mask a real
  laughter burst (z fell to ~2). Robust stats keep the threshold anchored to
  the quiet bed.
- **Reaction boundary rule runs after sentence snapping** because snapping
  would revert the extension; ending on the reaction deliberately breaks
  sentence bounds.
- **actors_hint is treated as a presence signal only**: the reframe pins to
  the face the existing MediaPipe tracking already found at that moment; if
  tracking saw no face there, no cut happens. Re-identifying "the man on the
  left" would need a new model, which the constraints forbid.
