# Feature 5 / Feature 2 Audit

Every per-run option claimed in `REPORT.md` was traced end to end from the
Gradio control through `config.apply_run_options` into the render/config path.
Evidence is cited as `file:line`. Status is one of **WIRED** (works today),
**BROKEN** (control exists but the value never reaches output on the common
path), or **MISSING** (never built).

| Item | Status | Evidence / chain |
|------|--------|------------------|
| CTA text field → `style.cta.text` | **BROKEN → FIXED** | `app.py:587` → `_run_generator` `app.py:143` → `config.py:137-141`. Consumed only via `edit_plan` in `pipeline.py:378-382`; the no-refine `else` branch set `cap_kwargs={}` (`pipeline.py:389`), so CTA was silently dropped whenever Style Refinement was off. Same gap in `rerender.py:159`. Fixed by passing `cfg["style"]["cta"]` through the no-refine branch of both files. |
| Keyword highlight color → caption preset | **WIRED** | `app.py:591` → `config.py:143-146` (`hex_to_ass`) → `captions.py:128,160-162`. Mutates the active preset dict the run renders with. |
| Watermark (text) → rendered output | **WIRED** | `app.py:604-610` → `config.py:165-171` → `captions.py:254-256` (`watermark_filter` drawtext in the `-vf` chain). |
| Pacing slider → `style.max_pause_s` / `target_pause_s` | **PARTIAL (by design)** | `app.py:594` → `config.py:148-155` → `style_refiner.py:191-194`. The only consumer is the style refiner's `compress_pauses`; there is no non-refine consumer to wire it to. Left honest: slider relabelled "needs Style Refinement" and a `gr.Warning` fires if it is used with refinement off, rather than faking an effect. |
| Clip length → `clips.min_seconds` / `max_seconds` | **WIRED** | `app.py:597-603` → `config.py:157-163` → `highlights.py:169,194-195` (unconditional). |
| Background music → `music.py` | **WIRED** | It is a dropdown (`app.py:567`), not a toggle as REPORT's wording implied. `app.py:189-197` → `pipeline.py:222-233,395-399` (`music_mod.resolve`/`add_music`). |
| Card gallery Edit button | **MISSING → BUILT (Phase E)** | Cards are inert HTML (`_clip_card` `app.py:75-106`) rendered into `gr.HTML` (`app.py:614`); no per-card action. The Edit tab (`app.py:650-673`) is an independent job/clip dropdown selector — nothing carried a clip id from a card into it. |
| Original (pre-refine) source bounds in metadata | **MISSING → BUILT (Phase B)** | Candidate `cand["start"]/end` are overwritten by the refiner (`style_refiner.py:318-401`) and never persisted; `metadata.json` payload (`pipeline.py:424-429`) has no timestamps. Refined bounds live only in `job.json`. |

## Behavior-change note (CTA fix)
`style.cta.enabled` defaults to `true` in `config.yaml`, and the refine path
already rendered the default "Follow for more" CTA. The fix makes the
no-refine path consistent: runs with Style Refinement off now also render the
CTA when `style.cta.enabled` is true. This is the intended consistent behavior
and is what makes the CTA-text field actually take effect on the common path.
