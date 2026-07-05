---
title: ClipForge
emoji: 🎬
colorFrom: purple
colorTo: pink
sdk: gradio
sdk_version: 6.19.0
python_version: "3.11"
app_file: app.py
pinned: false
license: mit
short_description: Long videos → ranked vertical Shorts with animated captions
---

# ClipForge — hosted demo

Self-hosted AI video repurposing: a long video in → the most engaging 30–60s
moments out, as vertical 9:16 clips with animated karaoke captions and ready
social metadata. No API key required (deterministic mock provider); add a
`GEMINI_API_KEY` secret for LLM-powered highlight selection.

**Demo limits** (`CLIPFORGE_DEMO=1`): inputs are capped at 5 minutes and
YouTube upload is disabled. Run it locally for the full experience:
https://github.com/megaboss69-svg/clipforge

Processing is CPU-bound — a 5-minute video takes several minutes on free
Spaces hardware. Please be patient with the progress bar.
