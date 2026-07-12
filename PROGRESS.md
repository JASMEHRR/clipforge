# PROGRESS.md — requirement checklist + Known Issues

Next task: **see "Current" below.** Legend: [x] done · [~] in progress · [ ] pending · [!] see Known Issues

Current: **main** — Phase M (All-clips library tab + uploaded archive + zip backups) complete, all 3 parts. Next: merge/tag v2.0.0 (feature/frontend-rebuild is already on main so this is just the version bump), double-caption fix follow-up (Phase D Known), Docker daemon build, optional YouTube OAuth.

## Phase M — All-clips library tab + uploaded archive + zip backups (on main) [x]
Three-part task, extends Phase L's delete-from-app work (reused, not duplicated). All 3 parts done.
- [x] Part 1 — All-clips tab (d1a6bbb): `GET /api/clips/all` in `routes_library.py` scans every job/clip on disk regardless of status, cached after first scan (`?refresh=1` or any status-changing route forces a rescan — delete/approve/reject/exclude/upload-now/sync-schedule/unschedule/cleanup-uploaded all call `invalidate_all_clips_cache()`, added after code review caught the first pass only invalidating on delete). Status derived from `upload_scheduler.classify_uploads` (same source as the YouTube tab, not a re-derived date check) + per-clip approval. New `web/js/library.js`: sort by date/virality/size, chunked rendering via IntersectionObserver sentinel (no virtual-scroll dependency) for 500+ clips, per-job "select all," multi-select delete reusing `DELETE /api/clips`. `thumbButton` moved from `upload_queue.js` into shared `ui.js`.
- [x] Part 2 — Uploaded archive (9389ac5): new `archive.py` — `archive/uploaded/<YYYY-MM>/<video_id>__<slug>/` (final.mp4 + info.json + info.txt), idempotent per video_id. `upload_scheduler.upload_one`/`upload_now` archive the just-uploaded file (inside their own try block, before the watermarked temp copy is deleted in `finally` — the only point the exact uploaded file exists) via a shared `_archive_after_upload` best-effort helper. One-time backfill (`POST /api/archive/backfill`, "Archive older uploads" button) covers older `upload_log.json` entries from whatever's still in `output/`. `cleanup_uploaded` now archives (via `archive.ensure_archived`) before deleting, skipping anything that can't be archived. `POST /api/archive/open/{video_id}` (`os.startfile`) + an "Open folder" button on published rows; `/api/youtube/queue` exposes `archived` via `archive.index_by_video_id()` (one directory walk, not one glob per row — code review caught the first pass globbing per row). `archive/` added to `.gitignore`. **Gotcha for any future test touching an upload path**: `archive.ARCHIVE_DIR`/`archive.ROOT` are module-level constants — must be monkeypatched per test (see `tests/test_archive.py`'s `_isolate` fixture and `test_server_api.py`'s `_isolate_upload_scheduler`) or the test silently writes into the real repo's `archive/` folder — this happened once during development, cleaned up, isolation added to every fixture that can reach it.
- [x] Part 3 — Zip backups (b3db69e): `archive.create_backup_zip` → `archive/backups/clipforge_backup_<date>_<n>of<n>.zip`, streamed via `ZipFile.write` per file, verified (member count + `testzip()`) before the manifest updates or anything is deleted; a failed verify removes the broken zip and changes nothing. `zip_manifest.json` tracks which zip holds which video_ids (`find_zip_for`); `zip_status()` drives a 100-clip prompt banner in the YouTube tab plus an always-available "Zip archive now" button, both through a background job (`POST /api/archive/zip` + poll) since a big zip is too slow to block a request on. `delete_originals` opt-in (default off, checkbox in the confirm dialog). Published rows show "in backup \<name\>" once a clip's local folder is gone. **Concurrency gotcha**: `create_backup_zip` is wrapped in a module-level `_ZIP_LOCK` — two racing zip requests (the banner and the manual button are independently clickable) would otherwise compute the same output filename and one would truncate the other's in-progress zip; caught by a dedicated 4-thread test. Code review + a manual visual check also caught: a verify/IO failure was reaching the UI as "done" instead of "error" (frontend only checked job state, not the error payload); `_load_manifest`'s corrupt-file quarantine used `Path.rename`, which raises on Windows if a `.corrupt` file from a prior event already exists (now `Path.replace` + a fallback); and a zipped-but-kept clip (delete unchecked) was losing its "Open folder" button in favor of "in backup X" even though the folder was still on disk.

