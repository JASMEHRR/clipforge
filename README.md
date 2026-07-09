# ClipForge

Self-hosted video repurposing: feed it a long video (local file or YouTube
URL), it finds the most engaging 30–60 second moments and cuts them into
vertical 9:16 clips with animated karaoke captions, titles, descriptions and
hashtags — ready for Shorts / Reels / TikTok.

**Works with zero API keys.** Without a key it uses a deterministic rule-based
highlight scorer and template metadata. Add a free Gemini key and the LLM
picks the moments and writes the metadata instead.

## Features

- Highlight detection: LLM scoring (Gemini / Groq / Ollama) with automatic
  fallback ladder (retry → JSON repair → rule-based scorer) — a bad LLM
  response can never kill a run
- Word-level transcription (faster-whisper; GPU: large-v3, CPU: small/int8)
- Intelligent 9:16 reframing: face tracking + active-speaker approximation,
  motion fallback, smoothed crop path with enforced velocity/acceleration
  limits; scene-aware resets
- Animated captions: 4 presets (karaoke-pop, bold-impact, clean-minimal,
  highlight-box), bundled OFL Montserrat fonts, `.srt` exported per clip
- Branding: text or **image logo** watermark (single-pass alpha overlay), plus
  **custom font upload** with a gallery that previews each font through the real
  caption-burn pipeline (not a CSS mock)
- Post-render re-scoring: weak clips dropped (bottom 30%), at least 3 kept
- Gradio UI: create, batch queue (+ watched `inbox/` folder), clip editing
  with sentence-snapped re-render, YouTube upload, job history
- Idempotent pipeline: cached transcripts/scenes, per-stage completion
  markers, `--force` to redo

## Quick start (Windows)

```bat
winget install Python.Python.3.11 Gyan.FFmpeg
run.bat
```

Open http://127.0.0.1:7860. Or run the CLI directly:

```bat
.venv\Scripts\python.exe pipeline.py --sample          :: built-in demo video
.venv\Scripts\python.exe pipeline.py myvideo.mp4
.venv\Scripts\python.exe pipeline.py https://youtu.be/... --preset bold-impact
```

## Quick start (Linux / macOS)

```bash
# Linux
sudo apt-get install -y ffmpeg python3.11 python3.11-venv
# macOS
brew install ffmpeg python@3.11

./run.sh
```

## Docker

```bash
docker compose up --build
# UI on http://localhost:7860 ; output/, cache/, inbox/ are volume-mounted
```

> Python is pinned to **3.11** everywhere (MediaPipe wheels lag newer
> versions). The app refuses to start on other versions with a clear message.

## Getting a free Gemini API key (optional but recommended)

1. Visit **https://aistudio.google.com/apikey** (Google AI Studio).
2. Click **Create API key**, copy it.
3. `copy .env.example .env` and set `GEMINI_API_KEY=your-key`.
4. Restart. Startup logs will show `provider 'auto' resolved to 'gemini'`.

Groq (`GROQ_API_KEY` + `llm.provider: groq`) and local Ollama
(`llm.provider: ollama`) work the same way.

## YouTube upload — one-time OAuth setup (the only manual step)

1. https://console.cloud.google.com/ → create a project.
2. **APIs & Services → Library** → enable **YouTube Data API v3**.
3. **APIs & Services → OAuth consent screen** → External → add yourself as a
   test user.
4. **Credentials → Create credentials → OAuth client ID → Desktop app** →
   download the JSON.
5. In `.env`: `YOUTUBE_CLIENT_SECRETS=C:\path\to\client_secret.json`
6. In the UI → **YouTube upload** tab → **Authorize YouTube** (opens a browser
   once; token is cached in `cache/youtube_token.json`).

Uploads are **private** by default — review them in YouTube Studio and flip
to public yourself. Quota errors surface as a clear message (default quota
allows ~6 uploads/day; it resets at midnight Pacific).

## Configuration (config.yaml)

