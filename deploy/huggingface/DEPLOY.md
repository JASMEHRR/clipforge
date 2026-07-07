# Deploying ClipForge as a Hugging Face Space

ClipForge cannot run on serverless platforms (Vercel/Netlify/Lambda): it runs
ffmpeg and Whisper for minutes at a time and needs local disk. A Hugging Face
**Gradio Space** fits perfectly. Exact steps:

## 1. Create the Space

1. Go to https://huggingface.co/new-space
2. Name: `clipforge` · License: MIT · SDK: **Gradio** · Hardware: CPU basic
   (free) works; "CPU upgrade" is noticeably faster.
3. Create.

## 2. Upload the code

```bash
git clone https://github.com/JASMEHRR/clipforge
cd clipforge
# Replace the repo README with the Space-metadata one and use Space requirements:
cp deploy/huggingface/README.md README.md
cp deploy/huggingface/requirements.txt requirements.txt

git remote add space https://huggingface.co/spaces/YOUR_HF_USER/clipforge
git push space main:main --force
```

(Or drag-and-drop all files in the Space's "Files" tab — remember to replace
`README.md` and `requirements.txt` with the versions from `deploy/huggingface/`.)

## 3. Configure the Space (Settings → Variables and secrets)

| Type | Name | Value | Why |
|---|---|---|---|
| Variable | `CLIPFORGE_DEMO` | `1` | caps inputs at 5 min, disables YouTube upload |
| Variable | `CLIPFORGE_DEMO_MAX_SECONDS` | `300` (optional) | change the demo cap |
| Secret | `GEMINI_API_KEY` | your key (optional) | LLM highlight selection instead of the rule-based fallback |

ffmpeg is preinstalled on Gradio Spaces. The Whisper model (~460 MB) downloads
on first run and is cached afterwards.

## 4. Notes

- Uploads from visitors land in the Space's ephemeral disk; outputs vanish on
  restart. That's fine for a demo — direct serious users to run locally.
- YouTube URLs often fail on shared datacenter IPs (YouTube bot checks) —
  file upload is the reliable demo path. This is a YouTube limitation, not a
  ClipForge bug.
- Do NOT set a `YOUTUBE_CLIENT_SECRETS` secret on a public Space — anyone
  could upload to your channel. Demo mode keeps the tab disabled regardless.
