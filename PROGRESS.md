# PROGRESS.md — requirement checklist + Known Issues

Next task: **see "Current" below.** Legend: [x] done · [~] in progress · [ ] pending · [!] see Known Issues

Current: Phase 1 — core pipeline modules.

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
- [ ] ingest.py (+smoke) — file/URL → normalized mp4 + 16k wav
- [ ] transcribe.py (+smoke) — words/sentences, disk cache by file+model+config hash
- [ ] scenes.py (+smoke)
- [ ] highlights.py (+smoke) — LLM scoring, schema validation, retry→repair→rule-based fallback, 30–60s, sentence snapping, hook-first-3s criteria
- [ ] cut.py (+smoke) — frame-accurate re-encode
- [ ] reframe.py (+smoke) — face+mesh mouth-variance, motion fallback, center fallback, EMA+lookahead+velocity clamp, scene resets, 1080x1920, measurable smoothness, DEBUG frames
- [ ] captions.py (+smoke) — ASS karaoke, 3–4 words/line, 220px margin, bundled font via fontsdir
- [ ] metadata.py (+smoke) — strict JSON, template fallback always valid
- [ ] pipeline.py — orchestration, markers/resume/--force, --sample (mirrors→synthetic), --provider
- [ ] rescore: weighted score, drop bottom 30%, keep ≥3 (note if ≤3 candidates)
- [ ] app.py — Gradio bg threads, live progress, ranked gallery, download
- [ ] GATE 1 keyless: ≥3 vertical captioned clips + valid metadata; Gradio bg/poll/kill; sentence bounds; smoothness pass; DEBUG frames checked. Tag phase-1.

## Phase 2 — Creator features
- [ ] A. 4 caption presets + per-run UI dropdown + .srt export
- [ ] B. Batch queue (multi-line + /inbox watcher), per-job status, failure isolation
- [ ] C. Clip edit & re-render (sentence snap; single-clip re-render; regenerate metadata button)
- [ ] D. Thumbnails ×3 per clip (sharp, face, mid-action)
- [ ] E. YouTube upload: full impl, mocked-API unit tests, creds-absent guidance, private default, quota handling, no OAuth in build
- [ ] F. Aspect options 9:16 / 1:1 / 16:9 pass-through
- [ ] GATE 2 keyless: batch of 2; presets render; targeted re-render; thumbnails; upload guidance + mocked tests. Tag phase-2.

## Phase 3 — Hardening & release
- [ ] A. Parallel clip rendering; NVENC auto (x264 fallback); per-stage timing table
- [ ] B. SQLite job history + UI History tab
- [ ] C. pytest suite (snapping, durations, chunking, schemas, fallback scorer, metadata template, min-keep, smoothness, JSON failure paths, retry/fallback, idempotency)
- [ ] D. Dockerfile + compose + run.sh/run.bat + pinned requirements.txt
- [ ] E. README (per-OS setup, Gemini key, YouTube OAuth walkthrough, config ref, troubleshooting) + final CLAUDE.md
- [ ] F. Final QA: clean venv, keyless gate-1+2 re-run, walk this checklist
- [ ] GATE 3: pytest green; docker static-validated (no daemon on host); tag v1.0.0

## Global standards (verified at each gate)
- [ ] Central schemas.py used everywhere; no undefined JSON
- [ ] Per-module logging (inputs, summarized outputs, timings)
- [ ] DEBUG artifact persistence
- [ ] Structured errors; pipeline never dies on one clip/LLM failure
- [ ] Idempotent stages (markers, --force, hash-keyed cache under /cache)
- [ ] Timestamped job folders; no overwrites
- [ ] LLM retry + backoff everywhere
- [ ] Chunked/streaming processing for large files
- [ ] No silent failures
- [ ] Prompting standard (task, constraints, schema, example)

## Known Issues
- Docker daemon absent on build host → Dockerfile/compose statically validated only; container run "pending manual verification" (rule 6).
- Sample film is 320×240 (archive.org 512kb derivative) → vertical output is upscaled; fine for gates, users should feed ≥720p sources for quality.
- Host FFmpeg is 8.1.2 (winget gyan.dev build), verified at setup; Docker image pins its own FFmpeg.
