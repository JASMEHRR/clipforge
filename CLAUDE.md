# CLAUDE.md — ClipForge project guide

**Resume pointer: current phase and next task → see top of PROGRESS.md.** Architecture/decisions → PLAN.md and DECISIONS.md. User docs → README.md.

Release state: v1.0.0 and v1.1.0 tagged. The v2 line (segment-first transcribe, virality, music, updater) plus the overnight per-run options and card gallery are now merged into **main** (VERSION 1.1.0). The **feature/ui-rework** branch (merged to main) adds the UI rework + branding/fonts/provenance + updater verification — see PROGRESS.md "Phase U". New modules: `fontreg.py` (font registry + fontsdir), `style_preview.py` (real-burn caption/font previews), `scripts/screenshot_ui.py` (dev-only playwright). New docs: AUDIT.md, UPDATER-STATUS.md, design/screenshots/.

Watermark now supports `captions.watermark.mode` off|text|image (image = single-pass logo overlay). Caption font is a per-run override (`apply_run_options` `font_family`). User uploads persist to `assets/user_branding/` and `assets/user_fonts/` (both gitignored). Next milestone: tag v2.0.0 (bump VERSION so the self-updater offers it).

## What this is
Local Opus Clip alternative: long video → ranked 30–60s vertical clips with karaoke captions + metadata. Keyless operation is first-class (`--provider mock`, no .env needed).

## v2 features (all on branch v2.0)
- Segment-first transcription (segment.py): scenes detected first, only candidate windows transcribed on long videos; `--full-transcribe` disables. Stage order is scenes → transcribe (NOT transcribe → scenes like v1).
- Clip count selector: `--clips N` / UI slider / `clips.target_count` config (0 = auto).
- Virality rating (virality.py): 0–100 score + post/maybe/skip verdict per clip, LLM with deterministic rule-based fallback; stored in metadata.json, shown as badge in UI.
- Copyright-free background music (music.py + assets/music/manifest.json): CC-BY tracks, auto-duck under speech, attribution auto-added to description. `--music auto|<id>`, `--music-volume`.
- Bulk download (bundle.py): per-job zip, batch "download everything", `--zip` CLI.
- GPU + model selection: Settings tab (compute auto/gpu/cpu, Whisper model picker, LLM provider+model); persists to config.local.yaml (gitignored — never write these to config.yaml).
- Thumbnails feature is REMOVED. Nothing may import or reference thumbnails.py.

## v1.1 infrastructure (merged into v2.0)
- progress.py ProgressTracker: pipeline stages wrapped in tracker.start/finish/skip/update/item; run_job takes tracker=None and defaults to ProgressTracker(legacy_cb=progress_cb) — the simple UI bar is driven via legacy_cb. The rich render_text() board is NOT wired into the Create tab (deliberate; possible follow-up). model_download/model_load stages are skip()'d (v2 transcribe has no phase hooks).
- updater.py self-updater: background GitHub check on launch, banner + Install button in UI. REPO = "JASMEHRR/clipforge". Compares VERSION file against GitHub release tags — tagging releases is what makes updates appear.
- setup_env.py zero-setup installer + VERSION file.