## Phase L — Upload center: library mgmt + schedule-ahead + approval (on main)
Four-part task (task file "Library Management + Schedule-Ahead Uploads + Approval Flow"). All scheduling/upload paths stay single-source in `upload_scheduler.py`; the UI adds states, not logic forks.
- [x] Part 2 — Auto-organize by niche (committed 4b28c7b, prior session): classifier + pipeline tag + backfill + library filter.
- [x] Part 4 — Approval flow (8cd4674): PENDING APPROVAL state; `upload.approval` in metadata.json; `upload_scheduler.approval_ok` gates every upload path through the shared `find_candidates`; `require_approval` (config + Settings toggle, default ON) decides pending-eligibility, rejected never uploads. Routes: `/api/youtube/approvals`, `approvals/all`, per-clip approval PUT. Awaiting-approval section atop the queue.
- [x] Part 1 — Delete from the app (96b01cf): per-clip / batch / "Clean up uploaded" + Storage line. `delete_clip_dir` (rmtree + prune job.json, **never touches the upload log** so a deleted-but-uploaded clip stays deduped/ineligible); refuses a clip mid-upload (`key_is_uploading`). `DELETE /api/clips`, `GET /api/storage`, `POST /api/youtube/cleanup-uploaded`.
- [x] Part 3 — Schedule-ahead (a5799dc): "Sync schedule" pre-books open horizon slots as private+publishAt uploads (laptop can be off). `sync_schedule` stops at the first of quota / open slots / approved clips; `quota_status` honest math (~1600 units/upload, 10k/day → ~6/day); `next_publish_times(slots_per_day=)` spreads a batch across days; `classify_uploads` scheduled-vs-published (live `video_status` refines known ids, unknown fall back to the publishAt clock); `unschedule` (deletes the private upload, refuses already-published). `youtube_upload.delete_video`/`video_status`. UI: Sync button + quota line, Scheduled/Published panels, Un-schedule.
- [x] Dry-run guard (133fa17): this dev machine has live YouTube OAuth, so upload/sync endpoints run against the real channel. `CLIPFORGE_DRY_RUN=1` is now a hard floor in `youtube_upload.py` (build_service→sentinel; upload_clip→fake DRYRUN video; delete_video/video_status no-op) that no client flag can override; dry-run uses a separate `cache/upload_log.dryrun.json` so it never pollutes real dedupe/quota. UI shows a dry-run banner. Use it for any local smoke test that might POST an upload/sync endpoint.

## Phase A — Analytics tab + platform cross-post research
- [x] `PLATFORMS.md` — research-only decision doc for Instagram/TikTok/Pinterest auto-posting; recommends manual-staging now, Pinterest API first (shortest review path), Instagram next (needs Business-account conversion + Meta App Review), TikTok last (needs a post-audit before public posts are possible). No code written for this part.
- [x] Analytics tab (read-only): `analytics.py` (fetch/cache, reuses `youtube_upload.build_analytics_service` — the `yt-analytics.readonly` scope was already granted during the upload-flow build, no re-auth needed) + `analytics_insights.py` (pure recommendation engine: topic/length/timing/hook signals, each recommendation carries its exact evidence numbers) + `server/routes_analytics.py` (`GET /api/analytics/state`, `PUT /api/analytics/publish-slot`) + `web/js/charts.js` (dependency-free inline-SVG sparkline/bar charts) + new Analytics nav tab in `web/js/app.js`.
- [x] `youtube_upload.authorized()` — new shared helper (credentials_available + has_cached_token) used by routes_upload.py, routes_analytics.py, and analytics.py's background refresh, replacing three independent copies of the same check.
- [x] Fixed during review: publish-hour timezone bug (JS was reading local browser time instead of IST), `_hook_signal`'s weak/strong classifications could contradict each other on a low-retention channel, `refresh()`'s cache-freshness check could raise on a malformed cache file instead of degrading gracefully.
- [x] Proof: 299 tests green (`tests/test_analytics.py`, `tests/test_analytics_insights.py`); `scripts/screenshot_analytics.py` (dev-only) + manual click-through confirm the not-configured/not-authorized/authorized branches and the "Apply publish slot" round-trip all work against a real running server.
- [!] Known: this dev machine has YouTube OAuth configured but the YouTube Analytics API isn't enabled in its Google Cloud project (real 403 from Google, unrelated to this code) — enable it at https://console.developers.google.com/apis/api/youtubeanalytics.googleapis.com to see live data.

