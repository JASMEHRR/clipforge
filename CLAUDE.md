# CLAUDE.md — ClipForge project guide

**Resume pointer: current phase and next task → see top of PROGRESS.md.** Architecture/decisions → PLAN.md.

## What this is
Local Opus Clip alternative: long video → ranked 30–60s vertical clips with karaoke captions + metadata. Zero-touch build; ALL gates run keyless (`--provider mock`, no .env).

## Commands
- venv python: `.venv/Scripts/python.exe` (Python 3.11.9 — REQUIRED exactly 3.11; installed via winget)
- Full run: `.venv/Scripts/python.exe pipeline.py --sample --provider mock`
- UI: `.venv/Scripts/python.exe app.py` (background + poll http://127.0.0.1:7860, never foreground in build)
- Tests: `.venv/Scripts/python.exe -m pytest -q`

## Conventions
- Flat top-level modules (ingest.py, transcribe.py, …, pipeline.py, app.py); schemas in schemas.py only.
- All JSON validated against schemas.py before crossing module boundaries.
- Structured errors from errors.py; a failing clip/stage never kills the pipeline or queue.
- Stage completion markers: `<job>/.done_<stage>.json`; `--force` overrides; cache keys = input sha256 + config hash.
- Outputs: `output/<YYYYmmdd-HHMMSS>_<slug>/clip_NN/`. Cache: `cache/` (hashed dirs). Never commit output/, cache/, samples/, .env.
- LLM: only via `llm.complete_json(task, schema, prompt, context=...)`. Mock provider is first-class and deterministic.
- Fonts: bundled Montserrat in assets/fonts; ffmpeg subtitles filter with `fontsdir` — never system fonts.

## Gotchas
- Windows host: ffmpeg path args in filters need escaping (`C\:` and `/` separators inside subtitles filter).
- Git identity is local (builder/builder@local); GIT_TERMINAL_PROMPT=0.
- No Docker daemon on this host — static validation only (see PROGRESS.md Known Issues).
- Sample film is 320×240; don't judge visual quality by it.
- Never run interactive commands, browsers, or OAuth flows during the build (spec rules 3–5).