## Commands
- venv python: `.venv/Scripts/python.exe` (Python 3.11 REQUIRED exactly — MediaPipe wheels)
- Full run: `.venv/Scripts/python.exe pipeline.py <source> --clips 6 --music auto --zip`
- Keyless sample: `.venv/Scripts/python.exe pipeline.py --sample --provider mock`
- UI: `.venv/Scripts/python.exe app.py` (http://127.0.0.1:7860; if port busy, set GRADIO_SERVER_PORT)

## Style refiner (feature/style-refiner)
- Analyze reference Shorts → profile: `.venv/Scripts/python.exe style_profile.py refs/ --name user` (also accepts video files and URLs); set `style.profile: profiles/user.json` in config.yaml to use it. Frames land in `cache/style_frames/` for hand-tuning the JSON.
- Run with refinement (on by default via `style.enabled`): `pipeline.py --sample --provider mock`
- Pick burned-subtitle handling: `pipeline.py <src> --subs-mode auto|replace|keep|ignore`
- Disable refinement (reproduces pre-feature output): `pipeline.py <src> --no-style`
- Re-render one clip honoring refinement: `rerender.rerender_clip(job_dir, i, start, end)` (pass `style_refine=False` to skip).
- Module self-checks: `python subtitle_detect.py` (synthetic smoke), `python style_refiner.py` (EditPlan invariants).
- refs/ is gitignored (reference videos may be copyrighted). The refine stage lives between highlights and render; `style.enabled: false` skips it entirely.

## Overnight upgrades (feature/overnight-upgrades)
- **Progress + ETA**: `progress.estimate_eta`/`ema` (pure, tested); `history.render_rate_history` feeds an upfront render ETA (`tracker.set_hint`); per-clip `render_s` persisted in job.json; `rerender_clip(..., tracker=)` streams stage progress; Create + Edit tabs show live ETA.
- **Virality v2**: `virality.engagement_signals(features)` → 6 sub-scores + band (Strong/Promising/Weak); reuses refiner flags + StyleProfile; optional LLM rubric only on a real provider. Display/sort only — keep logic unchanged.
- **Per-run options**: `config.apply_run_options(cfg, opts)` (pure deep-copy; never mutates the singleton) — CTA text, highlight color (`config.hex_to_ass`), pacing slider, clip length, watermark (`captions.watermark_filter`). Threads through pipeline AND rerender via shared render-time config keys.
- **Auto-open**: `launcher.py` (`build_launch_command`, `detect_browser`, `open_ui`); config `ui.auto_open`/`ui.window_mode`. Tests assert command construction only — never opens a browser.
- **UI**: card gallery (`app._cards_html`) with band badge + expandable engagement breakdown, replaces the markdown table; History reopen renders the same gallery. `gr.themes.Soft()` passed at `launch()` (Gradio 6 moved theme/css off the Blocks constructor); card CSS injected as a `<style>` block.
- Branch note: built on feature/style-refiner because origin/main had diverged (12-hunk pipeline.py conflict). **main reconciliation deferred to a human** (see REPORT.md).

## Viral detection v2 (feature/viral-v2)
- video_events.py owns the per-job event timeline (schema `event_timeline`, absolute seconds): Gemini chunk uploads (Files API via `llm.upload_media`) → OpenRouter frame batches (free Qwen VL, model id in `llm.openrouter_model`) → local audio DSP (always, keyless too). Merge: overlapping/within `viral_v2.merge_gap_s` → union span, max intensity.
- PRIVACY GATE: local files are never uploaded unless `viral_v2.allow_upload: true` (default false); `source_type == "url"` is exempt. Audio DSP always runs (fully local).
- Free-tier guards: per-chunk cache `cache/viral_events/<chunk_sha16>_<prompt_sha8>_<provider>.json` (resumable); daily quota `cache/viral_v2_usage.json` vs `viral_v2.max_daily_minutes`; quota/429 → next provider → audio-only. The events stage never fails the job.
- Multimodal LLM: `llm.complete_json(..., media=[...])` — parts `{"kind":"gemini_file","handle":...}` / `{"kind":"image","mime":...,"data":bytes}`; groq/ollama reject media; mock ignores it (canned `viral_events` branch).
- Fusion (highlights.py): `fuse_event_scores` (additive only) → `apply_reaction_boundaries` (end-on-the-reaction; never start mid-event) run AFTER sentence snapping, before dedupe; `event_cluster_candidates` when transcript < `viral_v2.sparse_wpm`. `select_highlights(events=None)` is byte-identical to pre-feature (tested).
- Reframe: `reframe_clip(..., event_cuts_rel=[{"t","actors_hint"}])` hard-cuts to the tracked face with `viral_v2.min_shot_s` hysteresis (`event_cut_bounds`, pure); no detected face at that moment → no cut. Pipeline remaps event times through EditPlan segments (`_remap_to_output`).
- Pipeline stage `events` (after transcribe): bespoke marker `.done_events.json` keyed `config_hash(cfg,"viral_v2","llm")+provider`; `viral_v2.enabled: false` skips everything. Clip metadata.json gains `events`.
- Commands: keyless gate `pipeline.py --sample --provider mock` (canned events flow to metadata); transcript-only run: set `viral_v2.enabled: false` (config.local.yaml or UI checkbox); live needs GEMINI_API_KEY (+ optional OPENROUTER_API_KEY).

## Conventions
- Flat top-level modules; schemas in schemas.py only; all JSON validated at module boundaries.
- Structured errors from errors.py; a failing clip/stage never kills the pipeline or queue.
- Stage completion markers: `<job>/.done_<stage>.json`; `--force` overrides.
- Outputs: `output/<YYYYmmdd-HHMMSS>_<slug>/clip_NN/`. Never commit output/, cache/, samples/, tools/, .env, config.local.yaml.
- LLM only via `llm.complete_json(...)`. Mock provider deterministic and first-class.
- Fonts: bundled Montserrat in assets/fonts via ffmpeg `fontsdir` — never system fonts.
- User (PJ) tests manually — no automated test suites or full-pipeline validation runs unless explicitly asked. Minimal exact fixes; one short question when ambiguous, never silent guessing on ambiguity; commit after each completed change.

## GPU (feature/gpu-fix)
- ffmpeg binary is resolvable: `ffutil.ffmpeg_bin()`/`ffprobe_bin()` = `CLIPFORGE_FFMPEG`/`CLIPFORGE_FFPROBE` env → config `ffmpeg.binary`/`ffprobe_binary` → PATH. This machine's system ffmpeg 8.1.2 links NVENC SDK 13.1 (needs driver ≥610) but the driver is 581.08 (API 13.0), so its NVENC won't init — a driver-compatible **ffmpeg 7.1** in `tools/ffmpeg-7.1/` (gitignored) is selected via `config.local.yaml` (gitignored). The `nvenc_available()` probe was already correct; the bug was only a silent fallback.
- No silent GPU fallbacks: `nvenc_available()` and `transcribe.gpu_available()` log the specific reason. `check_gpu.py` self-reports GPU health (`.venv/Scripts/python.exe check_gpu.py`). Evidence in GPU-DIAGNOSTIC.md.

## Gotchas
- MULTIPLE-CLONE HISTORY: this repo previously existed in several folders and work got lost/confused. There is exactly ONE canonical clone now. Always verify `git remote -v` shows JASMEHRR/clipforge and ALWAYS push after committing.
- Windows host: ffmpeg path args in filters need escaping (`C\:` and `/` separators inside subtitles filter).
- venv breaks if the folder is moved across drives — rebuild it (py -3.11 -m venv .venv && pip install -r requirements.txt).
- Sample film is 320×240; don't judge visual quality by it.
- backup branch `backup-v2-working` = pre-merge v2 state, keep until v2.0.0 ships.
- `cut.py` clamps segment bounds to the source's real probed duration — style_refiner's `extend_forward` and other EditPlan producers are not trusted to stay within it themselves.
