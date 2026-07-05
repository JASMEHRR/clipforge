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
| thumbnails.py | final.mp4 | 3 jpgs (sharp/face/mid-action heuristic) |
| rescore (in highlights.py) | rendered clips + transcript | ClipScore JSON per clip; kept list |
| pipeline.py | source, options | job folder with everything above + `job.json` |
| youtube_upload.py | clip + metadata + creds | upload result / setup guidance (never live in build) |
| history.py (ph3) | job events | SQLite rows; query API for UI |

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
| Font: Inter (OFL) primary | Montserrat | excellent legibility at caption sizes; OFL |
| Face detect: MediaPipe FaceDetection + FaceMesh mouth variance | true ASD models | spec forbids audio-visual ASD; largest-face + mouth-variance approximation |
| Reframe smoothing: EMA + centered moving-average look-ahead + velocity clamp | Kalman | simpler, unit-testable, meets measurable-smoothness requirement |
| Captions: libass ASS karaoke via ffmpeg subtitles filter | drawtext, moviepy | karaoke \k tags + styling native to ASS; fontsdir for bundled font |
| SQLite via stdlib sqlite3 | SQLAlchemy | zero extra deps for a single table set |
| Gradio 4.x pinned | Streamlit | spec mandates Gradio |
| Docker: static validation only | install Docker | no daemon on host (rule 6) |
| Windows host runs: `py -3.11` venv `.venv` | conda | stdlib venv sufficient |

## 9. Phase gates (summary)

- **Gate 0**: `import llm` w/ no SDKs; mock JSON valid for every schema; sample HEAD-verified; commit.
- **Gate 1**: keyless `pipeline.py --sample --provider mock` → ≥3 vertical captioned clips + valid metadata; Gradio bg-launch smoke; sentence bounds; smoothness thresholds. Tag phase-1.
- **Gate 2**: keyless batch of 2; 4 presets; single-clip re-render; thumbnails; upload guidance + mocked tests. Tag phase-2.
- **Gate 3**: clean venv keyless full run; pytest green; Docker static-validated (no daemon → pending manual verification). Tag v1.0.0.
