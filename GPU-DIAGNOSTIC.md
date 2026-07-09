# GPU-DIAGNOSTIC.md — ClipForge GPU evidence log

Machine: Windows 11, **NVIDIA GeForce GTX 1650 (4 GB), driver 581.08, CUDA 13.0**.
Generated on branch `feature/gpu-fix` (2026-07-09).

Run the live self-check any time: `.venv\Scripts\python.exe check_gpu.py`

---

## Phase 1 — Diagnosis (evidence, not assumption)

### 1. `nvidia-smi`
```
NVIDIA GeForce GTX 1650, driver 581.08, CUDA 13.0, 4096 MiB
```

### 2. NVENC encoders present in the app's ffmpeg
System ffmpeg on PATH = `Gyan.FFmpeg 8.1.2-full_build`. Encoders compiled in:
```
h264_nvenc, hevc_nvenc, av1_nvenc  ← all present
```
So this is NOT a "missing encoder" problem.

### 3. Real NVENC smoke encode (the decisive test) — **system ffmpeg 8.1.2**
```
$ ffmpeg -f lavfi -i color=black:s=64x64 -frames:v 1 -c:v h264_nvenc -f null -
[h264_nvenc] Driver does not support the required nvenc API version. Required: 13.1 Found: 13.0
[h264_nvenc] The minimum required Nvidia driver for nvenc is 610.00 or newer
exit != 0
```

**ROOT CAUSE (encoding):** ffmpeg 8.1.2 links **NVENC SDK 13.1**, which needs
NVIDIA driver **≥ 610.00**. The installed driver 581.08 only exposes NVENC API
**13.0**. The ffmpeg binary is *newer than the installed driver supports*, so
`h264_nvenc` cannot initialize. This is a binary/driver mismatch, **not** hardware
incapability and **not** a detection bug.

### 4. `ffutil.nvenc_available()` review
The probe was **already correct** — it runs a real 1-frame smoke encode (not just
an encoder-list check) and correctly returned False. The prompt's "detection bug"
hypothesis is disproven. The only code-side defect: the failure was **silent** —
the ffmpeg stderr was discarded and the log said only `encoder: libx264 (CPU)`
with no reason.

### 5. faster-whisper / ctranslate2 CUDA — **already working**
```
ctranslate2 4.8.1 → get_cuda_device_count() = 1     (CUDA compiled in)
nvidia-cublas-cu12 12.9.2.10  installed
nvidia-cudnn-cu12  9.24.0.43   installed
WhisperModel('tiny', device='cuda', compute_type='float16') → loads OK
```
Transcription already selects `cuda / large-v3 / float16` under `render.compute:
auto`. No fix needed beyond logging the reason when GPU is genuinely absent.

---

## Phase 2 — Fix

- **Compatible ffmpeg:** installed gyan.dev **ffmpeg 7.1 full build** to
  `tools/ffmpeg-7.1/` (gitignored). Its NVENC SDK matches driver 581.08.
  App points at it via `config.local.yaml` → `ffmpeg.binary`
  (`ffutil.ffmpeg_bin()` resolves `CLIPFORGE_FFMPEG` env → config → PATH).
- **No silent fallbacks:** `nvenc_available()` and `transcribe.gpu_available()`
  now log the specific reason on any CPU fallback.

### NVENC smoke encode — **ffmpeg 7.1** (the fix)
```
$ tools/ffmpeg-7.1/bin/ffmpeg.exe -f lavfi -i color=black:s=64x64 -frames:v 1 \
      -c:v h264_nvenc -f null -
exit == 0    ← NVENC initializes and encodes
```

---

## Phase 3 — Proof (actual GPU usage, not log lines)

### check_gpu.py verdict (after the fix)
```
resolved ffmpeg   ...\tools\ffmpeg-7.1\bin\ffmpeg.exe  (7.1-full_build)
NVENC smoke encode  SUCCESS (h264_nvenc)
ctranslate2         4.8.1, cuda_devices=1
GPU encode:     WORKING
GPU transcribe: WORKING
```

### Transcription on GPU (`pipeline.py --sample`)
```
clipforge.transcribe whisper device=cuda compute=float16 model=large-v3
```
During transcribe, `nvidia-smi` polling showed **GPU compute util 100%,
mem ~3.4 GB** (whisper large-v3 on CUDA; encoder util 0%, as expected — whisper
uses CUDA compute, not NVENC).

### NVENC render on GPU — encoder-utilization evidence
Log: `clipforge.ffmpeg encoder: h264_nvenc (GPU)` on every render.
`nvidia-smi --query-gpu=utilization.gpu,utilization.encoder` sampled at 0.25 s
during the render stage:
```
time         gpu%  enc%
14:56:38.56   41   100
14:56:39.39   39   100
14:56:41.03   38   100
14:56:43.50   37   100
...
peak encoder utilization: 100%
61 samples with encoder util > 0%
```
This is the hard proof: the NVENC hardware encoder was actively used.

### Timing — GPU vs CPU encoder (render stage, identical cached CUDA transcript)
| Encoder | `render_clips` stage time |
|---|---|
| **h264_nvenc (GPU)** | **66.1 s** |
| libx264 (CPU, `use_nvenc: never`) | 231.1 s |

**~3.5× faster on the render stage.** (Output frames are 1080×1920, so NVENC's
fixed overhead is easily amortized — the tiny-320×240-source caveat does not
apply to the vertical output.)

---

## Reproduce
```
.venv\Scripts\python.exe check_gpu.py           # self-report
.venv\Scripts\python.exe pipeline.py --sample    # real run (GPU)
nvidia-smi --query-gpu=utilization.gpu,utilization.encoder --format=csv -l 1
```
