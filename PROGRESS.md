# PROGRESS.md — requirement checklist + Known Issues

Next task: **see "Current" below.** Legend: [x] done · [~] in progress · [ ] pending · [!] see Known Issues

Current: **feature/ui-rework** merged to main — UI rework + branding/fonts/provenance/updater verification complete (see Phase U below). Remaining manual items unchanged (Docker daemon build; optional YouTube OAuth).

## Phase G — GPU fix (feature/gpu-fix)
- [x] Diagnosed with hard evidence (GPU-DIAGNOSTIC.md), not the assumed "detection bug". Encoding fell back because system **ffmpeg 8.1.2 links NVENC SDK 13.1 (needs driver ≥610)** but the installed GTX 1650 driver is **581.08 (NVENC API 13.0)** — the ffmpeg binary is newer than the driver supports. `nvenc_available()` was already correct (real smoke encode); only defect was the silent fallback. Transcription already ran on GPU (ctranslate2 CUDA + cuBLAS/cuDNN present).
- [x] `ffutil.ffmpeg_bin()`/`ffprobe_bin()` — resolvable ffmpeg (CLIPFORGE_FFMPEG env → config `ffmpeg.binary` → PATH); all ffmpeg/ffprobe calls route through them. Driver-compatible gyan.dev **ffmpeg 7.1** installed to `tools/ffmpeg-7.1/` (gitignored), selected via `config.local.yaml`.
- [x] No silent fallbacks — `nvenc_available()` + `transcribe.gpu_available()` log the specific reason; new **`check_gpu.py`** self-reports GPU health in plain language.
- [x] `tests/test_nvenc.py` — probe logic unit-tested with subprocess mocked (5 cases).
- [x] Proof: `encoder: h264_nvenc (GPU)`, **NVENC encoder util peaked 100%** during render (61 samples >0%), render stage **66.1s GPU vs 231.1s CPU (~3.5×)**; transcribe `device=cuda large-v3 float16`, GPU compute 100% / ~3.4GB.

## Phase U — UI rework + branding/fonts/provenance (feature/ui-rework)
- [x] AUDIT.md — every Feature-5 option traced end to end; found CTA text silently dropped without style refinement (cap_kwargs={}); fixed via shared `captions.cta_from_cfg` in pipeline.py + rerender.py; pacing made honest (label + gr.Warning, no fake wiring)
- [x] Provenance — `original_source_start_s`/`_end_s` + `source_name` persisted in clip record + metadata.json; cards show `Source: mm:ss–mm:ss`
- [x] Logo watermark — `captions.watermark.mode` off|text|image (backward compatible); image mode = single-pass scale2ref+overlay -filter_complex (alpha-aware, audio fade routed through graph); uploads persist to assets/user_branding/ (gitignored); validated via ffprobe frame extraction (magenta logo present top-right, absent bottom-right)
- [x] fontreg.py — real family names via fonttools; validates/rejects non-fonts; fonts_dir resolves bundled-only (unchanged) or combined cache/fonts_all when user fonts exist; assets/user_fonts/ gitignored
- [x] style_preview.py — one still frame through the exact write_ass + FFmpeg subtitles burn; cached per (preset, font, text); shared by preset + font pickers; proven two fonts render differently (Impact vs Montserrat pixel diff)
- [x] UI — accent theme + CSS (theme/css at launch per Gradio 6); "Style & Branding" section; font-gallery popup (gr.Column overlay; no gr.Modal in 6.19) with real-burn previews; per-card "Edit this clip" via @gr.render → Edit tab pre-loaded (desired-clip State avoids the job-change clobber); allowed_paths serves previews/branding
- [x] scripts/screenshot_ui.py (dev-only, playwright) → design/screenshots/ 01–06: themed Create, font gallery, History, card gallery w/ provenance + Edit buttons, Edit tab pre-loaded
- [x] updater — tests/test_updater.py sandboxed apply/preserve-config/reject-broken/rollback (monkeypatch updater.ROOT); live read-only check reaches GitHub; UPDATER-STATUS.md from observed behaviour
- [x] verify: pytest 164 passed / 1 skipped; `--sample --provider mock` with image logo confirmed via frame extraction; font override confirmed via caption-region pixel diff
- [!] Known: font-gallery previews are generated at page load for the active preset (cached after first time; 4 ffmpeg frames). style_preview positions the sample for the strip via anchor; color/highlight/stroke/font are production-exact. Live-network updater delta/download path guarded by integrity checks but not exercised against real GitHub (see UPDATER-STATUS.md).

## Phase S — Style refiner (feature/style-refiner)

