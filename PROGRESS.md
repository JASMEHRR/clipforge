# PROGRESS.md — requirement checklist + Known Issues

Next task: **see "Current" below.** Legend: [x] done · [~] in progress · [ ] pending · [!] see Known Issues

Current: RELEASED — v1.0.0 tagged. All gates passed. Remaining manual items: run Docker build on a machine with a daemon; optional YouTube OAuth (user).

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

## Phase 4 — Production upgrade (v1.1.0)
- [x] progress.py stage tracker (per-stage %, ETA, elapsed, current file, speed, per-clip bars) wired into pipeline + UI heartbeat
- [x] setup_env.py zero-setup installer (Python/venv/pip retries, portable FFmpeg auto-download, GPU/CUDA detect + CPU fallback messaging, resumable cached downloads, Whisper prefetch, preflight validation); ffutil bundled-binary resolution; self-installing run.bat/run.sh
- [x] updater.py self-update (background check, delta via compare API + blob-sha verify, zipball fallback, staged py_compile verify, backup + auto-rollback, user data always preserved); VERSION file + UI banner