| Key | Meaning | Default |
|---|---|---|
| `llm.provider` | `auto` / `mock` / `gemini` / `groq` / `ollama` (`auto` = gemini when key present, else mock) | `auto` |
| `whisper.gpu` / `whisper.cpu` | model matrix (auto-selected by GPU detection) | large-v3/float16, small/int8 |
| `clips.min_seconds` / `max_seconds` | hard clip length bounds | 30 / 60 |
| `clips.keep_ratio` / `min_keep` | rescore keep rules | 0.7 / 3 |
| `captions.preset` | default caption style | `karaoke-pop` |
| `captions.bottom_margin_px` | safe-zone margin above platform UI | 220 |
| `reframe.max_center_velocity_px` / `max_center_accel_px` | crop smoothness thresholds (enforced + tested) | 14 / 4 |
| `render.use_nvenc` | `auto` = NVENC when an NVIDIA GPU is present | `auto` |
| `render.parallel_workers` | `auto` = cpu_count/2 | `auto` |
| `debug` | persist transcripts, prompts, raw LLM responses, rankings, reframe frames | `false` |
| `style.enabled` | run the style refinement layer (`false` = pre-feature output) | `true` |
| `style.profile` | StyleProfile JSON steering hook/pacing/caption targets | `profiles/user.json` |
| `style.max_pause_s` / `target_pause_s` | pause above this is compressed to this | 0.6 / 0.35 |
| `style.max_removal_ratio` | cap on total time removed per clip | 0.20 |
| `style.hook_search_window_s` | forward search for a self-contained hook | 5.0 |
| `style.captions.vertical_anchor` | caption block center (hard-clamped to [0.52, 0.66]) | 0.60 |
| `style.cta.enabled` / `text` / `duration_s` | end-of-clip call-to-action overlay | true / "Follow for more" / 1.5 |
| `style.existing_subs.mode` | burned-in subtitle handling: `auto`/`replace`/`keep`/`ignore` | `auto` |
| `style.existing_subs.max_band_ratio` | REPLACE only if the detected band ≤ this fraction of frame height | 0.18 |
| `captions.watermark.mode` | `off` / `text` / `image` (image overlays a logo PNG). Legacy configs with no `mode` fall back to `enabled` → text | off |
| `captions.watermark.enabled` / `text` / `position` | brand/handle overlay burned on every clip (`top-left`/`top-right`/`bottom-left`/`bottom-right`/`center`) | false / "" / bottom-right |
| `captions.watermark.image_path` / `scale` | image mode: logo PNG (alpha respected) and its width as a fraction of the frame | "" / 0.12 |
| `captions.watermark.font_size` / `opacity` / `margin_px` | watermark styling (opacity applies to text and image) | 36 / 0.6 / 40 |
| `music.default_track` / `default_volume_db` | background-music defaults when the UI leaves them unset | "" / -22 |
| `ui.auto_open` | open the UI automatically when `app.py` starts | true |
| `ui.window_mode` | `app` = chromeless Edge/Chrome window (`--app`); `tab` = normal browser tab | app |

Secrets live **only** in `.env` (see `.env.example`). Never commit `.env`.

### Per-run options (Create → "More options")

These override the config **for one run** (applied to a private copy — the saved
config is never mutated) and thread through both the pipeline and single-clip
re-render via the shared render path:

| Option | Maps to | Default (no-op) |
|---|---|---|
| Custom CTA text | `style.cta.text` (+ enables CTA) | config value |
| Keyword highlight color | active caption preset's `highlight_color` (hex/rgb → ASS) | preset value |
| Pacing aggressiveness (0–1) | `style.max_pause_s` / `target_pause_s` within safe bounds | 0.5 |
| Min / max clip length | `clips.min_seconds` / `max_seconds` | config bounds |
| Watermark mode + text/logo + position | `captions.watermark.*` (`text`, or `image` with an uploaded logo persisted to `assets/user_branding/`) | off |
| Caption font | active preset's `font` — pick from the font gallery (see below) | preset font |
| Background music + volume | per-run music track + dB | none |
| Clips to keep | `clips.target_count` | 0 (auto) |

### Branding & fonts (Create → "Style & Branding")

- **Logo watermark**: choose watermark mode `image`, upload a transparent PNG.
  It is overlaid in the same encode as the captions (single pass, alpha
  respected) and scales to `captions.watermark.scale` of the frame width. Logos
  persist to `assets/user_branding/` (gitignored — your logo is never committed).
- **Custom fonts + real-preview gallery**: click **Browse fonts** for a popup
  listing every bundled and uploaded font, each shown as a large sample rendered
  through the *actual* caption-burn pipeline (`style_preview.py` reuses
  `captions.write_ass` + the FFmpeg subtitles filter), not a browser
  approximation. Upload `.ttf`/`.otf` files (validated, real family name read via
  fonttools; stored in `assets/user_fonts/`, gitignored). Picking a font sets it
  as the caption font for that run.
- **Clip provenance**: each result card shows `Source: mm:ss–mm:ss` — the
  original window the clip came from in the source video, before refinement
  shifted the bounds (`original_source_start_s`/`end_s` in `metadata.json`).
- **Direct edit**: each card has an **Edit this clip** button that opens the Edit
  tab pre-loaded with that clip's bounds.

Design screenshots of the reworked UI live in `design/screenshots/`.

## Engagement signals (virality v2)

Each clip gets an explainable **engagement-signals** score (0–100, banded
Strong / Promising / Weak) — never presented as a guarantee. Six sub-scores
(0–10) are computed from data the pipeline already has: **hook, completeness,
pacing, captions, duration, delivery** (weights and sources in `RESEARCH.md`).
The card gallery shows the band badge and an expandable per-signal breakdown;
the full breakdown is stored in each clip's `metadata.json`. A real LLM key adds
one rubric sub-score; under the mock provider the heuristics carry it (so the
keyless path stays deterministic). This score drives display/sort only — it does
**not** change how many clips are kept.

## Style refinement

After the highlight is chosen, ClipForge rewrites each clip's **timeline** (never
the finished pixels) so the output reads like a native Short: a self-contained
hook in the first seconds, dead-air pauses compressed, a complete/resolved
ending, punchy captions pinned in a fixed mid-lower band, an optional CTA, and
clean handling of source videos that already carry burned-in subtitles. It runs
*before* cut/reframe/captions, so every clip is still rendered exactly once. Set
`style.enabled: false` (or `--no-style`) to reproduce the pre-feature output
byte-for-byte.