## Phase S — Style refiner (feature/style-refiner)
- [x] schemas: STYLE_PROFILE, SUBTITLE_DETECT_RESULT, EDIT_PLAN, HOOK_CLASSIFY, ENDING_CLASSIFY (caption anchor clamped [0.52,0.66] at schema level); StyleError; config `style:` block
- [x] subtitle_detect.py — OpenCV burned-in band detector, cached, font-free synthetic self-test
- [x] style_profile.py — reference analyzer (ingest/transcribe/scenes reuse); profiles/default.json; frames to cache/style_frames/
- [x] style_refiner.py — start fixer, pacing cleaner, ending optimizer, word-timeline remap, existing-subs ladder → EditPlan; 13 pure-logic tests
- [x] cut.py cut_segments() — concat filter + declick fades; single-segment fast path unchanged
- [x] reframe.py — bottom_exclusion_ratio (REPLACE) + h_bias_center (KEEP), no-op by default
- [x] captions.py — CAPTION POSITION LAW (\pos anchor), CTA overlay, KEEP no-caption, envelope fades + zoom; legacy path byte-identical
- [x] pipeline.py refine stage (marker-cached, profile+config-hashed) + _render_one EditPlan wiring; run_job style_refine/subs_mode; --no-style/--subs-mode CLI
- [x] rerender.py mirrors refinement for edited bounds
- [x] app.py Create-tab: style toggle, profile dropdown, subs-mode selector
- [x] verify: pytest green (123 passed / 1 skipped); `--sample --provider mock` style-on (EditPlan summaries, no gap>max_pause, durations in bounds, anchor in band); `--no-style` skips stage; analyzer on refs/ → profiles/user.json activated + re-verified
- [!] Known: multi-segment reframe drops scene-cut smoothing resets across concat joins (joins are removed silence — low risk). Burned-sub KEEP cannot preserve subs wider than the 9:16 crop (physical limit). subtitle_detect can fire on on-screen title cards (e.g. the sample film) → REPLACE; tune persistence_ratio/max_band_ratio if over-eager.
- [!] Env: this venv had corrupted installs (protobuf, gradio, fsspec, google-api-python-client missing files) repaired via `pip install --force-reinstall --no-deps`; keep protobuf==4.25.9 (mediapipe needs <5).

## Phase 0 — Scaffold
- [x] git init + local identity (builder/builder@local), GIT_TERMINAL_PROMPT=0
- [x] PLAN.md written before code (architecture, interfaces, contracts, failure modes, perf, deps, Decisions)
- [x] Python 3.11 available (winget 3.11.9) + .venv created
- [x] config.yaml (provider auto, model matrix, clip bounds, caption defaults, sample block)
- [x] .env.example
- [x] schemas.py — all JSON contracts + validators
- [x] llm.py — mock/gemini/groq/ollama, lazy imports, retry+backoff, JSON repair (groq/ollama via requests HTTP — no SDK needed; gemini via google-genai lazy)
- [x] logging setup (per-stage timings)
- [x] scripts/make_synthetic_sample.py (Windows TTS narration; sine fallback)
- [x] Sample source selected + HEAD-verified + sha256 recorded (Duck and Cover 1951, archive.org, 9m15s) + 2 mirrors
- [x] OFL font bundled (Montserrat TTFs + OFL.txt in assets/fonts)
- [x] GATE 0 PASSED: `import llm` w/o provider SDKs; mock valid for all 7 schemas; auto→mock keyless; initial commit

## Phase 1 — Core pipeline
- [x] ingest.py (+smoke) — file/URL → normalized mp4 + 16k wav
- [x] transcribe.py (+smoke) — words/sentences, disk cache by file+model+config hash
- [x] scenes.py (+smoke)
- [x] highlights.py (+smoke) — LLM scoring, schema validation, retry→repair→rule-based fallback, 30–60s, sentence snapping, hook-first-3s criteria
- [x] cut.py (+smoke) — frame-accurate re-encode
- [x] reframe.py (+smoke) — face+mesh mouth-variance, motion fallback, center fallback, EMA+lookahead+velocity clamp, scene resets, 1080x1920, measurable smoothness, DEBUG frames
- [x] captions.py (+smoke) — ASS karaoke, 3–4 words/line, 220px margin, bundled font via fontsdir
- [x] metadata.py (+smoke) — strict JSON, template fallback always valid
- [x] pipeline.py — orchestration, markers/resume/--force, --sample (mirrors→synthetic), --provider
- [x] rescore: weighted score, drop bottom 30%, keep ≥3 (note if ≤3 candidates)
- [x] app.py — Gradio bg threads, live progress, ranked gallery, download
- [x] GATE 1 PASSED (5 kept clips, tagged phase-1): ≥3 vertical captioned clips + valid metadata; Gradio bg/poll/kill; sentence bounds; smoothness pass; DEBUG frames checked. Tag phase-1.

