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

Secrets live **only** in `.env` (see `.env.example`). Never commit `.env`.

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
| `provider 'auto' resolved to 'mock'` but you set a key | key must be in `.env` next to `config.yaml`, name `GEMINI_API_KEY` |
| Upload says quota exhausted | daily API quota; resets midnight Pacific |
| UI unreachable in Docker | the container binds 0.0.0.0:7860; check `docker compose ps` and port mapping |

## Updating

```bash
git pull
# activate your venv, then refresh dependencies in case pins changed:
pip install --no-input -r requirements.txt
```

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

Architecture and design decisions: `PLAN.md`. Requirement checklist and known
issues: `PROGRESS.md`.

## License

- Code: **MIT** (see `LICENSE`)
- Bundled Montserrat fonts: **SIL Open Font License** (`assets/fonts/OFL.txt`)
- Bundled sample film ("Duck and Cover", 1951): **public domain** (archive.org)
