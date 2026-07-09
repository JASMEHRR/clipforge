# ClipForge — PLAN.md

Self-hosted video repurposing tool: long video in → ranked, captioned, vertical 30–60s clips out.
Zero-touch build; every gate runs keyless (mock LLM provider + deterministic fallbacks).

## 1. System architecture

```
                       ┌────────────────────────────────────────────────┐
                       │                 app.py (Gradio UI)             │
                       │  upload/URL · batch queue · progress · gallery │
                       │  clip editor · thumbnails · upload tab · history│
                       └───────────────┬────────────────────────────────┘
                                       │ background threads
                                       ▼
                       ┌────────────────────────────────────────────────┐
                       │            pipeline.py (orchestrator)          │
                       │  stage markers · resume · timings · job folder │
                       └───┬───────┬───────┬────────┬───────┬───────────┘
                           ▼       ▼       ▼        ▼       ▼
   source ──► ingest.py ► transcribe.py ► scenes.py ► highlights.py ► cut.py
   (file/URL)  mp4+wav     words/sents     shot list    candidates      clip mp4s
                                                            │              │
                        llm.py ◄────────────────────────────┘              ▼
                 (mock|gemini|groq|ollama)                          reframe.py
                        ▲                                           1080x1920 crop
                        │                                                │
              metadata.py ◄── rescore (weighted, drop 30%, keep ≥3)      ▼
              title/desc/tags                                     captions.py
                        │                                         ASS burn-in
                        ▼                                                │
              output/<job_ts>/clip_NN/{clip.mp4, meta.json, .srt, thumbs} ◄──┘

   Support: schemas.py (all JSON contracts) · config.py (yaml+env) · logutil.py
            cache/ (hashed artifacts, whisper models) · jobs.db (SQLite, ph3)
            youtube_upload.py (mocked in build) · errors.py (structured errors)
```

## 2. Module interfaces

| Module | Input | Output |
|---|---|---|
| ingest.py | path or URL, job dir | `video.mp4` (h264/aac normalized), `audio.wav` (16 kHz mono), IngestInfo JSON |
| transcribe.py | audio.wav, model cfg | Transcript JSON: `words[{word,start,end}]`, `sentences[{text,start,end,words[]}]`, `text` |
| scenes.py | video.mp4 | Scenes JSON: `[{index,start,end}]` |
| highlights.py | Transcript, Scenes, cfg | Candidates JSON: `[{start,end,hook,reason,score}]` (30–60 s, sentence-snapped) |
| cut.py | video.mp4, start, end | frame-accurate re-encoded `cut.mp4` |
| reframe.py | cut.mp4, scenes, aspect | `reframed.mp4` (1080×1920 / 1080×1080 / passthrough), crop-path debug JSON |
| captions.py | reframed.mp4, words, preset | `final.mp4` (burned ASS), `.ass`, `.srt` |
| metadata.py | transcript slice, hook | ClipMetadata JSON: title ≤60, description, 8–12 hashtags |
| rescore (in highlights.py) | rendered clips + transcript | ClipScore JSON per clip; kept list |
| pipeline.py | source, options | job folder with everything above + `job.json` |
| youtube_upload.py | clip + metadata + creds | upload result / setup guidance (never live in build) |
| history.py (ph3) | job events | SQLite rows; query API for UI |
| **style_profile.py** | refs/ dir, files, and/or URLs | `profiles/<name>.json` (StyleProfile), sample frames in `cache/style_frames/` |
| **subtitle_detect.py** | video.mp4, start, end | SubtitleDetectResult `{present, band_top_pct, band_bottom_pct, confidence, sampled_frames}` (cached) |
| **style_refiner.py** | candidate + transcript + scenes + subs + profile | EditPlan `{segments, words(remapped), start/ending_action, fades, cta, existing_subs, flags, caption_anchor}` |

### Style refinement layer (feature/style-refiner)

Inserted between highlight selection and rendering — a TIMELINE layer on source
timestamps + the word transcript, never on finished pixels, so the render path
still produces each clip once:

```
highlights ─► [refine stage]  ─► render per clip
              subtitle_detect(clip)      cut(_segments) ─► reframe ─► captions
              style_refiner.refine_clip  (EditPlan drives all three)
              → EditPlan per candidate
```

`style.enabled: false` skips the stage entirely — output is byte-identical to
pre-feature main. EditPlan drives: `cut_segments` (multi-segment pause removal),
`reframe_clip` (bottom-band exclusion / horizontal bias for burned subtitles),
and `caption_clip` (position law, CTA, remapped karaoke, envelope fades, weak-hook
zoom).

