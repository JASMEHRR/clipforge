# ClipForge overnight run — REPORT

**Branch:** `feature/overnight-upgrades` (pushed to origin after every commit)
**Base:** `feature/style-refiner` (see "Branch decision" below)
**Date:** 2026-07-09 (autonomous overnight run)
**Final state:** 144 passed / 1 skipped; `--sample --provider mock` completes; app + rerender smoke tests pass.

---

## CHECK THESE 3 THINGS FIRST (morning)

1. **`main` was NOT updated.** `origin/main` had diverged from `feature/style-refiner`
   (NVENC-probe, ETA-logging, gitignore commits) and merging it produced a **12-hunk
   conflict in `pipeline.py`**. Resolving core pipeline logic unattended risked
   corrupting the highlight/refiner decision behavior (protocol forbids), so all work
   was built on `feature/style-refiner` instead. **You need to reconcile
   `feature/overnight-upgrades` ↔ `origin/main` by hand** (the conflicts are in
   pipeline.py / progress.py / transcribe.py / setup_env.py). Their `progress.py` ETA
   logging overlaps my ETA work — review before merging.
2. **Auto-open now defaults ON** (`ui.auto_open: true`). Launching `app.py` (or
   double-clicking `run.bat`) will open a chromeless Edge/Chrome window automatically.
   If you don't want that, set `ui.auto_open: false` in `config.yaml` (or Settings).
   It was **never actually opened during the build** (verified by unit-testing the
   command construction only).
3. **Emoji captions were CUT** (the one un-built item from the candidate list). The
   only bundled font (Montserrat) has no emoji glyphs, so burned emoji would render as
   tofu boxes, and no new font dependency is allowed. Decide whether to add an
   emoji-capable font asset if you want this.

---

## Per-feature status

| Feature | Status | Notes |
|---|---|---|
| 1 — Progress + ETA everywhere | **DONE** | Pure `estimate_eta`/`ema`; history-based render ETA hint; per-clip `render_s` persisted; `rerender_clip(tracker=)` streams live progress; Create + Edit tabs show ETA. |
| 2 — UI upgrade (card gallery) | **DONE** | Card gallery with band badge + expandable engagement breakdown, render time, refine-flag chips; History reopen renders the same gallery + total-time column; `gr.themes.Soft()`. |
| 3 — Auto-open on launch | **DONE** | `launcher.py`: browser detection, chromeless `--app` window, tab fallback, port-poll before open; config `ui.auto_open`/`window_mode`; command construction unit-tested (no browser opened). |
| 4 — Virality v2 | **DONE** | 6 explainable engagement sub-scores (hook/completeness/pacing/captions/duration/delivery) → 0-100 + band; reuses refiner flags + StyleProfile; optional LLM rubric on a real key; keep logic untouched. |
| 5 — New user options | **DONE (6 of 7 candidates)** | CTA text, keyword highlight color, watermark text+position, pacing aggressiveness, clip length min/max, background music (already present) — all config-driven, thread through pipeline + rerender. **Emoji captions CUT** (font glyphs). |

## Branch decision (important)

`feature/style-refiner` was verified green (123 passed on entry; sample pipeline OK) but
was **not merged to main** because `origin/main` had independently advanced. The merge
conflicted heavily in `pipeline.py`. Per protocol ("never modify decision behavior",
"never stall"), I aborted the merge, branched `feature/overnight-upgrades` off the
known-green `feature/style-refiner`, and deferred main reconciliation to a human. Also
deleted a stray root file `tatus` (an accidental `git status >` redirect artifact).

## Research summary (RESEARCH.md)

Studied Opus Clip / Submagic / Klap / Vizard feature sets and published short-form
retention guidance. Key inputs used tonight:
- Hook in the first ~3s is the dominant retention lever (~71% decide early) → highest
  weight (0.28) in virality v2.
- Duration sweet spot is a 30–60s plateau (peak ~45s); sub-10s underperforms → duration
  sub-score centered at 45s.
- 85% watch muted → captions/readability matters (caption sub-score).
- Opus Clip's own virality score is publicly criticized as unreliable → v2 is framed as
  **explainable engagement signals with a visible breakdown, never a guarantee.**