## Phase 2 — Creator features
- [x] A. 4 caption presets + per-run UI dropdown + .srt export
- [x] B. Batch queue (multi-line + /inbox watcher), per-job status, failure isolation
- [x] C. Clip edit & re-render (sentence snap; single-clip re-render; regenerate metadata button)
- [x] D. Thumbnails ×3 per clip (sharp, face, mid-action)
- [x] E. YouTube upload: full impl, mocked-API unit tests, creds-absent guidance, private default, quota handling, no OAuth in build
- [x] F. Aspect options 9:16 / 1:1 / 16:9 pass-through
- [x] GATE 2 PASSED (tagged phase-2): batch of 2; presets render; targeted re-render; thumbnails; upload guidance + mocked tests. Tag phase-2.

## Phase 3 — Hardening & release
- [x] A. Parallel clip rendering; NVENC auto (x264 fallback); per-stage timing table
- [x] B. SQLite job history + UI History tab
- [x] C. pytest suite (73 tests) (snapping, durations, chunking, schemas, fallback scorer, metadata template, min-keep, smoothness, JSON failure paths, retry/fallback, idempotency)
- [x] D. Dockerfile + compose + run.sh/run.bat + pinned requirements.txt
- [x] E. README (per-OS setup, Gemini key, YouTube OAuth walkthrough, config ref, troubleshooting) + final CLAUDE.md
- [x] F. Final QA PASSED: clean venv (.venv_qa from pinned requirements, pip check clean), caches cleared, keyless gate-1+2 re-run green (see output/gate3.log)
- [x] GATE 3 PASSED: pytest green (74 tests) in clean venv; ffmpeg pin verified; docker static-validated (no daemon on host — container run pending manual verification); tagged v1.0.0

## Global standards (verified at each gate)
- [x] Central schemas.py used everywhere; no undefined JSON
- [x] Per-module logging (inputs, summarized outputs, timings)
- [x] DEBUG artifact persistence
- [x] Structured errors; pipeline never dies on one clip/LLM failure
- [x] Idempotent stages (markers, --force, hash-keyed cache under /cache)
- [x] Timestamped job folders; no overwrites
- [x] LLM retry + backoff everywhere
- [x] Chunked/streaming processing for large files
- [x] No silent failures
- [x] Prompting standard (task, constraints, schema, example)

## Overnight upgrades (feature/overnight-upgrades, 2026-07-09)
- [x] Feature 1 — progress + ETA everywhere (pipeline + single-clip rerender; per-clip render_s persisted; history-based render ETA)
- [x] Feature 2 — UI card gallery (band badge + expandable engagement breakdown, render time, refine flags); History reopen renders it; Soft theme
- [x] Feature 3 — auto-open on launch (config-driven, chromeless app window w/ tab fallback; command construction unit-tested, no browser opened)
- [x] Feature 4 — virality v2 (6 explainable engagement sub-scores + band; reuses refiner flags; keep logic unchanged)
- [x] Feature 5 — per-run options: CTA text, highlight color, watermark, pacing slider, clip length (config-driven, threads through pipeline + rerender)
- [ ] Feature 5 — emoji captions CUT (bundled Montserrat has no emoji glyphs; needs an emoji-capable font asset — see REPORT.md)
- [x] Verification: 144 passed / 1 skipped; `--sample --provider mock` OK (metadata carries breakdown, render_s recorded); app + rerender smoke pass

## Known Issues
- Docker daemon absent on build host → Dockerfile/compose statically validated only; container run "pending manual verification" (rule 6).
- mediapipe pinned to 0.10.14: 0.10.35 removed the legacy `solutions` API (rule 6 substitution, documented in PLAN.md Decisions).
- ~~pip protobuf conflict~~ RESOLVED: google-api-core pinned to 2.24.2 (accepts protobuf 4.x alongside mediapipe 0.10.14); `pip check` clean; clean-venv install from requirements.txt verified.
- google-genai SDK is intentionally NOT installed in the build venv (gate 0 requires `import llm` with zero provider SDKs; the missing-SDK path raises a clean LLMError and is unit-tested). It IS pinned in requirements.txt so end-user installs get Gemini support out of the box.
- Sample film is 320×240 (archive.org 512kb derivative) → vertical output is upscaled; fine for gates, users should feed ≥720p sources for quality.
- Host FFmpeg is 8.1.2 (winget gyan.dev build), verified at setup; Docker image pins its own FFmpeg.

## Performance (measured, CPU-only: 4 cores, no GPU)
- 9m15s sample, from scratch (no caches), keyless: transcribe 213s + scenes 15s + render 736s (medium preset) = 16.1 min → over budget.
- Fix: final encode preset medium → veryfast (crf 19): caption burn 85s → 33s per clip (2.6×); projected total ≈ 9.5 min → a 10-min 1080p video completes well under the 15-min CPU target.
- Cached re-runs (transcript+scenes hit): ~12s to candidate selection; renders dominate.