## 3. Data contracts

All defined in `schemas.py` as JSON-Schema dicts + validate helpers. Structures:

- **IngestInfo**: `{source, source_type, duration, width, height, fps, video_path, audio_path}`
- **Word**: `{word:str, start:float, end:float}`
- **Sentence**: `{text, start, end, words:[Word]}`
- **Transcript**: `{text, language, words:[Word], sentences:[Sentence], duration}`
- **SceneList**: `{scenes:[{index:int, start:float, end:float}]}`
- **HighlightCandidates** (LLM out): `{candidates:[{start, end, hook:str, reason:str, score:float 0-10}]}`
- **ClipScore** (LLM out): `{hook_strength, retention, clarity, impact: float 1-10, weighted:float}`
- **ClipMetadata** (LLM out): `{title:str ≤60, description:str, hashtags:[str] 8-12}`
- **JobRecord**: `{job_id, created, source, settings, stages:{name:{status,seconds}}, clips:[...]}`

Rule: no module emits JSON not covered by schemas.py; all LLM output validated before use.

## 4. LLM layer (llm.py)

One interface: `complete_json(task: str, schema: dict, prompt: str, provider=None) -> dict`.
Providers: `mock` (deterministic, schema-driven synthesis — first-class), `gemini`
(google-genai, `response_schema` structured output), `groq`, `ollama`. Lazy imports;
`import llm` needs no SDK. Provider resolution: config `auto` → gemini iff
`GEMINI_API_KEY` set else mock; logged at startup. Retry w/ exponential backoff
(2 retries), then JSON repair, then caller-specific deterministic fallback.
Live calls never happen in gates (gates force `--provider mock`).

## 5. Failure modes & recovery

| Module | Failure | Recovery |
|---|---|---|
| ingest | yt-dlp fails / bad file | structured IngestError; job marked failed, queue continues |
| ingest --sample | primary download fails | mirror 1 → mirror 2 → synthetic sample (ffmpeg generator) |
| transcribe | model download fails | retry; fall back to smaller model (base→tiny); Known Issues note |
| scenes | detector finds 0 scenes | single scene [0, duration] |
| highlights | invalid LLM JSON | retry w/ schema reminder → JSON repair → rule-based scorer |
| highlights | <3 candidates | keep all, log note (min-keep rule) |
| cut/captions | ffmpeg non-zero | structured error w/ stderr tail; clip skipped, job continues |
| reframe | no face detected | motion tracking → center crop w/ headroom |
| reframe | tracking jitter | EMA + look-ahead + velocity clamp (thresholds unit-tested) |
| metadata | LLM fails after ladder | deterministic template generator (always valid) |
| upload | no creds | setup instructions in UI, no error |
| upload | quota error | clear message, no crash |
| any stage | re-run | completion markers skip finished stages unless --force |

## 6. Performance

- CPU path (this machine): whisper `small` int8, x264 veryfast for intermediates,
  medium for finals; scene detect downscaled; face detect every N=5 frames.