**Teach it your style.** Point the analyzer at example Shorts (a folder, files,
or URLs) to distil their hook type, pacing, silence and endings into a profile:

```bat
.venv\Scripts\python.exe style_profile.py refs\ --name user
```

Then set `style.profile: profiles/user.json` in `config.yaml`. Sample frames are
written to `cache/style_frames/` so you can eyeball the references and hand-edit
the JSON (e.g. `captions.vertical_anchor`, which is clamped to [0.52, 0.66]).
`refs/` is gitignored — reference videos are never committed.

**Existing (burned-in) subtitles** — `style.existing_subs.mode`, or per run
`--subs-mode`:

| Mode | Behaviour |
|---|---|
| `auto` (default) | detect a band; if it's thin enough to crop away, exclude it and add fresh captions (**replace**); otherwise keep the source subs and bias the crop to keep them centered (**keep**) |
| `replace` | force crop-above-the-band + fresh captions when a band is detected |
| `keep` | never add captions; bias the crop to preserve the source subs |
| `ignore` | treat the clip as having none; always add captions |

Honest limits: burned-in subtitles are pixels — they cannot be moved or erased
(no OCR/inpainting). In **keep** mode, source subtitles **wider than the 9:16
window cannot be fully preserved** — that is a physical limit of cropping. The
detector can also fire on on-screen title cards; tune
`style.subtitle_detect.persistence_ratio` / `existing_subs.max_band_ratio` if it
is over-eager.

## Outputs

```
output/20260705-123456_myvideo/
  job.json  job.log
  clip_00/final.mp4  final.srt  metadata.json
  clip_01/...
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| "Python 3.11.x is required" | install 3.11 and recreate `.venv` (`run.bat`/`run.sh` do this) |
| ffmpeg not found | install FFmpeg and ensure it's on PATH (`ffmpeg -version`) |
| YouTube download fails | update yt-dlp: `pip install -U yt-dlp` (YouTube changes often) |
| Whisper model download is slow | it's cached under `cache/models` after the first run |
| Clips look soft/upscaled | feed ≥720p sources; the bundled sample is only 240p |
| GPU present but encoding runs on CPU | usually your ffmpeg's NVENC SDK is **newer than your NVIDIA driver** (e.g. ffmpeg 8.1.2 needs driver ≥610; log shows `Driver does not support the required nvenc API version`). Either update the driver, or point ClipForge at a driver-compatible ffmpeg build via `CLIPFORGE_FFMPEG=...\ffmpeg.exe` (or `ffmpeg.binary` in `config.local.yaml`). Run `python check_gpu.py` to see the exact reason. |
| `provider 'auto' resolved to 'mock'` but you set a key | key must be in `.env` next to `config.yaml`, name `GEMINI_API_KEY` |
| Upload says quota exhausted | daily API quota; resets midnight Pacific |
| UI unreachable in Docker | the container binds 0.0.0.0:7860; check `docker compose ps` and port mapping |

**GPU health check:** `.venv\Scripts\python.exe check_gpu.py` prints a plain-language
report — driver, resolved ffmpeg, a real NVENC smoke encode (with the actual error
if it fails), and whether faster-whisper will run on CUDA — so you never have to
guess why a run fell back to CPU.

## Updating

```bash
git pull
# activate your venv, then refresh dependencies in case pins changed:
pip install --no-input -r requirements.txt
```

There is also a built-in one-click self-updater (launch banner → **Install
update**): it checks GitHub, downloads only changed files (full-zipball
fallback), verifies every `.py` compiles, backs up and applies, and rolls back
automatically on any failure. Your `config.yaml` is preserved (the incoming one
lands as `config.yaml.new`). Observed behaviour and its one gap (no dry-run;
live network delta path not exercised in tests) are documented in
`UPDATER-STATUS.md`.

Job outputs, caches, and your `.env` are untouched by updates (all gitignored).

## Content rights disclaimer

ClipForge is a tool. **You are responsible for the content you process and
publish with it** — only download, clip, and re-upload videos you own or have
the rights/permission to use, and follow the terms of service of YouTube and
any platform you post to. The authors accept no liability for misuse.

## Development

```bash
.venv/Scripts/python.exe -m pytest -q         # unit tests (pure logic, mocked LLM/API)
.venv/Scripts/python.exe pipeline.py --sample --provider mock --debug
```

Optional UI screenshots (dev-only, not in `requirements.txt`):

```bash
pip install playwright && python -m playwright install chromium
.venv/Scripts/python.exe scripts/screenshot_ui.py   # → design/screenshots/
```

Architecture and design decisions: `PLAN.md`. Requirement checklist and known
issues: `PROGRESS.md`. Feature-5 wiring audit: `AUDIT.md`.

## License

- Code: **MIT** (see `LICENSE`)
- Bundled Montserrat fonts: **SIL Open Font License** (`assets/fonts/OFL.txt`)
- Bundled sample film ("Duck and Cover", 1951): **public domain** (archive.org)