- SELECTED 7 user options (built 6, cut emoji); REJECTED heavy/external ideas (AI B-roll,
  SFX, auto-posting, translation, dubbing) — all need new services/deps.

## UI changes (what the user will see)

- **Create tab**: progress line now shows a live **ETA**; results are a **card gallery**
  (was a markdown table) — each card shows rank, title, a colored **band badge**
  (Strong/Promising/Weak · score), a meta row (`⏱ duration · ★ quality · ⚙ rendered in Xs · 🎬 preset`),
  red **refine-flag chips** (e.g. weak_hook), and an expandable **"engagement signals"**
  section with a 0–10 bar + one-line reason per signal.
- **Create → "More options" accordion**: Custom CTA text, keyword highlight color picker,
  pacing aggressiveness slider, min/max clip length, watermark text + position.
- **Edit tab**: single-clip re-render now streams a **live progress bar + ETA** and reports
  "rendered in Xs".
- **History tab**: new **total-time** column; "Open job" now renders the full **card gallery**
  for that past job (not just a file list).
- Theme switched to Gradio **Soft**; card styling injected as a scoped `<style>` block.

## Exact commands (verification)

```
cd E:\imp\projects\clipforge
.venv\Scripts\python.exe -m pytest -q                               # 144 passed, 1 skipped
.venv\Scripts\python.exe pipeline.py --sample --provider mock       # completes; metadata carries virality.signals; per-clip render_s recorded
.venv\Scripts\python.exe virality.py                                # engagement-signals self-check
.venv\Scripts\python.exe launcher.py                                # pure launch-command self-check (no browser)
.venv\Scripts\python.exe app.py                                     # UI on http://127.0.0.1:7860 (auto-opens a window)
```

New tests: `tests/test_progress_eta.py`, `tests/test_virality_v2.py`,
`tests/test_launcher.py`, `tests/test_run_options.py`.

## Files changed (high level)

- `progress.py` — `ema`, `estimate_eta`, `_Stage.hint_eta`, `set_hint`.
- `history.py` — `render_rate_history`.
- `pipeline.py` — per-clip `render_s`; render ETA hint; richer virality inputs.
- `rerender.py` — optional `tracker`; per-clip `render_s`.
- `virality.py` — v2 engagement signals (pure sub-scores + composer + LLM blend).
- `config.py` — `apply_run_options`, `hex_to_ass`.
- `captions.py` — `watermark_filter` + burn-time watermark.
- `launcher.py` — NEW (auto-open).
- `app.py` — card gallery, ETA display, streaming rerender, "More options", history reopen, theme.
- `schemas.py` — optional `band` + `signals` on the virality schema.
- `config.yaml` — `captions.watermark`, `music`, `ui` sections.
- `run.bat` — auto-open comment.
- Docs: `README.md`, `PLAN.md`, `PROGRESS.md`, `CLAUDE.md`, `RESEARCH.md`, `DECISIONS.md`.

## Known issues / follow-ups

- **Emoji captions (cut)**: needs an emoji-capable font in `assets/fonts/` and a
  `fontsdir`/fallback strategy in `captions.py`. A soft-caption (.srt-only) emoji path is
  possible without a font, but the burned video is the product, so it was skipped.
- **Per-run options in the Edit tab**: the Edit/rerender path honors the same config keys
  (CTA, highlight color, watermark, pacing) via the shared render code, but the Edit tab
  does not (yet) expose per-run override controls of its own — set them in Settings if you
  want them applied to a re-render. (Create-tab per-run overrides apply to the full run.)
- **Delivery sub-score is neutral (5.0) under the current pipeline**: audio energy variance
  is not yet wired from the wav into the score (graceful neutral default). Wiring
  `segment.py`'s per-second RMS in would make it a live signal — left as a clean follow-up.
- **Sample video is 320×240** (as before) — don't judge visual quality by it.
- Gradio 6 emits a one-line deprecation warning if theme is passed to the Blocks
  constructor; theme is now passed at `launch()` and CSS via `<style>` — warning resolved.