- GPU path: whisper `large-v3` float16; NVENC h264 when nvidia-smi present (ph3).
- Parallel clip rendering with worker pool = min(clips, cpu_count//2) (ph3).
- Whisper models cached in `./cache/models`; transcripts/scenes cached by
  input-hash+config-hash; chunked audio processing for large files.
- Targets: 10-min 1080p ≤15 min CPU, ≤5 min GPU.

## 7. External dependencies & risks

| Dep | Risk | Mitigation |
|---|---|---|
| Python 3.11 | not preinstalled | winget silent install; fallback 3.12 (mediapipe cp312 wheels exist) |
| mediapipe | wheel lag | pin 0.10.x with cp311 wheel; if fails → OpenCV Haar cascade substitute |
| faster-whisper | ctranslate2 wheel | pin known-good versions |
| FFmpeg 8.1.2 | system-provided (Windows) | verify at setup; Docker pins its own |
| yt-dlp | YouTube flakiness | never used for --sample; user URLs best-effort |
| archive.org sample | availability | HEAD-verified + 2 mirrors + synthetic fallback |
| Inter font | download fails | try Montserrat mirror; last resort: DejaVuSans from matplotlib wheel (also libre) |
| gradio | version churn | pin exact version |

## 8. Decisions

| Decision | Alternatives considered | Rationale |
|---|---|---|
| Project name/dir `clipforge` at C:\Users\HP\clipforge | opusclip-local, ~/projects | short, descriptive; home dir not a repo |
| Python via winget 3.11.x | pyenv-win, store python | winget already present, silent-capable; spec pins 3.11 |
| Flat module layout (no src/ package) | src/clipforge pkg | spec names modules as top-level files (pipeline.py etc.); simpler CLI |
| JSON Schema dicts + jsonschema lib for contracts | pydantic | schema dicts feed Gemini response_schema directly; single source of truth |
| Mock provider synthesizes from schema + task-specific canned logic | random data | deterministic, schema-valid, exercises real code paths |
| Sample: archive.org public-domain talk | Pexels, Wikimedia | direct mp4, stable, license-clean; HEAD-verified w/ mirrors |
| Font: Montserrat (OFL) | Inter | direct raw static TTF URLs (no zip extraction); chunky weights suit captions; OFL |
| groq/ollama via plain requests HTTP | official SDKs | fewer deps; `import llm` needs no SDK; unit tests mock HTTP |
| mediapipe pinned 0.10.14 | 0.10.35 (latest) | 0.10.35 removed the legacy `solutions` API used for FaceDetection/FaceMesh |
| Crop smoothing: accel-limited trapezoidal follower | plain velocity clamp | velocity clamping alone spikes acceleration on target flips; follower guarantees both bounds by construction |
| Face detect: MediaPipe FaceDetection + FaceMesh mouth variance | true ASD models | spec forbids audio-visual ASD; largest-face + mouth-variance approximation |
| Reframe smoothing: EMA + centered moving-average look-ahead + velocity clamp | Kalman | simpler, unit-testable, meets measurable-smoothness requirement |
| Captions: libass ASS karaoke via ffmpeg subtitles filter | drawtext, moviepy | karaoke \k tags + styling native to ASS; fontsdir for bundled font |
| SQLite via stdlib sqlite3 | SQLAlchemy | zero extra deps for a single table set |
| Gradio 4.x pinned | Streamlit | spec mandates Gradio |
| Docker: static validation only | install Docker | no daemon on host (rule 6) |
| Windows host runs: `py -3.11` venv `.venv` | conda | stdlib venv sufficient |
| **Style refiner is a timeline layer (source timestamps → EditPlan), not post-processing** | filter finished mp4s | captions are burned pixels; re-chunking/repositioning a finished clip is impossible and re-encoding loses quality — refine BEFORE render so the clip is still produced once |
| Refine stage runs per-clip subtitle_detect (not once per whole source) | one detect over the source | per-clip range is more accurate (subs often appear only in parts) and is cached, so cost is the same |
| Multi-segment clips: cut_segments concat first, then reframe the cut file | teach reframe a segment list | keeps reframe's single-re-encode + crop math unchanged; scene-cut resets across concat joins are dropped (joins are removed silence, rarely mid-motion) |
| Envelope (audio fades, video fade-out, zoom-punch) applied in captions.py final pass | in cut or reframe | captions is the last full-frame re-encode and always yields final.mp4 (incl. KEEP no-caption mode); one place, always runs |
| Segment-join declick = short non-overlapping afade at each boundary | true acrossfade overlap | overlap shifts the timeline and would break the exact word-remap; a brief fade-to-zero kills the click without moving timestamps |
| hook/ending classifiers: rule heuristic is BOTH the LLM fallback and the mock answer | separate mock branch in llm.py | one deterministic rule path; mock runs are meaningful and stable with no llm.py change |
| Feature branch off origin/v2.0, not main | branch off main (spec text) | local main is v1.0.1 and lacks the v2 modules the working tree imports (segment/virality/music); v2.0 is the live line |
| Caption anchor clamped to [0.52,0.66] at BOTH schema and code | trust config value | position law is a hard invariant; enforced even if a profile is hand-edited out of range |
| **Overnight: branch off feature/style-refiner, not main** | resolve the origin/main merge | origin/main diverged (NVENC/ETA-log commits) and merging it gave a 12-hunk pipeline.py conflict; resolving core pipeline logic unattended risks corrupting decision behavior (protocol forbids). main reconciliation deferred to a human |
| Virality v2 is display/sort only; keep logic untouched | fold score into keep | true virality isn't predictable (honesty req); keep uses `rescore_clips`, so the new score changes nothing about how many clips survive |
| Per-run options applied via `apply_run_options` deep-copy | mutate the cfg singleton (existing pattern) | the singleton leaks overrides across runs; a deep copy keeps runs isolated and lets rerender reuse the same keys safely |
| Emoji captions CUT | ship with bundled font | Montserrat (the only bundled font) has no emoji glyphs → burned emoji render as tofu; needs an emoji-capable font asset (a user decision), and no new deps allowed |
| Auto-open verified by asserting the command, not opening a browser | launch a real window | protocol rule 6 (no browsers during build); `build_launch_command` is pure and unit-tested |
| Theme/CSS: theme at `launch()`, CSS via injected `<style>` | Blocks constructor args | Gradio 6 moved theme/css off the Blocks constructor (warns + ignores); `<style>` injection is version-proof |
| **UI-rework: `feature/ui-rework` off main** (main now current) | branch off v2.0 | overnight branch confirmed already merged to main (`git log main..feature/overnight-upgrades` empty); no reconciliation needed |
| **CTA works without style refinement**: no-refine path passes `style.cta` via shared `captions.cta_from_cfg` | leave CTA refine-only; or fake it | audit found CTA silently dropped when refine off (the common path) — the actual user complaint; config default `cta.enabled: true` means no-refine runs now match the refine path |
| **Pacing left honest, not fake-wired** | invent a non-refine consumer | pacing only has meaning inside the style refiner; relabelled slider + `gr.Warning` beats faking an effect the engine can't deliver |
| **Watermark image = single `-filter_complex` (scale2ref+overlay)**; audio fade routed through the graph | second encode pass; keep `-vf` | one encode preserves quality; `-filter_complex` and `-af` cannot coexist, so afade moves into the graph |
| Watermark config stays under `captions.watermark` (add `mode`) | move to `style.watermark` (brief's wording) | it already lives at `captions.watermark` and is consumed in `captions.caption_clip`; moving it would break the existing text path for no gain |
| **`style_preview.py` renders through the real write_ass + subtitles burn**, cropping a band from a true-res frame | CSS/HTML font preview | fidelity is the whole point — a CSS mock can't show libass stroke/highlight/fallback; sharing the burn path guarantees the preview equals the clip |
| **fontsdir: bundled dir unchanged; combined `cache/fonts_all` only once user fonts exist** | always a combined dir; copy user fonts into assets/fonts | keeps the common (bundled-only) output byte-identical; libass fontsdir is a single directory, so user fonts need a dir that also holds the bundled ones |
| fonttools already pinned (4.63.0) — no new dep; repaired a broken venv install | add fonttools | it was already a transitive pin; the on-disk copy was missing `ttLib`, fixed with `--force-reinstall --no-deps` |
| **Popups = `gr.Column(visible=False)` overlay; per-card buttons via `@gr.render`** | custom `gr.HTML` + `js_on_load` events | Gradio 6.19 has no `gr.Modal`; `@gr.render` gives real Python-wired buttons (no fragile JS) and survives gallery re-renders |
| Card-Edit clip pre-selection carried via a `desired_clip` State | set clip dropdown value directly | setting `job_dd` value fires its `.change` (`_job_clips`), which would clobber the selection with the first clip; the State lets `_job_clips` honor the requested clip |
| Playwright added dev-only (not in requirements.txt) | add to requirements | screenshots are a dev/verification tool, not a runtime dep; installed separately and documented |
| **GPU fix = swap ffmpeg, not patch code** (feature/gpu-fix) | fix `nvenc_available()`; update NVIDIA driver | evidence disproved the "detection bug" premise: `nvenc_available()` was already correct (real smoke encode). The true cause is system ffmpeg 8.1.2 linking NVENC SDK 13.1 (needs driver ≥610) vs installed driver 581.08 (API 13.0). Driver ≥610 isn't released and needs admin+reboot; a driver-compatible gyan.dev **ffmpeg 7.1** build in `tools/` (gitignored), selected via `config.local.yaml`→`ffmpeg.binary`, makes NVENC init succeed (proven: encoder util 100%, render 66s vs 231s CPU). Transcription already ran on GPU. |
| GPU fallbacks must name the reason | keep the single `libx264 (CPU)` line | "silent fallback" was the actual complaint; `nvenc_available()` + `transcribe.gpu_available()` now log the specific cause, and `check_gpu.py` reports it standalone |

## 9. Phase gates (summary)

- **Gate 0**: `import llm` w/ no SDKs; mock JSON valid for every schema; sample HEAD-verified; commit.
- **Gate 1**: keyless `pipeline.py --sample --provider mock` → ≥3 vertical captioned clips + valid metadata; Gradio bg-launch smoke; sentence bounds; smoothness thresholds. Tag phase-1.
- **Gate 2**: keyless batch of 2; 4 presets; single-clip re-render; thumbnails; upload guidance + mocked tests. Tag phase-2.
- **Gate 3**: clean venv keyless full run; pytest green; Docker static-validated (no daemon → pending manual verification). Tag v1.0.0.