## Phase F — Frontend rebuild (feature/frontend-rebuild)
- [x] P1 FastAPI backend: `server/` routes over pipeline/rerender/upload_scheduler/batch/updater, WS progress fan-out (jobs.py RunHandle), cancel via Event between stages, run.bat "new" flag; 12 offline API tests. Gate: full mock run via curl only.
- [x] P2 design system: web/css/tokens.css (monochrome + #D71921 accent, strict type scale, spring motion), app.css components, dots.js canvas dot-matrix (progress + mini variants), Doto/Inter/JetBrains Mono bundled (OFL), design/styleguide.html.
- [x] P3 core loop: home (hero input, drag&drop, options disclosure) → dot-matrix progress (plain-language stages, ETA, cancel, restart fallback) → results gallery (inline video, virality mini-dots, keep/discard via new PUT kept endpoint, exclude, zip). Gate: keyless mock run driven wholly in-UI; p3_01–04 screenshots at 1920×1080 + 1280×800.
- [x] P4 parity: pickers.js preview-first modals (caption/font real burns + upload, music listen, position, shape, subs, profile), clip editor (snap, re-render w/ inline dots via shared WS, regen metadata), queue (batch + inbox + zip-all endpoint), YouTube center (connect/auto/slots/recent), settings (provider/models/speed/whisper + updater), history cards. Gate: p4_01–15 incl. every modal OPEN, editor re-render driven end-to-end; 266 tests.
- [x] P5 retire Gradio: app.py + gradio tests + gradio dev scripts deleted, gradio pins dropped from requirements.txt, run.bat/run.sh/Dockerfile → server.main, port 7860, launch update-toast, README rewritten with new screenshots.
- [x] deploy/huggingface removed entirely (described the retired Gradio Space; never actually deployed — PJ's call 2026-07-11).
- [!] Known: per-run provider override and viral_v2.allow_upload have no UI control (Settings-level provider; privacy gate stays default-off). scripts/screenshot_ui.py and gate1.py deleted with app.py.

## Phase D — Double-caption false-negative fix (feature/double-caption-fix)
- [x] subtitle_detect.py — root-cause fix for the false-negative this ticket was filed against: `persistence_ratio` was checked against a *single global average* over the whole clip range, which dilutes a genuinely-visible burned caption that only recurs for a few seconds at a time (per-line/intermittent captions) below threshold. Fixed via a `window_seconds` (config `style.subtitle_detect.window_seconds`, default 6.0) sliding-window candidate search, ANDed with a stricter acceptance rule: the candidate band must either (a) hold continuously for >= persistence_ratio of the whole range (old behavior, unchanged for continuous captions), or (b) recur across >= 2 separate bursts each >= 2s (new — catches per-line captions that cycle on/off). A single one-off burst (an incidental on-screen object holding still for a few seconds — found during verification: a lit marquee sign in clip_00 of the diagnosis job false-positived under a naive "any window passes" version of this fix) satisfies neither and is correctly rejected. Regression tests: `tests/test_subtitle_detect.py` (synthetic intermittent-caption clip, synthetic one-off-burst clip, decision-ladder integration).
- [x] pipeline.py — hard invariant: after final render, if `existing_subs` decision was replace/keep, `subtitle_detect.verify_no_leftover_subs` re-scans the FINAL rendered clip (same detector, uncached) for a text-like band outside ClipForge's own caption/CTA zone (derived from `caption_anchor`); if found, logs a structured warning and records it on `metadata.json`'s `style.double_caption_warning` rather than silently shipping a double-captioned clip. Regression tests cover: leftover flagged, own-caption band correctly ignored, clean render passes.
- [!] Known: could NOT visually confirm the exact clip from the reported screenshot ("25. Oh yeah." double caption) in job `output/20260710-144449_url`. Initial diagnosis flagged clip_02 (existing_subs=none, matching caption text) as the likely culprit and it was RE-RENDERED with `--provider mock` before its original final.mp4 was visually verified — a process mistake (should have extracted/inspected the original frame first). On closer inspection the raw SOURCE frame at the exact "25. Oh yeah" timestamp (source ~3315.5s) shows no burned-in text at all; "25." is Whisper transcribing the spoken words "I'm 25" at a caption-line boundary, not a source subtitle. Contact-sheet review of all 9 other clips in the job (all existing_subs modes) shows single, clean captions — no double-caption artifact anywhere in the currently available data. The subtitle_detect false-negative this ticket describes is real and fixed (verified via synthetic regression tests + no regression on any of the 9 intact real clips), but the specific reported screenshot's clip is unaccounted for — needs the original screenshot's source file/timestamp or a fresh non-mock run to pin down.

## Phase R — Revamp v2: pickers, music UI, auto-upload panel, polish (feature/double-caption-fix)
- [x] Popup card-pickers extended from the font gallery to 5 more selectors (shared `_static_picker_modal`): caption preset (real burn previews via style_preview), output shape / existing-subs / watermark position (illustrated plain-language cards), style profile (summary card from profile JSON). Value flow unchanged (`gr.State` feeds `_run_generator`).
- [x] Music UI: preview-first gallery (shared gr.Audio player, lazy track download, Auto/Random/None), per-batch music dropdown with 'random' resolved to a concrete track per job at enqueue (`_resolve_music_choice`). music.py untouched.
- [x] Auto-upload panel on the YouTube tab: `upload_scheduler.panel_state` (pure, tested) → toggle (persists upload.auto_enabled to config.local.yaml), authorize refresh, uploads today vs cap, next slot, recent youtu.be links; per-clip "Don't auto-upload" checkbox on result cards writes metadata.json upload.exclude, respected by find_candidates.
- [x] Polish: `_friendly()` one-line errors (full tracebacks → cache/logs/ui.log via logutil.add_file_handler), plain-language provider/hardware/transcription labels, yt-dlp label removed, modal cards no longer flex-stretch.
- [x] Proof: 252 tests green; combined render (captions+CTA+watermark+music, mock) frame-verified + attribution in description; exclude toggle verified against live find_candidates; design/screenshots 07–14.

## Phase Y — YouTube auto-upload integration (feature/double-caption-fix)
- [x] `upload_scheduler.py` — ported the standalone auto_upload.py behavior unchanged into a ClipForge module: candidate scan of `output/*/clip_*/metadata.json`, dedupe vs `cache/upload_log.json` (atomic writes, corrupt-log recovery), `min_virality` gate, junk-hashtag cleaning (+`#shorts`), IST publish slots with collision avoidance, daily/per-run caps, ntfy.sh notifications.
- [x] Event-driven hook: `trigger_after_render` in pipeline.py after clip finalize (guards: auto_enabled + authorized + daily cap; never raises into render). `watch` polling mode kept as fallback. `upload.py` CLI: default/dry/watch/report.
- [x] youtube_upload.py — publish_at + category_id params, analytics scopes + `build_analytics_service` (peak-hours scheduling is a deliberate stub; static config slots used today).
- [x] config.yaml `upload:` block; README plain-language setup + daily-operation docs.
- [x] tests/test_upload_scheduler.py — discovery/dedupe, hashtag cleaning, slot collisions, cap counting, corrupt-log recovery, trigger_after_render guards; all Google calls mocked, offline.

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

## Viral detection v2 (feature/viral-v2, 2026-07-10)
- [x] Module 1 — video_events.py: Gemini chunk upload path (Files API, wait-ACTIVE), OpenRouter free-Qwen-VL frame-batch fallback, local audio DSP (robust median/MAD energy spikes + laughter-like bursts), 2s merge/dedupe, per-chunk hash cache (resumable), daily-minutes quota guard, privacy gate (allow_upload default false, URLs exempt)
- [x] Module 2 — fusion: additive event scoring + end-on-the-reaction / never-start-mid-event boundary rules (post-snap, pre-dedupe), event-cluster candidates for sparse/silent sources; pipeline `events` stage with config-hash-keyed marker; events in metadata.json
- [x] Module 3 — reaction-aware reframe: event hard cuts with min_shot_s hysteresis (event_cut_bounds, pure), pin to tracked face only when tracking saw a face; EditPlan-segment time remap
- [x] Module 4 — UI (viral toggle + allow-upload privacy checkbox, event line on clip cards), docs (README, CLAUDE.md, PLAN.md Decisions, DECISIONS.md)
- [x] Keyless verification: 215 passed / 1 skipped; `--sample --provider mock` end-to-end OK (.done_events.json written, mock+audio events merged, event visible in clip metadata)
- [ ] LIVE verification: pending live test (no GEMINI_API_KEY in build env) — run a public YouTube URL through the events stage, confirm timestamps/quota logging, reaction-rule end-extension in metadata, event_cuts in reframe metrics

## Known Issues
- Docker daemon absent on build host → Dockerfile/compose statically validated only; container run "pending manual verification" (rule 6).
- mediapipe pinned to 0.10.14: 0.10.35 removed the legacy `solutions` API (rule 6 substitution, documented in PLAN.md Decisions).
- ~~pip protobuf conflict~~ RESOLVED: google-api-core pinned to 2.24.2 (accepts protobuf 4.x alongside mediapipe 0.10.14); `pip check` clean; clean-venv install from requirements.txt verified.
- google-genai SDK is intentionally NOT installed in the build venv (gate 0 requires `import llm` with zero provider SDKs; the missing-SDK path raises a clean LLMError and is unit-tested). It IS pinned in requirements.txt so end-user installs get Gemini support out of the box.
- Sample film is 320×240 (archive.org 512kb derivative) → vertical output is upscaled; fine for gates, users should feed ≥720p sources for quality.
- Host FFmpeg is 8.1.2 (winget gyan.dev build), verified at setup; Docker image pins its own FFmpeg.
- ~~gemini SDK errors (503/429/etc.) bypassed complete_json's retry loop~~ RESOLVED: `_gemini_complete`/`upload_media` raised raw google-genai exceptions, uncaught by the loop's `(LLMError, SchemaValidationError, ValueError)` filter — a single transient 503 killed the whole job. Fixed via `llm._classify_gemini_error` wrapping SDK errors into `LLMError(retryable=...)`; confirmed live: job survived two real consecutive 503s on `highlight_candidates` with backoff between attempts instead of crashing.
- ~~viral_v2 Gemini video chunking never succeeded live~~ RESOLVED: `ffutil._run_with_progress` sliced the ffmpeg cmd at index 3 (before `-v error`'s value), injecting `-stats_period` as a bogus loglevel — every chunk() call crashed and silently fell back to audio-only. Fixed by slicing at index 4; confirmed live: chunk uploaded to Gemini Files API and returned 6 real timestamped viral_events.
- ~~cut.mp4 could come out with zero streams~~ RESOLVED: neither `cut_clip` nor `cut_segments` validated segment bounds against the source's real probed duration — an EditPlan `extend_forward` bound (or any other producer) past the true container duration made ffmpeg's `trim`/`atrim` filters emit nothing for that span, and the muxer can drop the stream entirely. Fixed by clamping every segment to `probe(video_path)["duration"]` in `cut.py` (the shared chokepoint for both paths) before building the ffmpeg command, plus an explicit post-cut `has_audio` check alongside the existing duration-tolerance check so a broken cut raises `CutError` for that clip only (pipeline already skips a failed clip and continues).
- ~~Gemini SDK calls could hang indefinitely~~ RESOLVED: `genai.Client(...)` had no timeout configured, so a stalled connection blocked the whole pipeline (one `hook_classify` call logged 634s with no retry activity). Fixed with `http_options=types.HttpOptions(timeout=45_000)` (45s) on the client; `_classify_gemini_error` already treated timeout exceptions as retryable, so this closes the gap without any new classification logic.

## Performance (measured, CPU-only: 4 cores, no GPU)
- 9m15s sample, from scratch (no caches), keyless: transcribe 213s + scenes 15s + render 736s (medium preset) = 16.1 min → over budget.
- Fix: final encode preset medium → veryfast (crf 19): caption burn 85s → 33s per clip (2.6×); projected total ≈ 9.5 min → a 10-min 1080p video completes well under the 15-min CPU target.
- Cached re-runs (transcript+scenes hit): ~12s to candidate selection; renders dominate.
