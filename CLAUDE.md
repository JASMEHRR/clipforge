# CLAUDE.md — ClipForge project guide

**Resume pointer: current phase and next task → see top of PROGRESS.md.** Architecture/decisions → PLAN.md and DECISIONS.md. User docs → README.md.

Release state: v1.0.0 and v1.1.0 tagged on main. Current work lives on branch **v2.0** (pushed to origin), which contains all v2 features PLUS the v1.1 progress tracker and self-updater merged in (commit 672a081). main is still at v1.0.1 — do not assume main is current. Next milestone: merge v2.0 → main and tag v2.0.0 (bump VERSION file so the self-updater offers it).

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

## Conventions
- Flat top-level modules; schemas in schemas.py only; all JSON validated at module boundaries.
- Structured errors from errors.py; a failing clip/stage never kills the pipeline or queue.
- Stage completion markers: `<job>/.done_<stage>.json`; `--force` overrides.
- Outputs: `output/<YYYYmmdd-HHMMSS>_<slug>/clip_NN/`. Never commit output/, cache/, samples/, tools/, .env, config.local.yaml.
- LLM only via `llm.complete_json(...)`. Mock provider deterministic and first-class.
- Fonts: bundled Montserrat in assets/fonts via ffmpeg `fontsdir` — never system fonts.
- User (PJ) tests manually — no automated test suites or full-pipeline validation runs unless explicitly asked. Minimal exact fixes; one short question when ambiguous, never silent guessing on ambiguity; commit after each completed change.

## Gotchas
- MULTIPLE-CLONE HISTORY: this repo previously existed in several folders and work got lost/confused. There is exactly ONE canonical clone now. Always verify `git remote -v` shows JASMEHRR/clipforge and ALWAYS push after committing.
- Windows host: ffmpeg path args in filters need escaping (`C\:` and `/` separators inside subtitles filter).
- venv breaks if the folder is moved across drives — rebuild it (py -3.11 -m venv .venv && pip install -r requirements.txt).
- Sample film is 320×240; don't judge visual quality by it.
- backup branch `backup-v2-working` = pre-merge v2 state, keep until v2.0.0 ships.
